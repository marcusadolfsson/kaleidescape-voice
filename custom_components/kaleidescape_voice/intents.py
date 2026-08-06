"""Intent handlers for Kaleidescape Voice.

These are deliberately plain intents rather than an LLM-backed conversation
agent. Playing a named title is a lookup against a 202-row table, and a local
sentence match resolves it in milliseconds, offline, for free -- an LLM adds
latency and a failure mode for no benefit.

The important rule here: **playback requires confidence**. If the phrase does
not resolve to exactly one title, nothing plays -- the library is searched and
the results are handed back instead. "james bond" is not a title and matches
twenty-odd films; picking one would be a coin flip the user has to undo.
Results are remembered so "play number three" completes the interaction.

**There is no speaker to answer on.** The spoken text set here is largely
vestigial -- there may be nothing to speak it. The real reply surface is the
screen: results are published on `sensor.*_search_results` and the
`kaleidescape_voice_search_results` event, and the remote renders them as a
tappable list. That beats reading seventeen Bond titles aloud.
"""

from __future__ import annotations

import logging
import random

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, intent

from .const import DOMAIN, MAX_SPOKEN_RESULTS
from .library import by_release_year, clean_query

_LOGGER = logging.getLogger(__name__)

INTENT_PLAY_MOVIE = "KaleidescapePlayMovie"
INTENT_TRANSPORT = "KaleidescapeTransport"
INTENT_WHATS_PLAYING = "KaleidescapeWhatsPlaying"
INTENT_FIND = "KaleidescapeFind"
INTENT_SURPRISE = "KaleidescapeSurprise"
INTENT_SEARCH = "KaleidescapeSearch"
INTENT_PLAY_RESULT = "KaleidescapePlayResult"

# Spoken action -> protocol command. Zero-argument transport commands only.
TRANSPORT_COMMANDS: dict[str, str] = {
    "play": "PLAY",
    "pause": "PAUSE",
    "toggle": "PLAY_OR_PAUSE",
    "stop": "STOP",
    "next": "NEXT",
    "previous": "PREVIOUS",
    "replay": "REPLAY",
    "forward": "SCAN_FORWARD",
    "rewind": "SCAN_REVERSE",
    "details": "DETAILS",
    "menu": "KALEIDESCAPE_MENU_TOGGLE",
    "covers": "GO_MOVIE_COVERS",
    "list": "GO_MOVIE_LIST",
    "subtitles": "SUBTITLES_NEXT",
    "audio": "AUDIO_NEXT",
}

TRANSPORT_REPLIES: dict[str, str] = {
    "play": "Playing.",
    "pause": "Paused.",
    "toggle": "OK.",
    "stop": "Stopped.",
    "next": "Next chapter.",
    "previous": "Previous chapter.",
    "replay": "Back ten seconds.",
    "forward": "Fast forwarding.",
    "rewind": "Rewinding.",
    "details": "Showing details.",
    "menu": "Menu.",
    "covers": "Showing movie covers.",
    "list": "Showing the movie list.",
    "subtitles": "Changed subtitles.",
    "audio": "Changed the audio track.",
}


def _runtime(hass: HomeAssistant):
    """Return the configured runtime, or raise a speakable error."""
    runtimes = list(hass.data.get(DOMAIN, {}).values())
    if not runtimes:
        raise intent.IntentHandleError("Kaleidescape Voice is not set up.")
    return runtimes[0]


def _speak_results(query: str, results: list) -> str:
    """Phrase a result list for speech without reciting all of it."""
    if not results:
        return f"I couldn't find anything matching {query} in your library."

    shown = results[:MAX_SPOKEN_RESULTS]
    names = ", ".join(
        f"{i}. {m.title}" for i, m in enumerate(shown, start=1)
    )
    if len(results) <= MAX_SPOKEN_RESULTS:
        return f"I found {len(results)}: {names}. Say a number to play one."
    return (
        f"I found {len(results)} matching {query}. "
        f"The first {len(shown)} are: {names}. "
        "Say a number to play one, or be more specific."
    )


def _gated(runtime, intent_obj):
    """Return a refusal response when the Kaleidescape isn't the active source.

    Applied to the intents whose sentences are broad enough to catch utterances
    meant for something else -- `play {movie}` in particular is a bare wildcard.
    Returning early here means "play some jazz" gets a clear answer instead of
    being looked up as a movie title.
    """
    if runtime.gate_open:
        return None
    response = intent_obj.create_response()
    response.async_set_speech(runtime.gate_reason)
    _LOGGER.debug("gate shut; declining utterance")
    return response


class PlayMovieIntent(intent.IntentHandler):
    """Play a movie by spoken title, or search when it isn't clear-cut."""

    intent_type = INTENT_PLAY_MOVIE
    slot_schema = {vol.Required("movie"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Play only on a confident single match; otherwise search."""
        runtime = _runtime(intent_obj.hass)
        if (refused := _gated(runtime, intent_obj)) is not None:
            return refused
        spoken = intent_obj.slots["movie"]["value"]
        response = intent_obj.create_response()

        match = runtime.library.best(spoken)
        if match is not None:
            title = await runtime.async_play_handle(match.movie.handle)
            response.async_set_speech(f"Playing {title}.")
            return response

        # Not confident. Never pick one at random -- "james bond" matches 20+
        # films and guessing is worse than showing the list. The results are
        # remembered so "play number three" works as a follow-up, and fired as
        # an event so a screen can display them.
        results = await runtime.async_search(spoken)
        response.async_set_speech(_speak_results(spoken, results))
        return response


class SearchIntent(intent.IntentHandler):
    """Explicitly search the library."""

    intent_type = INTENT_SEARCH
    slot_schema = {vol.Required("query"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Search and report."""
        runtime = _runtime(intent_obj.hass)
        if (refused := _gated(runtime, intent_obj)) is not None:
            return refused
        # Clean here too, not just inside async_search: the spoken reply quotes
        # the query back, and quoting the raw slot said "8 matching any
        # Spielberg movies" for a search that actually ran on "Spielberg".
        query = clean_query(str(intent_obj.slots["query"]["value"]))
        results = await runtime.async_search(query)
        response = intent_obj.create_response()
        response.async_set_speech(_speak_results(query, results))
        return response


class PlayResultIntent(intent.IntentHandler):
    """Play the Nth result of the previous search."""

    intent_type = INTENT_PLAY_RESULT
    slot_schema = {vol.Required("index"): vol.Coerce(int)}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Resolve the ordinal against the remembered results."""
        runtime = _runtime(intent_obj.hass)
        if (refused := _gated(runtime, intent_obj)) is not None:
            return refused
        index = int(intent_obj.slots["index"]["value"])
        response = intent_obj.create_response()

        if not runtime.last_results:
            response.async_set_speech("I don't have a list of results to pick from.")
            return response
        if not 1 <= index <= len(runtime.last_results):
            response.async_set_speech(
                f"Pick a number between 1 and {len(runtime.last_results)}."
            )
            return response

        title = await runtime.async_play_handle(runtime.last_results[index - 1].handle)
        response.async_set_speech(f"Playing {title}.")
        return response


class TransportIntent(intent.IntentHandler):
    """Transport and navigation control."""

    intent_type = INTENT_TRANSPORT
    slot_schema = {vol.Required("action"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Send the mapped protocol command."""
        runtime = _runtime(intent_obj.hass)
        if (refused := _gated(runtime, intent_obj)) is not None:
            return refused
        action = intent_obj.slots["action"]["value"].lower().strip()

        command = TRANSPORT_COMMANDS.get(action)
        response = intent_obj.create_response()
        if command is None:
            response.async_set_speech(f"I don't know how to {action} on Kaleidescape.")
            return response

        try:
            _, player = runtime.resolve_player()
        except HomeAssistantError as err:
            response.async_set_speech(str(err))
            return response
        await player.async_transport(command)
        response.async_set_speech(TRANSPORT_REPLIES.get(action, "OK."))
        return response


class WhatsPlayingIntent(intent.IntentHandler):
    """Report the current title."""

    intent_type = INTENT_WHATS_PLAYING

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Read play status from the player."""
        runtime = _runtime(intent_obj.hass)
        response = intent_obj.create_response()
        try:
            _, player = runtime.resolve_player()
        except HomeAssistantError as err:
            response.async_set_speech(str(err))
            return response
        status = await player.async_get_play_status()

        title = status.get("title")
        mode = status.get("mode", "idle")
        if not title:
            reply = "Nothing is playing on the Kaleidescape."
        elif mode == "paused":
            reply = f"{title} is paused."
        else:
            reply = f"Playing {title}."
        response.async_set_speech(reply)
        return response


class FindIntent(intent.IntentHandler):
    """Answer questions about what is in the library."""

    intent_type = INTENT_FIND
    slot_schema = {
        vol.Optional("genre"): cv.string,
        vol.Optional("director"): cv.string,
        vol.Optional("actor"): cv.string,
        vol.Optional("year"): cv.string,
        vol.Optional("rating"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Filter the library and speak a short summary."""
        runtime = _runtime(intent_obj.hass)
        criteria = {
            key: clean_query(str(slot["value"]))
            for key, slot in intent_obj.slots.items()
            if key in ("genre", "director", "actor", "year", "rating")
        }
        criteria = {k: v for k, v in criteria.items() if v}
        results = runtime.library.filter(**criteria)
        label = " ".join(str(v) for v in criteria.values()) or "your library"

        # A metadata filter only knows the handful of values the server actually
        # publishes, so a slot that isn't one of them ("the marvel universe",
        # "james bond") filters to nothing -- and answering "no movies" is
        # wrong, because a full search DOES find those. Fall back to search,
        # which covers cast, director and synopsis, and can reach the resolver.
        if not results and criteria:
            results = await runtime.async_search(label)
            response = intent_obj.create_response()
            response.async_set_speech(_speak_results(label, results))
            return response

        response = intent_obj.create_response()

        # Same treatment as a search: the list goes on the wall, is remembered
        # for "play number two", and only a summary is spoken.
        runtime.last_query = label
        runtime.last_results = by_release_year(results)
        results = runtime.last_results
        response.async_set_speech(_speak_results(label, results))
        return response


class SurpriseIntent(intent.IntentHandler):
    """Pick something at random, optionally within a genre."""

    intent_type = INTENT_SURPRISE
    slot_schema = {vol.Optional("genre"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Choose a title and play it."""
        runtime = _runtime(intent_obj.hass)
        if (refused := _gated(runtime, intent_obj)) is not None:
            return refused
        genre = intent_obj.slots.get("genre", {}).get("value")
        pool = runtime.library.filter(genre=genre) if genre else runtime.library.movies

        response = intent_obj.create_response()
        if not pool:
            response.async_set_speech("I couldn't find anything to pick from.")
            return response

        choice = random.choice(pool)
        title = await runtime.async_play_handle(choice.handle)
        response.async_set_speech(f"Playing {title}.")
        return response


async def async_setup_intents(hass: HomeAssistant) -> None:
    """Register every intent handler (idempotent across config entries)."""
    if hass.data.get(f"{DOMAIN}_intents_registered"):
        return
    for handler in (
        PlayMovieIntent(),
        SearchIntent(),
        PlayResultIntent(),
        TransportIntent(),
        WhatsPlayingIntent(),
        FindIntent(),
        SurpriseIntent(),
    ):
        intent.async_register(hass, handler)
    hass.data[f"{DOMAIN}_intents_registered"] = True
    _LOGGER.debug("Kaleidescape Voice intents registered")
