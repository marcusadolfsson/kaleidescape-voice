"""Offline self-test for the Kaleidescape Voice library parser and matcher.

Run inside the HA container (it has aiohttp):

    docker exec homeassistant python3 \
        /config/scripts/kscape_library_selftest.py <server-ip>
"""

from __future__ import annotations

import asyncio
import sys

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402

SERVER = sys.argv[1] if len(sys.argv) > 1 else "192.0.2.11"

# Phrases as speech-to-text would plausibly deliver them.
PROBES = [
    ("air force one", "Air Force One"),
    ("play air force one", "Air Force One"),
    ("2001 a space odyssey", "2001: A Space Odyssey"),
    ("two thousand one a space odyssey", "2001: A Space Odyssey"),
    ("indiana jones crystal skull", "Indiana Jones and the Kingdom of the Crystal Skull"),
    ("the amazing spider man", "The Amazing Spider-Man"),
    ("amazing spiderman 2", "The Amazing Spider-Man 2"),
    ("aladdin", "Aladdin"),
    ("zero dark thirty", "Zero Dark Thirty"),
    ("bourne ultimatum", "The Bourne Ultimatum"),
    ("you only live twice", "You Only Live Twice"),
    ("the world is not enough", "The World Is Not Enough"),
]


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        lib = KaleidescapeLibrary(session, SERVER)
        count = await lib.async_refresh()
        print(f"parsed {count} titles")
        sample = lib.movies[0]
        print(f"sample: {sample.handle} | {sample.describe()}")

        failures = 0
        print("\n--- matching ---")
        for spoken, expected in PROBES:
            best = lib.best(spoken)
            got = best.movie.title if best else None
            ok = got == expected
            if not ok:
                failures += 1
                top = lib.match(spoken, limit=3)
                detail = ", ".join(f"{m.movie.title}={m.score}" for m in top)
                print(f"  FAIL {spoken!r} -> {got!r} (want {expected!r}) [{detail}]")
            else:
                print(f"  ok   {spoken!r} -> {got} ({best.score})")

        print("\n--- ambiguity guard (should decline) ---")
        for spoken in ("spider man", "james bond", "play a movie"):
            best = lib.best(spoken)
            top = lib.match(spoken, limit=3)
            names = ", ".join(f"{m.movie.title}={m.score}" for m in top)
            print(f"  {spoken!r} -> {'DECLINED' if best is None else best.movie.title} [{names}]")

        print("\n--- filters ---")
        print(f"  Sci-Fi: {len(lib.filter(genre='Sci-Fi'))}")
        print(f"  Kubrick: {[m.title for m in lib.filter(director='Kubrick')]}")
        print(f"  rated G: {len(lib.filter(rating='G'))}")

        print(f"\n{failures} failure(s)")
        return 1 if failures else 0


sys.exit(asyncio.run(main()))
