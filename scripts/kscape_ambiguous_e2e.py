"""End-to-end test of the low-confidence path: search instead of guessing.

Proves that a vague request does NOT play anything, returns a usable result
list, and that picking a number from that list plays the right film.

    docker exec homeassistant python3 \
        /config/scripts/kscape_ambiguous_e2e.py
"""

from __future__ import annotations

import asyncio
import sys

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402
from kaleidescape_voice.player import KaleidescapePlayer  # noqa: E402

PLAYER = "192.0.2.10"
SERVER = "192.0.2.11"

# Phrases that must NEVER auto-play.
MUST_SEARCH = ["james bond", "spy", "sean connery", "star wars", "spider man 2 or 3"]
# Phrases that must still play straight away.
MUST_PLAY = ["air force one", "aladdin", "indiana jones crystal skull"]


async def main() -> int:
    player = KaleidescapePlayer(PLAYER)
    await player.async_discover()

    async with aiohttp.ClientSession() as session:
        lib = KaleidescapeLibrary(session, SERVER)
        await lib.async_refresh()
        await lib.async_enrich()
        await player.async_detect_handle_prefix([m.handle for m in lib.movies])

        failures = 0

        print("--- must SEARCH (never auto-play) ---")
        for phrase in MUST_SEARCH:
            best = lib.best(phrase)
            results = lib.search(phrase, limit=40)
            if best is not None:
                failures += 1
                print(f"  FAIL {phrase!r} would auto-play {best.movie.title!r}")
            else:
                names = ", ".join(f"{i}. {m.movie.title}" for i, m in enumerate(results[:5], 1))
                print(f"  ok   {phrase!r} -> {len(results)} results: {names}")

        print("\n--- must PLAY directly ---")
        for phrase in MUST_PLAY:
            best = lib.best(phrase)
            if best is None:
                failures += 1
                print(f"  FAIL {phrase!r} did not resolve")
            else:
                print(f"  ok   {phrase!r} -> {best.movie.title} ({best.score})")

        # The real interaction: vague ask -> list -> pick a number -> plays.
        print("\n--- follow-up: 'james bond' then 'play number 3' ---")
        results = [m.movie for m in lib.search("james bond", limit=40)]
        for i, movie in enumerate(results[:5], 1):
            print(f"   {i}. {movie.title} ({movie.year})")

        pick = results[2]
        print(f"   picking number 3 -> {pick.title}")
        title = await player.async_play_handle(pick.handle)
        print(f"   PLAYING: {title!r}")
        if pick.title not in title and title not in pick.title:
            failures += 1
            print(f"   FAIL expected {pick.title!r}")

        await asyncio.sleep(2)
        await player.async_transport("STOP")
        await asyncio.sleep(2)
        print(f"   stopped: {await player.async_get_play_status()}")

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
