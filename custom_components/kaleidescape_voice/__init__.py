"""Kaleidescape Voice: say a movie name, the theater plays it.

Two halves:

* `library.py` scrapes the movie server's web UI, because the control protocol
  cannot enumerate the library at all.
* `player.py` speaks the undocumented PLAY_MOVIE command to the player, which is
  the only way to start an arbitrary title without driving the on-screen UI.

Everything the protocol does that will surprise you is documented in
`claude-memory/reference_kaleidescape_play_by_name.md`.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_COMMAND,
    ATTR_HANDLE,
    ATTR_INDEX,
    ATTR_PLAYER,
    ATTR_QUERY,
    ATTR_TITLE,
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_STATE,
    CONF_API_KEY,
    CONF_DEFAULT_PLAYER,
    CONF_HOST,
    CONF_PLAYERS,
    CONF_SERVER_HOST,
    DOMAIN,
    EVENT_SEARCH_RESULTS,
    LIBRARY_REFRESH_INTERVAL_HOURS,
    LOCAL_CONFIDENT_SCORE,
    MAX_RESULTS,
    SERVICE_PLAY_MOVIE,
    SERVICE_PLAY_RESULT,
    SERVICE_REFRESH_LIBRARY,
    SERVICE_SEARCH,
    SERVICE_SEND_COMMAND,
    SERVICE_VOICE_REQUEST,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .intents import async_setup_intents
from .library import (
    KaleidescapeLibrary,
    by_release_year,
    clean_query,
    names_title,
    rank_matches,
    strip_command,
    wants_playback,
)
from .now_playing import async_setup_now_playing
from .player import KaleidescapePlayer, KaleidescapeProtocolError
from .resolver import ClaudeResolver, decide_playback

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.MEDIA_PLAYER]


class KaleidescapeVoiceRuntime:
    """Everything one configured Kaleidescape system needs at runtime."""

    def __init__(
        self,
        hass: HomeAssistant,
        players: dict[str, KaleidescapePlayer],
        names: dict[str, str],
        library: KaleidescapeLibrary,
        resolver: ClaudeResolver,
        activity_entity: str = "",
        activity_state: str = "",
        default_player: str = "",
    ) -> None:
        """Initialise the runtime."""
        self.hass = hass
        self._activity_entity = activity_entity
        self._activity_state = activity_state
        self._default_player = default_player
        # serial -> client, and serial -> the name the owner set on the device.
        self.players = players
        self.names = names
        self.library = library
        self.resolver = resolver
        self.last_error: str | None = None
        self.last_query: str = ""
        self.last_results: list = []
        # Resolver matches (with confidence) behind last_results, when the
        # answer came from Claude rather than local search.
        self.last_suggestions: list = []
        self._listeners: list = []
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    def resolve_player(self, target: str | None = None) -> tuple[str, KaleidescapePlayer]:
        """Pick which player a command is for.

        Targeting is explicit by design: with several players, guessing means a
        movie starting in the wrong room, which is worse than being asked. But
        with exactly one player an unnamed command is unambiguous, so requiring
        the room there would be ceremony for its own sake.

        Order: an explicit name -> the configured default -> the sole player.
        """
        if target:
            wanted = target.strip().casefold()
            for serial, name in self.names.items():
                if name.casefold() == wanted:
                    return serial, self.players[serial]
            known = ", ".join(self.names.values())
            raise HomeAssistantError(
                f"No Kaleidescape player called '{target}'. I have: {known}."
            )

        if self._default_player:
            for serial, name in self.names.items():
                if name.casefold() == self._default_player.casefold():
                    return serial, self.players[serial]

        if len(self.players) == 1:
            serial = next(iter(self.players))
            return serial, self.players[serial]

        known = ", ".join(self.names.values())
        raise HomeAssistantError(
            f"Which player? Say a room name, or set a default in the "
            f"integration's options. I have: {known}."
        )

    @property
    def gate_open(self) -> bool:
        """True if this integration should claim the current utterance.

        `play {movie}` is a bare wildcard, so without a gate it swallows every
        "play ..." in the house -- "play some jazz" would be looked up as a movie
        title. The gate makes the Kaleidescape claim those only while it is the
        active source. Leave `activity_entity` unset to disable gating (fine for
        a single-purpose install, wrong for a house with other media players).
        """
        if not self._activity_entity:
            return True
        state = self.hass.states.get(self._activity_entity)
        return state is not None and state.state == self._activity_state

    @property
    def gate_reason(self) -> str:
        """Human-readable reason the gate is shut, for the spoken reply."""
        state = self.hass.states.get(self._activity_entity)
        current = state.state if state else "unknown"
        return (
            f"The Kaleidescape isn't the active source (it's {current}). "
            f"Switch to {self._activity_state} first."
        )

    async def async_refresh_library(self) -> int:
        """Re-scrape the library and re-detect the handle prefix."""
        count = await self.library.async_refresh()
        # The library prefix is a property of the library, not the player, so
        # ask once and share it -- but every client needs it, since each one
        # qualifies handles itself.
        handles = [m.handle for m in self.library.movies]
        first = next(iter(self.players.values()), None)
        if first is not None:
            prefix = await first.async_detect_handle_prefix(handles)
            for player in self.players.values():
                player.set_handle_prefix(prefix)
        # Re-apply cached cast/synopsis so search works immediately, before the
        # (slower) enrichment sweep has had a chance to run.
        if cached := await self._store.async_load():
            self.library.load_enrichment(cached)
        return count

    async def async_enrich_library(self) -> int:
        """Fetch any missing cast/genre/synopsis metadata and cache it."""
        added = await self.library.async_enrich()
        if added:
            await self._store.async_save(self.library.dump_enrichment())
        return added

    async def async_search(self, query: str) -> list:
        """Search, PUBLISH the results, and return them.

        Publishing is what puts the list on the remote and keeps it for a
        follow-up ("play the third one"). Callers that might act on the answer
        themselves should use async_find() and publish only if they don't --
        otherwise a list flashes up for a film that is already starting.
        """
        query = clean_query(query)
        results, suggestions = await self.async_find(query)
        self.last_suggestions = suggestions
        return self._remember(query, results)

    async def async_find(self, query: str) -> tuple[list, list]:
        """Find matches WITHOUT publishing them. Returns (results, suggestions).

        `suggestions` carries the resolver's per-match confidence when the
        answer came from Claude, and is empty when local search answered.
        """
        matches = self.library.search(query, limit=MAX_RESULTS)
        results = [m.movie for m in matches]

        # Local search only matches literal strings in the scraped metadata, so
        # a descriptive request ("the one with the president on the plane")
        # finds nothing useful. Ask Claude, grounded on the catalog. Its answer
        # replaces the local one only when local search came back weak -- a
        # confident local hit is faster and free.
        #
        # Gate on the best SCORE, not the result count. Counting conflates "one
        # perfect hit" with "one bad guess" and gets both directions wrong:
        # "air force one" and "aladdin" score 1.00 and are the only hit, so a
        # count gate paid for a resolver call on an answer it already had; while
        # "the one with the shark" returns nine junk rows at 0.31 and a count
        # gate therefore never asked, which is exactly the query that needs it.
        top = matches[0].score if matches else 0.0
        if self.resolver.available and top < LOCAL_CONFIDENT_SCORE:
            grounded = await self.resolver.async_resolve(query, self.library)
            if grounded:
                # Kept so a voice caller can ask whether this was decisive
                # enough to play outright -- see resolver.decide_playback().
                # Claude returns a themed set whose members are peers by
                # construction ("the Marvel ones"), so plain year order applies.
                return by_release_year([s.movie for s in grounded]), grounded
            if grounded is not None:
                # The resolver ran and found nothing that fits. It read the whole
                # catalog, so its "no" outranks whatever lexical crumbs local
                # search turned up -- and those crumbs are always weak here, since
                # a strong local hit is why we would not have asked at all.
                # "the one with the shark" is the case: the model names Jaws,
                # which isn't owned, and the alternative was answering a request
                # for Jaws with nine unrelated films scoring 0.31.
                # None is different and must NOT land here: it means the resolver
                # could not be consulted, so local search is all there is.
                _LOGGER.debug(
                    "resolver found nothing for %r; dropping %d weak local hit(s)",
                    query,
                    len(matches),
                )
                return [], []

        elif top < LOCAL_CONFIDENT_SCORE and matches:
            # Same judgement, no resolver to appeal to. Local search has already
            # said these are weak, and weak local hits are not merely thin --
            # they are confidently wrong. Unenriched, "james bond" returns seven
            # films matched on a DIRECTOR'S FIRST NAME (James Gunn, James
            # Mangold, James Cameron): Guardians of the Galaxy, Terminator,
            # Titanic, and not one Bond film.
            #
            # With a key those are replaced by the resolver's answer. Without
            # one, serving them anyway would make "no API key" mean "answers get
            # worse" rather than "descriptive requests are unavailable" -- so
            # they are dropped here too. Everywhere else this integration prefers
            # nothing over a wrong answer; this is the one place that didn't.
            _LOGGER.debug(
                "best local score %.2f for %r is below %.2f and no resolver is "
                "configured; dropping %d weak hit(s)",
                top,
                query,
                LOCAL_CONFIDENT_SCORE,
                len(matches),
            )
            return [], []

        # Local hits carry scores, so year-sort within relevance tiers rather
        # than across them -- see rank_matches().
        return rank_matches(matches), []

    def _remember(self, query: str, results: list) -> list:
        """Keep results for a follow-up, and tell everything that renders them."""
        self.last_query = query
        self.last_results = results
        self.hass.bus.async_fire(
            EVENT_SEARCH_RESULTS,
            {
                "query": query,
                "count": len(results),
                "results": [m.as_result() for m in results],
            },
        )
        for callback in list(self._listeners):
            callback()
        return results

    async def async_play_title(self, spoken: str, target: str | None = None) -> str:
        """Resolve a spoken title and play it. Returns the title played."""
        match = self.library.best(spoken)
        if match is None:
            raise HomeAssistantError(
                f"'{spoken}' did not match one title confidently. "
                f"Use {DOMAIN}.search to see the candidates."
            )
        return await self.async_play_handle(match.movie.handle, target)

    async def async_voice_request(self, spoken: str, target: str | None = None) -> dict:
        """Handle a free-form spoken request: play it, or offer a list.

        This is the whole voice path in one place, and it takes ANY phrasing --
        there is no sentence template in front of it. That is deliberate: the
        templates exist to tell HA's shared agent which utterances are ours, and
        this entry point is reached only when the caller already knows (the
        remote, having checked that the Kaleidescape is the active source), so
        matching phrasing twice would only add ways to fail.

        Order matters. A named title resolves locally in milliseconds, offline
        and free, and that is the most common request by far -- so it is tried
        first and the resolver never sees "play Aladdin".
        """
        # Strip the leading verb ONCE, here, and use the result for everything
        # below. On this path no sentence template has removed it, so leaving it
        # in means searching for it: "find james bond" returned 4 films instead
        # of 17 because "find" occurs across the synopses.
        # Does this ask for a film to START, or ask ABOUT films? Decided on the
        # RAW utterance, before the verb is stripped -- stripping is what removes
        # the evidence. "watch the one where the boy is left home alone in new
        # york" and "which movie is a child left alone in new york city" resolve
        # to the same film with the same certainty; only the first wants it
        # played.
        play_intent = wants_playback(spoken)
        spoken = clean_query(strip_command(spoken))
        if not spoken:
            return {"action": "none"}

        # 1. Named title, decisively matched -> play it. best() already refuses
        #    when two titles score within a hair of each other; names_title()
        #    additionally requires that the title accounts for most of what was
        #    said, so a DESCRIPTION that happens to contain a title's words
        #    ("the one where the boy is left home alone") goes to the resolver
        #    instead of playing Home Alone and skipping the choice.
        match = self.library.best(spoken)
        if play_intent and match is not None and names_title(spoken, match.movie.title):
            title = await self.async_play_handle(match.movie.handle, target)
            self.last_suggestions = []
            self._remember(spoken, [])
            return {
                "action": "played",
                "title": title,
                "via": "local",
                "message": f"Playing {title}",
            }

        # 2. Otherwise find -- WITHOUT publishing yet. Publishing here would put
        #    a picker on the remote for a film that is about to start playing
        #    anyway, so the decision comes first and only one of the two
        #    outcomes reaches the screen.
        results, suggestions = await self.async_find(spoken)

        # 3. Play outright only if the resolver was decisive AND unambiguous.
        if play_intent and (movie := decide_playback(suggestions)) is not None:
            title = await self.async_play_handle(movie.handle, target)
            # Clear any list left from an earlier request: what is on screen
            # should describe what is on screen.
            self.last_suggestions = []
            self._remember(spoken, [])
            return {
                "action": "played",
                "title": title,
                "via": "resolver",
                "message": f"Playing {title}",
            }

        self.last_suggestions = suggestions
        self._remember(spoken, results)
        if not results:
            return {
                "action": "none",
                "count": 0,
                "query": spoken,
                "message": f"Nothing in your library matches “{spoken}”",
            }
        return {
            "action": "results",
            "count": len(results),
            "query": spoken,
            "message": f"{len(results)} matches for “{spoken}”",
        }

    async def async_play_index(self, index: int, target: str | None = None) -> str:
        """Play the Nth result of the last search (1-based)."""
        if not self.last_results:
            raise HomeAssistantError("There are no search results to pick from.")
        if not 1 <= index <= len(self.last_results):
            raise HomeAssistantError(
                f"Pick a number between 1 and {len(self.last_results)}."
            )
        return await self.async_play_handle(self.last_results[index - 1].handle, target)

    def add_listener(self, callback) -> None:
        """Register a callback fired when search results change."""
        self._listeners.append(callback)

    async def async_play_handle(self, handle: str, target: str | None = None) -> str:
        """Play a specific handle on a chosen player."""
        movie = self.library.get(handle)
        serial, player = self.resolve_player(target)
        try:
            confirmed = await player.async_play_handle(handle)
        except KaleidescapeProtocolError as err:
            self.last_error = str(err)
            raise HomeAssistantError(
                f"{self.names.get(serial, serial)} did not start playback: {err}"
            ) from err

        self.last_error = None
        return confirmed or (movie.title if movie else handle)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Kaleidescape system from a config entry."""
    host = entry.data[CONF_HOST]
    configured = entry.data.get(CONF_PLAYERS, [])
    if not configured:
        raise HomeAssistantError("no Kaleidescape players configured")

    # One address, N players: the protocol forwards by serial (see system.py).
    players = {p["serial"]: KaleidescapePlayer(host, serial=p["serial"])
               for p in configured}
    names = {p["serial"]: p["name"] for p in configured}

    probe = next(iter(players.values()))
    try:
        await probe.async_is_awake()
    except (KaleidescapeProtocolError, OSError, TimeoutError) as err:
        raise HomeAssistantError(
            f"cannot reach the Kaleidescape system at {host}: {err}"
        ) from err

    library = KaleidescapeLibrary(
        async_get_clientsession(hass), entry.data[CONF_SERVER_HOST]
    )
    resolver = ClaudeResolver(entry.options.get(CONF_API_KEY, ""))
    runtime = KaleidescapeVoiceRuntime(
        hass,
        players,
        names,
        library,
        resolver,
        activity_entity=entry.options.get(CONF_ACTIVITY_ENTITY, ""),
        activity_state=entry.options.get(CONF_ACTIVITY_STATE, ""),
        default_player=entry.options.get(CONF_DEFAULT_PLAYER, ""),
    )

    try:
        await runtime.async_refresh_library()
    except Exception as err:  # noqa: BLE001 - a stale library must not block setup
        _LOGGER.warning("initial library scrape failed (will retry): %s", err)
        runtime.last_error = str(err)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    async def _scheduled_refresh(_now) -> None:
        try:
            await runtime.async_refresh_library()
            # Enrich in the same pass. Re-scraping alone picks a new movie's
            # title up (so it is playable by name) but leaves it with no cast or
            # synopsis -- which are exactly what "james bond" and every
            # descriptive query match on, and what the Claude catalog carries.
            # Without this, a movie bought today is invisible to search until HA
            # restarts. `only_missing` means this costs nothing when nothing
            # changed.
            await runtime.async_enrich_library()
        except Exception as err:  # noqa: BLE001 - never let the timer die
            _LOGGER.warning("scheduled library refresh failed: %s", err)
            runtime.last_error = str(err)

    entry.async_on_unload(
        async_track_time_interval(
            hass, _scheduled_refresh, timedelta(hours=LIBRARY_REFRESH_INTERVAL_HOURS)
        )
    )

    async def _enrich(_now=None) -> None:
        try:
            await runtime.async_enrich_library()
        except Exception as err:  # noqa: BLE001 - search degrades, playback still works
            _LOGGER.warning("library enrichment failed: %s", err)

    # One request per title, so this runs in the background rather than blocking
    # setup. Playing by name works immediately; search by actor/franchise only
    # becomes accurate once this finishes (~12 s cold, instant when cached).
    entry.async_create_background_task(hass, _enrich(), "kaleidescape_voice_enrich")

    # Keep the BUILT-IN kaleidescape integration's now-playing title and poster
    # live, if it is configured. Soft dependency: absent, this does nothing.
    if (stop_now_playing := async_setup_now_playing(hass)) is not None:
        entry.async_on_unload(stop_now_playing)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    await async_setup_intents(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


def _first_runtime(hass: HomeAssistant) -> KaleidescapeVoiceRuntime:
    """Return the single configured system (this integration is one-per-house)."""
    runtimes = list(hass.data.get(DOMAIN, {}).values())
    if not runtimes:
        raise HomeAssistantError("Kaleidescape Voice is not configured")
    return runtimes[0]


def _async_register_services(hass: HomeAssistant) -> None:
    """Register services once, regardless of entry count."""
    if hass.services.has_service(DOMAIN, SERVICE_PLAY_MOVIE):
        return

    async def _play_movie(call: ServiceCall) -> dict[str, str]:
        runtime = _first_runtime(hass)
        target = call.data.get(ATTR_PLAYER)
        if handle := call.data.get(ATTR_HANDLE):
            title = await runtime.async_play_handle(handle, target)
        else:
            title = await runtime.async_play_title(call.data[ATTR_TITLE], target)
        return {"title": title}

    async def _search(call: ServiceCall) -> dict:
        runtime = _first_runtime(hass)
        results = await runtime.async_search(call.data[ATTR_QUERY])
        return {
            "query": call.data[ATTR_QUERY],
            "count": len(results),
            "results": [m.as_result() for m in results],
        }

    async def _voice_request(call: ServiceCall) -> dict:
        """A free-form spoken request: play it outright, or offer a list."""
        runtime = _first_runtime(hass)
        return await runtime.async_voice_request(
            call.data[ATTR_QUERY], call.data.get(ATTR_PLAYER)
        )

    async def _play_result(call: ServiceCall) -> dict[str, str]:
        runtime = _first_runtime(hass)
        return {
            "title": await runtime.async_play_index(
                int(call.data[ATTR_INDEX]), call.data.get(ATTR_PLAYER)
            )
        }

    async def _refresh_library(_call: ServiceCall) -> dict[str, int]:
        runtime = _first_runtime(hass)
        count = await runtime.async_refresh_library()
        return {"count": count, "enriched": await runtime.async_enrich_library()}

    async def _send_command(call: ServiceCall) -> dict[str, list[str]]:
        runtime = _first_runtime(hass)
        _, player = runtime.resolve_player(call.data.get(ATTR_PLAYER))
        return {"response": await player.async_send_raw(call.data[ATTR_COMMAND])}

    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_MOVIE,
        _play_movie,
        schema=vol.Schema(
            vol.All(
                {
                    vol.Optional(ATTR_TITLE): cv.string,
                    vol.Optional(ATTR_HANDLE): cv.string,
                    vol.Optional(ATTR_PLAYER): cv.string,
                },
                cv.has_at_least_one_key(ATTR_TITLE, ATTR_HANDLE),
            )
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH,
        _search,
        schema=vol.Schema({vol.Required(ATTR_QUERY): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VOICE_REQUEST,
        _voice_request,
        schema=vol.Schema({
            vol.Required(ATTR_QUERY): cv.string,
            vol.Optional(ATTR_PLAYER): cv.string,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_RESULT,
        _play_result,
        schema=vol.Schema(
            {
                vol.Required(ATTR_INDEX): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(ATTR_PLAYER): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_LIBRARY, _refresh_library, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        _send_command,
        schema=vol.Schema(
            {
                vol.Required(ATTR_COMMAND): cv.string,
                vol.Optional(ATTR_PLAYER): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
