"""Self-test for the Claude descriptive-query resolver (no playback).

Reads the key from ANTHROPIC_API_KEY -- never hardcode it here; this file is
git-tracked.

    docker exec -e ANTHROPIC_API_KEY=sk-ant-... homeassistant python3 \
        /config/scripts/kscape_resolver_selftest.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402
from kaleidescape_voice.resolver import ClaudeResolver  # noqa: E402

SERVER = os.environ.get("KSCAPE_SERVER", "192.0.2.11")

# Descriptive requests the local matcher structurally cannot answer.
PROBES = [
    "the one where the president fights terrorists on a plane",
    "a movie about dinosaurs",
    "something with talking toys for the kids",
    "the one with the shark",
    "a heist movie set in dreams",
    "something by the director of Jaws",
    "a james bond movie with sean connery",
    "a movie that definitely is not in this library, like The Godfather",
]


async def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("set ANTHROPIC_API_KEY")
        return 2

    async with aiohttp.ClientSession() as session:
        lib = KaleidescapeLibrary(session, SERVER)
        await lib.async_refresh()
        await lib.async_enrich()

        resolver = ClaudeResolver(key)
        catalog = resolver.build_catalog(lib.movies)
        print(f"library {len(lib)} titles; catalog {len(catalog)} chars "
              f"(~{len(catalog)//4} tokens)")

        for probe in PROBES:
            started = time.monotonic()
            found = await resolver.async_resolve(probe, lib)
            elapsed = time.monotonic() - started
            # None and [] mean different things: could not ask vs asked and the
            # library has nothing. Only the second suppresses local results.
            if found is None:
                names = "(resolver unavailable -- local search stands)"
            else:
                names = ", ".join(
                    f"{s.movie.title} ({s.movie.year}) {s.confidence:.2f}"
                    for s in found[:4]
                ) or "(nothing in this library fits)"
            print(f"\n{probe!r}\n   {elapsed:.1f}s -> {names}")

    return 0


sys.exit(asyncio.run(main()))
