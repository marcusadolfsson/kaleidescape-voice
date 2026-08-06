"""End-to-end test: spoken phrase -> fuzzy match -> playback confirmed.

Exercises the real integration modules (no HA required). Plays an actual movie,
then stops it.

    docker exec homeassistant python3 \
        /config/scripts/kscape_voice_e2e.py \
        192.0.2.10 192.0.2.11 "indiana jones crystal skull"
"""

from __future__ import annotations

import asyncio
import sys

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402
from kaleidescape_voice.player import (  # noqa: E402
    KaleidescapePlayer,
    KaleidescapeProtocolError,
)

PLAYER = sys.argv[1] if len(sys.argv) > 1 else "192.0.2.10"
SERVER = sys.argv[2] if len(sys.argv) > 2 else "192.0.2.11"
SPOKEN = sys.argv[3] if len(sys.argv) > 3 else "indiana jones crystal skull"


async def main() -> int:
    player = KaleidescapePlayer(PLAYER)

    info = await player.async_discover()
    print(f"1. player: serial={info.get('serial')} name={info.get('name')!r}")

    async with aiohttp.ClientSession() as session:
        library = KaleidescapeLibrary(session, SERVER)
        count = await library.async_refresh()
        print(f"2. library: {count} titles")

        prefix = await player.async_detect_handle_prefix(
            [m.handle for m in library.movies]
        )
        print(f"3. handle prefix detected: {prefix!r}")

        match = library.best(SPOKEN)
        if match is None:
            top = library.match(SPOKEN, limit=3)
            print(f"   AMBIGUOUS {SPOKEN!r}: {[(m.movie.title, m.score) for m in top]}")
            return 1
        print(f"4. matched {SPOKEN!r} -> {match.movie.title} (score {match.score})")
        print(f"   qualified handle: {player.qualify(match.movie.handle)}")

        try:
            title = await player.async_play_handle(match.movie.handle)
        except KaleidescapeProtocolError as err:
            print(f"5. FAIL playback not confirmed: {err}")
            return 1
        print(f"5. PLAYING (confirmed by the player): {title!r}")

        await asyncio.sleep(3)
        status = await player.async_get_play_status()
        print(f"6. status re-read: {status}")

        await player.async_transport("STOP")
        await asyncio.sleep(2)
        print(f"7. stopped: {await player.async_get_play_status()}")

    print("\nPASS")
    return 0


sys.exit(asyncio.run(main()))
