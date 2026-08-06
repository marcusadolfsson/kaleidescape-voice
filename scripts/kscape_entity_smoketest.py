"""Read every entity property against a stubbed runtime.

`python -m py_compile` and importing the modules both passed while
`sensor.py` still referenced `runtime.player`, which the multi-player refactor
had removed -- because neither one ever *evaluates* an attribute. The failure
surfaced only when Home Assistant added the entity and read its properties, as
`AttributeError: 'KaleidescapeVoiceRuntime' object has no attribute 'player'`,
and cost the library sensor entirely.

So: build a runtime with stub players and touch every property the entities
expose. Cheap, no hardware, no HA, and it catches exactly the class of mistake a
refactor leaves behind.

    docker exec homeassistant python3 \
        /config/scripts/kscape_entity_smoketest.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

# Run inside the Home Assistant container, where the integration lives.
sys.path.insert(0, "/config/custom_components")

from kaleidescape_voice import KaleidescapeVoiceRuntime  # noqa: E402
from kaleidescape_voice.media_player import (  # noqa: E402
    KaleidescapeSearchPicker,
)
from kaleidescape_voice.player import KaleidescapePlayer  # noqa: E402
from kaleidescape_voice.resolver import ClaudeResolver  # noqa: E402
from kaleidescape_voice.sensor import (  # noqa: E402
    KaleidescapeLibrarySensor,
    KaleidescapeSearchResultsSensor,
)

FAKE_MOVIE = SimpleNamespace(
    handle="0-S_abc", title="A Film", year="1999", genre="Drama",
    director="Someone", rating="PG", running_time="100",
    as_result=lambda: {"handle": "0-S_abc", "title": "A Film", "year": "1999"},
)


def build_runtime(player_count: int):
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _e: None),
        bus=SimpleNamespace(async_fire=lambda *a, **k: None),
        data={},
        config=SimpleNamespace(config_dir="/config"),
    )
    players, names = {}, {}
    for i in range(player_count):
        serial = f"0703000020{i:02d}"
        players[serial] = KaleidescapePlayer("192.0.2.11", serial=serial)
        names[serial] = f"Room {i + 1}"
    # A real object: len() is looked up on the TYPE, so a SimpleNamespace
    # attribute called __len__ is never used.
    class _Library:
        movies: list = []

        def __len__(self) -> int:
            return 0

    library = _Library()
    return KaleidescapeVoiceRuntime(
        hass, players, names, library, ClaudeResolver("")
    )


def main() -> int:
    entry = SimpleNamespace(
        entry_id="abc123",
        data={"host": "192.0.2.11", "server_host": "192.0.2.11"},
        options={},
    )

    failures = 0
    for count in (1, 2):
        runtime = build_runtime(count)
        runtime.last_results = [FAKE_MOVIE]
        runtime.last_query = "a film"

        entities = [
            ("library sensor", KaleidescapeLibrarySensor(runtime, entry)),
            ("results sensor", KaleidescapeSearchResultsSensor(runtime, entry)),
            ("search picker", KaleidescapeSearchPicker(runtime, entry)),
        ]
        for label, entity in entities:
            for prop in (
                "native_value", "extra_state_attributes", "state",
                "media_title", "source_list", "source", "name", "icon",
            ):
                if not hasattr(type(entity), prop):
                    continue
                try:
                    getattr(entity, prop)
                except Exception as err:  # noqa: BLE001
                    failures += 1
                    print(f"  FAIL [{count} player] {label}.{prop}: "
                          f"{type(err).__name__}: {err}")
        print(f"  ok   [{count} player] all entity properties read")

    # Targeting: the paths a voice command actually takes.
    one = build_runtime(1)
    serial, _ = one.resolve_player()
    print(f"  ok   1 player, no target -> {one.names[serial]}")

    two = build_runtime(2)
    try:
        two.resolve_player()
        print("  FAIL 2 players with no target should refuse, not guess")
        failures += 1
    except Exception as err:  # noqa: BLE001
        print(f"  ok   2 players, no target -> refuses: {str(err)[:60]}…")
    serial, _ = two.resolve_player("Room 2")
    print(f"  ok   2 players, named       -> {two.names[serial]}")

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


sys.exit(main())
