"""Search results as a media_player, so a remote or dashboard can render them.

Speech is the wrong reply channel for this: a theater often has no speaker free,
and seventeen Bond films want to be *tapped*, not recited. An on-screen display
on the projector was tried first and was worse -- modal, no line breaks, and
blank whenever the video path bypassed the device drawing it.

So the results need an entity shape that existing UI can already render as a
LIVE list. That rules out anything whose options come from layout config, since
a results list is not known when the dashboard is written. `source_list` is the
one widely-supported attribute that is genuinely dynamic: a card reads it and
fires `media_player.select_source` on the chosen entry.

Hence a media_player. Results appear on hardware whose firmware you cannot
change -- which is a far bigger lift than adding an entity.

It is deliberately a picker, not a transport: playback lives on the real
`media_player.kaleidescape_strato_e`.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_RESULTS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the search-results picker."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KaleidescapeSearchPicker(runtime, entry)])


class KaleidescapeSearchPicker(MediaPlayerEntity):
    """A tappable list of the last search's results."""

    _attr_has_entity_name = True
    _attr_name = "Search"
    _attr_icon = "mdi:movie-search"
    _attr_should_poll = False
    _attr_supported_features = MediaPlayerEntityFeature.SELECT_SOURCE

    def __init__(self, runtime, entry: ConfigEntry) -> None:
        """Initialise the picker."""
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_search_picker"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        """Re-render whenever a new search runs."""
        self._runtime.add_listener(self.async_write_ha_state)

    @property
    def state(self) -> MediaPlayerState:
        """`on` while there is a list to pick from."""
        return (
            MediaPlayerState.ON
            if self._runtime.last_results
            else MediaPlayerState.OFF
        )

    @property
    def media_title(self) -> str | None:
        """What was searched for, shown above the list."""
        if not self._runtime.last_results:
            return None
        return f"{self._runtime.last_query} ({len(self._runtime.last_results)})"

    @property
    def source_list(self) -> list[str]:
        """The results, numbered to match what was spoken back."""
        return [
            self._label(index, movie)
            for index, movie in enumerate(self._runtime.last_results[:MAX_RESULTS], 1)
        ]

    @property
    def source(self) -> None:
        """Always unset -- picking is an action, not a persistent selection."""
        return None

    @staticmethod
    def _label(index: int, movie) -> str:
        """Number + title + year, so duplicates across years stay distinct."""
        year = f" ({movie.year})" if movie.year else ""
        return f"{index}. {movie.title}{year}"

    async def async_select_source(self, source: str) -> None:
        """Play whichever result was tapped."""
        for index, movie in enumerate(self._runtime.last_results[:MAX_RESULTS], 1):
            if self._label(index, movie) == source:
                await self._runtime.async_play_handle(movie.handle)
                return
        # The list changed under the user's finger (a new search landed between
        # render and tap). Fall back to the leading number rather than failing.
        number, _, _ = source.partition(".")
        if number.strip().isdigit():
            await self._runtime.async_play_index(int(number.strip()))
