"""Raw Kaleidescape control-protocol client.

Everything non-obvious about PLAY_MOVIE is encoded here. The command is
UNDOCUMENTED -- it appears nowhere in the Rev 17 control protocol manual, whose
complete command vocabulary contains no play-by-handle command at all. It was
recovered from a packet capture of the Kaleidescape app and verified against a
Strato E on 2026-08-06.

Three quirks, each of which makes it fail *silently* (status 000, no playback):

1. It must be addressed to the player BY SERIAL with a `#` prefix.
   `01/0/PLAY_MOVIE:...` -- the normal local-device address used by every other
   command -- returns 000 and does nothing, at any zone.
2. The trailing `::` is mandatory. The message carries three fields:
   handle, `bookmark;options`, and an empty third. Drop it and you get
   `011:Invalid number of parameters`.
3. The options field is mandatory. `...:26-0.NUL::` is accepted and ignored;
   `...:26-0.NUL;158-0=1::` plays.

Because a malformed-but-accepted command is indistinguishable from a working one
by status code alone, `async_play_handle` does not trust 000 -- it waits for the
TITLE_NAME / PLAY_STATUS events the player emits on real playback.
"""

from __future__ import annotations

import asyncio
import logging
import re

from .const import (
    BOOKMARK_START,
    DEFAULT_PLAY_OPTIONS,
    DEFAULT_PORT,
    FALLBACK_HANDLE_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 8.0
_READ_TIMEOUT = 4.0
# Playback confirmation can take a few seconds on a cold title.
_PLAY_CONFIRM_TIMEOUT = 15.0
# How long to wait on the play command's own connection for the status reply.
_PLAY_REPLY_LISTEN = 1.0
# Gap between GET_PLAY_STATUS polls while waiting for playback to come up.
_PLAY_POLL_INTERVAL = 1.0


class KaleidescapeProtocolError(Exception):
    """A command was rejected by the player."""


class KaleidescapePlayer:
    """One-shot TCP client for the player's control protocol.

    Connections are deliberately not pooled. The port is documented as accepting
    up to twenty simultaneous connections, but long-lived reverse-engineered
    control sockets have a habit of wedging;
    a fresh connection per command is cheap and cannot go stale.
    """

    def __init__(
        self, host: str, serial: str | None = None, port: int = DEFAULT_PORT
    ) -> None:
        """Initialise the client.

        `host` is any component of the system -- the protocol forwards commands
        to whichever device `serial` names, so several players share one address
        (see system.py). Pass `serial` when it is already known from discovery;
        omit it and the client asks the device it connects to who it is.
        """
        self._host = host
        self._port = port
        self._serial: str | None = serial
        self._handle_prefix: str = FALLBACK_HANDLE_PREFIX
        self._lock = asyncio.Lock()

    @property
    def serial(self) -> str | None:
        """Player serial number, as reported by the device."""
        return self._serial

    @property
    def handle_prefix(self) -> str:
        """Library prefix that turns a web-UI handle into a player handle."""
        return self._handle_prefix

    def set_handle_prefix(self, prefix: str) -> None:
        """Share a prefix discovered by another client.

        The prefix identifies the LIBRARY, not the player, so several players in
        one system share it -- detecting it once and copying it avoids N probes
        that would all return the same answer.
        """
        if prefix:
            self._handle_prefix = prefix

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _converse(self, lines: list[str], listen: float = 0.0) -> list[str]:
        """Send lines on one connection and return every line received."""
        received: list[str] = []
        async with self._lock:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), _CONNECT_TIMEOUT
            )
            try:
                # Drain anything already queued (banner / async events) so it
                # cannot be mistaken for a reply to our command.
                await self._drain(reader, received, 0.4)

                for line in lines:
                    writer.write((line + "\r\n").encode())
                    await writer.drain()
                    _LOGGER.debug(">>> %s", line)
                    await self._drain(reader, received, 1.0)

                if listen:
                    await self._drain(reader, received, listen)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 - closing must never raise
                    pass
        return received

    @staticmethod
    async def _drain(reader, sink: list[str], seconds: float) -> None:
        """Collect lines for `seconds`, tolerating idle gaps."""
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
                line = raw.strip()
                if line:
                    _LOGGER.debug("<<< %s", line)
                    sink.append(line)

    # ------------------------------------------------------------------
    # addressing
    # ------------------------------------------------------------------

    def _address(self, seq: int = 1) -> str:
        """Return the message prefix for this player.

        PLAY_MOVIE only works when addressed by serial. The device echoes the
        serial zero-padded, but expects it *without* the leading zero.
        """
        if not self._serial:
            raise KaleidescapeProtocolError("player serial not discovered yet")
        return f"#{self._serial.lstrip('0')}/{seq}/"

    async def async_discover(self) -> dict[str, str]:
        """Learn this device's serial and friendly name; validates the host.

        Skipped when the serial came from discovery -- there is nothing to learn
        and it would describe the device we connected *through*, not the target.
        """
        # Deliberately local-addressed (`01/`): this asks "who are you?" of the
        # device we connected to. Serial-addressing it would be circular.
        lines = await self._converse(["01/1/GET_DEVICE_INFO:"])
        info: dict[str, str] = {}
        for line in lines:
            # DEVICE_INFO:<type>:<serial>:<cpdid>:<ip>:
            match = re.search(
                r"DEVICE_INFO:(\d+):([0-9A-Fa-f]+):(\d*):([\d.]*):", line
            )
            if match:
                info["device_type"] = match.group(1)
                info["serial"] = match.group(2)
                info["cpdid"] = match.group(3)
                # The device zero-pads each octet (010.010.010.182).
                info["ip"] = ".".join(
                    str(int(part)) for part in match.group(4).split(".") if part
                )
                self._serial = match.group(2)
                break
        if not self._serial:
            raise KaleidescapeProtocolError(
                f"no DEVICE_INFO from {self._host}:{self._port}"
            )

        for line in await self._converse(["01/1/GET_FRIENDLY_SYSTEM_NAME:"]):
            match = re.search(r"FRIENDLY_SYSTEM_NAME:([^:]*):", line)
            if match and match.group(1):
                info["name"] = match.group(1)
        return info

    async def async_detect_handle_prefix(self, bare_handles: list[str]) -> str:
        """Find the prefix that turns a scraped handle into a player handle.

        The web UI hands out `0-S_c449dd9b` while the player wants
        `26-0.0-S_c449dd9b`. The `26-0.` part is a library id, not the KOS
        version, so it is discovered rather than assumed: first from whatever is
        highlighted on screen, then by probing candidates against a real handle
        until GET_CONTENT_DETAILS stops saying "Invalid content handle".
        """
        for line in await self._converse(
            [f"{self._address()}GET_HIGHLIGHTED_SELECTION:"]
        ):
            match = re.search(r"HIGHLIGHTED_SELECTION:([^:]*\.)0-", line)
            if match:
                self._handle_prefix = match.group(1)
                _LOGGER.debug("handle prefix from highlight: %s", self._handle_prefix)
                return self._handle_prefix

        if not bare_handles:
            return self._handle_prefix

        probe = bare_handles[0]
        for candidate in (FALLBACK_HANDLE_PREFIX, "0-", ""):
            lines = await self._converse(
                [f"{self._address()}GET_CONTENT_DETAILS:1:{candidate}{probe}::"]
            )
            if any("CONTENT_DETAILS" in line for line in lines):
                self._handle_prefix = candidate
                _LOGGER.debug("handle prefix probed: %r", candidate)
                return candidate

        _LOGGER.warning(
            "could not detect handle prefix; falling back to %r", FALLBACK_HANDLE_PREFIX
        )
        return self._handle_prefix

    def qualify(self, handle: str) -> str:
        """Return a player-qualified handle for a scraped (bare) handle."""
        if "." in handle:
            return handle
        return f"{self._handle_prefix}{handle}"

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @staticmethod
    def _error_in(lines: list[str]) -> tuple[str, str] | None:
        """Return (status, message) for the first non-000 reply, if any."""
        for line in lines:
            # Replies to our commands echo the serial: #070300002028/1/020:msg:/65
            match = re.match(r"#\d+/\d+/(\d{3}):([^/]*)", line)
            if match and match.group(1) != "000":
                return match.group(1), match.group(2).rstrip(":").strip()
        return None

    async def async_is_awake(self) -> bool:
        """True if the player is out of standby."""
        lines = await self._converse([f"{self._address()}GET_DEVICE_POWER_STATE:"])
        for line in lines:
            if match := re.search(r"DEVICE_POWER_STATE:(\d):", line):
                return match.group(1) == "1"
        return False

    async def async_wake(self) -> None:
        """Bring the player out of standby (near-instant on a Strato)."""
        await self._converse([f"{self._address()}LEAVE_STANDBY:"], listen=1.0)

    async def async_play_handle(
        self,
        handle: str,
        *,
        bookmark: str | None = None,
        options: str = DEFAULT_PLAY_OPTIONS,
        wake: bool = True,
    ) -> str:
        """Start a movie by handle. Returns the confirmed title.

        Wakes the player first if it is asleep -- a voice request routinely
        arrives when the theater is idle, and the player answers
        `020 Device is in standby` and does nothing.

        Raises KaleidescapeProtocolError if the player never confirms playback:
        a 000 status alone is NOT evidence the command did anything.
        """
        qualified = self.qualify(handle)
        mark = f"{self._handle_prefix}{bookmark or BOOKMARK_START}"
        if options:
            mark = f"{mark};{options}"

        # The trailing empty third field (the `::`) is required.
        command = f"{self._address()}PLAY_MOVIE:{qualified}:{mark}::"
        # Short listen: just long enough for the status reply (a 020 standby
        # rejection has to be seen here). Confirmation is polled for below, so
        # there is nothing to gain by holding the connection open longer.
        lines = await self._converse([command], listen=_PLAY_REPLY_LISTEN)

        if (error := self._error_in(lines)) and error[0] == "020" and wake:
            _LOGGER.debug("player was in standby; waking and retrying")
            await self.async_wake()
            await asyncio.sleep(2)
            return await self.async_play_handle(
                handle, bookmark=bookmark, options=options, wake=False
            )

        if error := self._error_in(lines):
            raise KaleidescapeProtocolError(
                f"player rejected the command ({error[0]}): {error[1]}"
            )

        # Fast path: the player volunteered a status line.
        title, playing = self._playback_in(lines)
        if title or playing:
            return title

        # Otherwise ASK. Confirmation used to rely purely on unsolicited
        # TITLE_NAME/PLAY_STATUS events, which this connection never receives --
        # the player only pushes those after ENABLE_EVENTS, which we don't send.
        # So a perfectly good play sat silent for the whole listen window and
        # was then reported to the user as a failure while the movie played.
        # Polling is both correct and quicker: a real start confirms in ~1-2 s.
        deadline = asyncio.get_running_loop().time() + _PLAY_CONFIRM_TIMEOUT
        while True:
            status = await self.async_get_play_status()
            if status.get("mode") in ("playing", "paused") or status.get("title"):
                return status.get("title", "")
            if asyncio.get_running_loop().time() >= deadline:
                raise KaleidescapeProtocolError(
                    "command was accepted but playback never started "
                    "(the player returns 000 for commands it ignores)"
                )
            await asyncio.sleep(_PLAY_POLL_INTERVAL)

    @staticmethod
    def _unescape(value: str) -> str:
        r"""Undo the protocol's field escaping.

        `:` delimits fields, so a colon inside a value arrives backslashed --
        which surfaced as the spoken/displayed title
        "Home Alone 2\: Lost in New York".
        """
        return value.replace("\\:", ":").replace("\\\\", "\\")

    @staticmethod
    def _playback_in(lines: list[str]) -> tuple[str, bool]:
        """Return (title, playing) for any status lines present."""
        title, playing = "", False
        for line in lines:
            if match := re.search(r"(?:PLAYING_)?TITLE_NAME:(.+):/", line):
                title = KaleidescapePlayer._unescape(match.group(1))
            if re.search(r"PLAY_STATUS:[12]:", line):
                playing = True
        return title, playing

    async def async_send_raw(self, command: str) -> list[str]:
        """Send an arbitrary protocol command, addressed by serial."""
        if not command.endswith(":"):
            command += ":"
        return await self._converse([f"{self._address()}{command}"], listen=1.5)

    async def async_transport(self, command: str) -> None:
        """Send a zero-argument transport command (PLAY, PAUSE, STOP, ...)."""
        await self._converse([f"{self._address()}{command}:"])

    async def async_get_play_status(self) -> dict[str, str]:
        """Return what the player is doing right now."""
        status: dict[str, str] = {}
        lines = await self._converse(
            [
                f"{self._address(1)}GET_PLAY_STATUS:",
                f"{self._address(2)}GET_PLAYING_TITLE_NAME:",
            ]
        )
        for line in lines:
            match = re.search(r"PLAY_STATUS:(\d):", line)
            if match:
                status["mode"] = {
                    "0": "idle",
                    "1": "paused",
                    "2": "playing",
                }.get(match.group(1), match.group(1))
            match = re.search(r"(?:PLAYING_)?TITLE_NAME:(.+):/", line)
            if match:
                status["title"] = self._unescape(match.group(1))
        return status
