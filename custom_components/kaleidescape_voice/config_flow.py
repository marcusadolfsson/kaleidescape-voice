"""Config flow for Kaleidescape Voice.

Setup asks for **one address** and works the rest out.

The first version asked for the player's IP *and* the server's, which was both
the easiest thing to get wrong (they are different machines with different
roles, and swapping them yields a system that looks configured and plays
nothing) and useless for a second player.

It is unnecessary. The control protocol forwards by device: connect to any
component and address another with `#<serial>/`. So the flow takes any
Kaleidescape address, enumerates the system, picks the server for the library
and registers every player it found (see system.py).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACTIVITY_ENTITY,
    CONF_ACTIVITY_STATE,
    CONF_API_KEY,
    CONF_DEFAULT_PLAYER,
    CONF_HOST,
    CONF_PLAYERS,
    CONF_SERVER_HOST,
    DOMAIN,
)
from .library import KaleidescapeLibrary
from .system import KaleidescapeSystem

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_HOST): TextSelector()})


class KaleidescapeVoiceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup: one address in, a discovered system out."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._host: str = ""
        self._server_host: str = ""
        self._players: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take one Kaleidescape address and enumerate the system."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            devices = []
            try:
                devices = await KaleidescapeSystem(host).async_discover()
            except Exception as err:  # noqa: BLE001 - surface anything as a form error
                _LOGGER.debug("discovery failed against %s: %s", host, err)
                errors[CONF_HOST] = "cannot_connect"

            players = [d for d in devices if d.is_player]
            if devices and not players:
                errors[CONF_HOST] = "no_players"

            if not errors:
                # The library lives on a device with no movie zones (the server).
                # Fall back to the entered address if the system is one box.
                server = next((d for d in devices if not d.is_player and d.ip), None)
                server_host = server.ip if server else host

                library = KaleidescapeLibrary(
                    async_get_clientsession(self.hass), server_host
                )
                try:
                    await library.async_refresh()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("library unreadable at %s: %s", server_host, err)
                    errors["base"] = "cannot_read_library"

                if not errors:
                    self._host = host
                    self._server_host = server_host
                    self._players = [
                        {"serial": d.serial, "name": d.display_name} for d in players
                    ]
                    await self.async_set_unique_id(
                        "_".join(sorted(p["serial"] for p in self._players))
                    )
                    self._abort_if_unique_id_configured()
                    return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found before committing."""
        if user_input is not None:
            plural = "s" if len(self._players) != 1 else ""
            return self.async_create_entry(
                title=f"Kaleidescape ({len(self._players)} player{plural})",
                data={
                    CONF_HOST: self._host,
                    CONF_SERVER_HOST: self._server_host,
                    CONF_PLAYERS: self._players,
                },
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "players": ", ".join(p["name"] for p in self._players),
                "server": self._server_host,
                "count": str(len(self._players)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return KaleidescapeVoiceOptionsFlow()


class KaleidescapeVoiceOptionsFlow(OptionsFlow):
    """Everything that isn't discovered: the Claude key and the activity gate."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the settings form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        players = self.config_entry.data.get(CONF_PLAYERS, [])

        schema: dict[Any, Any] = {
            # Claude key for descriptive requests ("the one where the president
            # fights terrorists on a plane"). Empty = fully local; naming a title
            # never calls out either way.
            vol.Optional(
                CONF_API_KEY, default=options.get(CONF_API_KEY, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(
                CONF_ACTIVITY_ENTITY,
                default=options.get(CONF_ACTIVITY_ENTITY, ""),
            ): EntitySelector(
                EntitySelectorConfig(domain=["input_select", "sensor", "select"])
            ),
            vol.Optional(
                CONF_ACTIVITY_STATE,
                default=options.get(CONF_ACTIVITY_STATE, "Watch Kaleidescape"),
            ): TextSelector(),
        }

        # With one player an unnamed command can only mean that player. With
        # several, guessing is a movie in the wrong room -- so the default is
        # chosen here explicitly, and commands that don't name a player are
        # refused when it is left blank.
        if len(players) > 1:
            schema[
                vol.Optional(
                    CONF_DEFAULT_PLAYER,
                    default=options.get(CONF_DEFAULT_PLAYER, ""),
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[p["name"] for p in players],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
