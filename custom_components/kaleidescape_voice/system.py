"""Discover the players in a Kaleidescape system.

The setup dialog used to ask for the player's IP *and* the server's, which is
the easy thing to get wrong (they are different machines with different roles)
and does not extend to a second player at all.

It turns out not to be necessary. The control protocol routes by device: connect
to any component's port 10000 and address a command to another device with
`#<serial>/`, and it is forwarded. Verified against a Terra + Strato E — asking
the *server* for the *player's* DEVICE_INFO returns the player's own IP.

So one address is enough. `GET_AVAILABLE_DEVICES_BY_SERIAL_NUMBER` lists every
device in the system, and each one answers:

    GET_DEVICE_TYPE_NAME   "Strato E" / "Terra Movie Server"
    GET_FRIENDLY_NAME      "Living Room"   (players only; servers reject it)
    GET_NUM_ZONES          movie:music     (a player has movie zones, a server 0)

A **player is a device with at least one movie zone.** That is the distinction
that matters -- not the model name, which changes with every product generation.

Its friendly name is whatever the owner already set in the Kaleidescape app, so
voice targeting ("play it in the living room") uses a name they chose, without
asking them to invent one during setup.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 8.0


@dataclass
class KaleidescapeDevice:
    """One component of a Kaleidescape system."""

    serial: str
    device_type: str = ""
    name: str = ""
    ip: str = ""
    movie_zones: int = 0

    @property
    def is_player(self) -> bool:
        """True if this device can play movies."""
        return self.movie_zones > 0

    @property
    def display_name(self) -> str:
        """Name to show and to target by voice."""
        return self.name or self.device_type or self.serial


class KaleidescapeSystem:
    """Talks to one address and enumerates everything behind it."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        """Initialise."""
        self._host = host
        self._port = port

    async def _converse(self, lines: list[str]) -> list[str]:
        """Send lines on one connection, collect every reply."""
        received: list[str] = []
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), _CONNECT_TIMEOUT
        )
        try:
            await self._drain(reader, received, 0.4)
            for line in lines:
                writer.write((line + "\r\n").encode())
                await writer.drain()
                await self._drain(reader, received, 1.2)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        return received

    @staticmethod
    async def _drain(reader, sink: list[str], seconds: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                return
            try:
                chunk = await asyncio.wait_for(reader.read(65535), remaining)
            except TimeoutError:
                return
            if not chunk:
                return
            for raw in chunk.decode(errors="replace").splitlines():
                if raw.strip():
                    sink.append(raw.strip())

    @staticmethod
    def _field(lines: list[str], name: str, serial: str = "") -> str:
        """Pull the first field of a named response, optionally for one device."""
        for line in lines:
            if f":{name}:" not in line:
                continue
            if serial and not line.startswith(f"#{serial.lstrip('0')}") and \
                    not re.match(rf"#0*{serial.lstrip('0')}/", line):
                continue
            match = re.search(rf":{name}:([^/]*)", line)
            if match:
                return match.group(1).rstrip(":")
        return ""

    async def async_discover(self) -> list[KaleidescapeDevice]:
        """Return every device in the system, players flagged."""
        lines = await self._converse(["01/1/GET_AVAILABLE_DEVICES_BY_SERIAL_NUMBER:"])
        raw = self._field(lines, "AVAILABLE_DEVICES_BY_SERIAL_NUMBER")
        serials = [s for s in raw.split(":") if s.strip()]
        if not serials:
            raise ValueError(
                f"{self._host} did not list any Kaleidescape devices "
                "(is this a Kaleidescape component?)"
            )

        devices: list[KaleidescapeDevice] = []
        for serial in serials:
            addr = f"#{serial.lstrip('0')}"
            replies = await self._converse(
                [
                    f"{addr}/1/GET_DEVICE_TYPE_NAME:",
                    f"{addr}/2/GET_NUM_ZONES:",
                    # Servers reject this one; an empty name is expected there.
                    f"{addr}/3/GET_FRIENDLY_NAME:",
                    f"{addr}/4/GET_DEVICE_INFO:",
                ]
            )
            zones = self._field(replies, "NUM_ZONES")
            movie_zones = 0
            if zones:
                try:
                    movie_zones = int(zones.split(":")[0])
                except ValueError:
                    movie_zones = 0

            ip = ""
            info = self._field(replies, "DEVICE_INFO")
            if info:
                parts = info.split(":")
                if len(parts) >= 4:
                    # The device zero-pads each octet (010.010.010.182).
                    ip = ".".join(
                        str(int(p)) for p in parts[3].split(".") if p.isdigit()
                    )

            devices.append(
                KaleidescapeDevice(
                    serial=serial,
                    device_type=self._field(replies, "DEVICE_TYPE_NAME"),
                    name=self._field(replies, "FRIENDLY_NAME"),
                    ip=ip,
                    movie_zones=movie_zones,
                )
            )

        _LOGGER.debug(
            "discovered %d device(s): %s",
            len(devices),
            [(d.serial, d.device_type, d.display_name, d.movie_zones) for d in devices],
        )
        return devices

    async def async_players(self) -> list[KaleidescapeDevice]:
        """Just the devices that can play a movie."""
        return [d for d in await self.async_discover() if d.is_player]
