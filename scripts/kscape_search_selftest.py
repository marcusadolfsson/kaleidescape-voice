"""Self-test for enrichment + library-wide search (no playback).

    docker exec homeassistant python3 \
        /config/scripts/kscape_search_selftest.py 192.0.2.11
"""

from __future__ import annotations

import asyncio
import sys
import time

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402

SERVER = sys.argv[1] if len(sys.argv) > 1 else "192.0.2.11"

QUERIES = [
    "james bond",
    "sean connery",
    "harrison ford",
    "spider man",
    "kubrick",
    "spy",
    "star wars",
    "batman",
    "tom hanks",
]


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        lib = KaleidescapeLibrary(session, SERVER)
        print(f"titles: {await lib.async_refresh()}")

        started = time.monotonic()
        enriched = await lib.async_enrich()
        print(f"enriched {enriched} titles in {time.monotonic() - started:.1f}s")

        sample = next(m for m in lib.movies if m.actors)
        print(f"sample: {sample.title} | cast={sample.actors[:3]} | genres={sample.genres}")
        print(f"        synopsis: {sample.synopsis[:90]}...")

        print("\n--- search ---")
        for query in QUERIES:
            results = lib.search(query, limit=40)
            head = ", ".join(f"{m.movie.title}" for m in results[:5])
            print(f"\n{query!r}: {len(results)} results")
            print(f"   top: {head}")

        print("\n--- play-path guard (best() must decline these) ---")
        for query in ("james bond", "spy", "sean connery"):
            best = lib.best(query)
            print(f"   {query!r} -> {'DECLINED (search instead)' if best is None else 'WOULD PLAY ' + best.movie.title}")

        print("\n--- exact titles must still play directly ---")
        for query in ("air force one", "you only live twice", "aladdin"):
            best = lib.best(query)
            print(f"   {query!r} -> {best.movie.title if best else 'DECLINED'}")

    return 0


sys.exit(asyncio.run(main()))
