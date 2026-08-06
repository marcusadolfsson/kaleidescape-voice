"""Sensor exposing the state of the scraped Kaleidescape library."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_SERVER_HOST, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the library sensor."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            KaleidescapeLibrarySensor(runtime, entry),
            KaleidescapeSearchResultsSensor(runtime, entry),
        ]
    )


class KaleidescapeLibrarySensor(SensorEntity):
    """How many titles the voice matcher currently knows about."""

    _attr_has_entity_name = True
    _attr_name = "Library titles"
    _attr_icon = "mdi:movie-open"
    _attr_native_unit_of_measurement = "movies"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, runtime, entry: ConfigEntry) -> None:
        """Initialise the sensor."""
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_library_titles"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Kaleidescape Voice",
            manufacturer="Kaleidescape",
            model="Voice Control",
            # The web UI lives on the server (the player serves no useful HTTP).
            configuration_url=f"http://{entry.data[CONF_SERVER_HOST]}",
        )

    @property
    def native_value(self) -> int:
        """Number of titles in the library."""
        return len(self._runtime.library)

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Diagnostics that matter when playback stops working."""
        first = next(iter(self._runtime.players.values()), None)
        return {
            "players": ", ".join(self._runtime.names.values()),
            # If this drifts from the library's namespace, every play silently
            # no-ops -- worth having visible.
            "handle_prefix": first.handle_prefix if first else "",
            "last_error": self._runtime.last_error,
        }


class KaleidescapeSearchResultsSensor(SensorEntity):
    """The last search's results, for a dashboard or the remote to render.

    Speech is a bad channel for "here are 27 Bond films", so when a request is
    too vague to play, the candidates land here (and on the
    kaleidescape_voice_search_results event) to be shown on a screen. Each entry
    carries its handle, so a UI can play one directly via
    kaleidescape_voice.play_movie.
    """

    _attr_has_entity_name = True
    _attr_name = "Search results"
    _attr_icon = "mdi:movie-search"
    _attr_native_unit_of_measurement = "results"
    _attr_should_poll = False

    def __init__(self, runtime, entry: ConfigEntry) -> None:
        """Initialise the sensor."""
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_search_results"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        """Refresh whenever a new search runs."""
        self._runtime.add_listener(self.async_write_ha_state)

    @property
    def native_value(self) -> int:
        """Number of results from the last search."""
        return len(self._runtime.last_results)

    @property
    def extra_state_attributes(self) -> dict:
        """The results themselves, numbered to match what was spoken."""
        return {
            "query": self._runtime.last_query,
            "results": [
                {"index": i, **movie.as_result()}
                for i, movie in enumerate(self._runtime.last_results, start=1)
            ],
        }
