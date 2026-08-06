"""Keep the built-in `kaleidescape` integration's now-playing data live.

Home Assistant's own Kaleidescape integration shows no title or poster for
streamed content, and the cause is not a missing feature: pykaleidescape's
`Device.refresh()` already fetches the play status and the highlighted
selection's details, which is what populates `movie.title`, `movie.cover` and
`movie.handle`. The integration simply never calls it on a timer -- its entities
are `should_poll = False` and only write state from dispatcher events -- so after
a reconnect or an HA restart mid-playback the device object goes stale and the
card empties out.

So this doesn't patch or reimplement anything. It calls the library's own public
refresh on a timer and pushes a state write. Because `media_image_url` is the
player's own HTTP art URL, HA's media_player_proxy then serves `entity_picture`
same-origin, so HTTPS dashboards and remotes alike get the poster
with no further plumbing.

## Two things worth knowing before changing this

**`refresh()` does not dispatch.** It updates the device object directly, taking
a different path from the handler that fires dispatcher events -- so refreshing
alone changes nothing on screen. The state write has to be forced separately,
which is what `async_update_entity` is for here.

**It is a no-op while the player sleeps.** `refresh()` returns early unless the
power state is ON, so a quiet timer is expected rather than a symptom. Anyone
measuring "is the shim working?" against a sleeping player will conclude wrongly
that it is dead -- that mistake has been made.

## Why this lives here now

It began as a separate `kaleidescape_meta` component, which had to monkeypatch
`KaleidescapeMediaPlayer.async_added_to_hass` to get hold of live entities and
force a config-entry reload at every startup so its patch would take. Neither is
needed any more: HA stores the device on `entry.runtime_data`, and the entity
registry maps the entry to its entities. Folding it in drops both, and one
Kaleidescape integration is easier to reason about than two.

The dependency is soft (`after_dependencies`): with the built-in integration
absent this does nothing, and everything else in kaleidescape_voice -- which
talks to the player over its own TCP client -- carries on regardless.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

KALEIDESCAPE_DOMAIN = "kaleidescape"
POLL_INTERVAL = timedelta(seconds=20)


def _targets(hass: HomeAssistant) -> list[tuple[object, str]]:
    """Return (device, media_player entity_id) for each built-in entry."""
    found: list[tuple[object, str]] = []
    registry = er.async_get(hass)
    for entry in hass.config_entries.async_entries(KALEIDESCAPE_DOMAIN):
        device = getattr(entry, "runtime_data", None)
        if device is None:
            continue
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == "media_player":
                found.append((device, entity.entity_id))
    return found


def async_setup_now_playing(hass: HomeAssistant) -> CALLBACK_TYPE | None:
    """Start the refresh timer. Returns an unsubscribe, or None if not wanted."""
    if not hass.config_entries.async_entries(KALEIDESCAPE_DOMAIN):
        _LOGGER.debug(
            "built-in kaleidescape integration not configured; "
            "not refreshing now-playing metadata"
        )
        return None

    async def _tick(_now=None) -> None:
        for device, entity_id in _targets(hass):
            try:
                await device.refresh()
            except Exception as err:  # noqa: BLE001 - the timer must never die
                _LOGGER.debug("refresh failed for %s: %s", entity_id, err)
                continue
            # refresh() mutates the device without dispatching, so the entity
            # has no idea anything changed until it is told to write state.
            await async_update_entity(hass, entity_id)

    _LOGGER.debug(
        "refreshing kaleidescape now-playing metadata every %ss",
        int(POLL_INTERVAL.total_seconds()),
    )
    return async_track_time_interval(hass, _tick, POLL_INTERVAL)
