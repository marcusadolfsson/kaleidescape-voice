"""Does the LLM actually get a turn?

The resolver working standalone proves nothing about production: async_search
runs local search FIRST and only consults Claude when the local result set looks
thin. If local search returns a handful of weak matches for a descriptive query,
the gate never opens and the resolver is dead code.

This prints, per probe, what local search returned and whether the gate would
open — no API calls, so it is free to run.

    docker exec homeassistant python3 \
        /config/scripts/kscape_gate_selftest.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

import aiohttp  # noqa: E402

from kaleidescape_voice.library import KaleidescapeLibrary  # noqa: E402

SERVER = os.environ.get("KSCAPE_SERVER", "192.0.2.11")

# Descriptive requests: the LLM MUST get a turn on these.
DESCRIPTIVE = [
    "the one where the president fights terrorists on a plane",
    "a movie about dinosaurs",
    "something with talking toys for the kids",
    "a heist movie set in dreams",
    "something by the director of Jaws",
    "a pixar movie",
    "something funny for the kids under two hours",
]

# Metadata requests local search answers well: the LLM should NOT be needed.
LOCAL = ["james bond", "sean connery", "tom hanks", "star wars", "kubrick"]


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        lib = KaleidescapeLibrary(session, SERVER)
        await lib.async_refresh()
        await lib.async_enrich()

        print("--- descriptive: gate SHOULD open (local search can't answer) ---")
        missed = 0
        for probe in DESCRIPTIVE:
            results = lib.search(probe, limit=40)
            opens = len(results) < 2
            top = ", ".join(f"{m.movie.title}({m.score})" for m in results[:3]) or "-"
            flag = "gate opens" if opens else "GATE SHUT"
            if not opens:
                missed += 1
            print(f"  {flag:10s} n={len(results):<3} {probe!r}\n              local: {top}")

        print("\n--- metadata: local search should answer alone ---")
        for probe in LOCAL:
            results = lib.search(probe, limit=40)
            opens = len(results) < 2
            print(f"  {'(llm too)' if opens else 'local only':11s} n={len(results):<3} {probe!r}")

        print(f"\n{missed} descriptive probe(s) would never reach the LLM")
        return 1 if missed else 0


sys.exit(asyncio.run(main()))
