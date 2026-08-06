# Kaleidescape Voice

Say a movie name, the theater plays it.

```
"Play Air Force One"
"Play Indiana Jones Crystal Skull"
"Surprise me with a sci-fi movie"
"What movies do I have by Kubrick?"
```

## How it works

Two halves, because the Kaleidescape splits the job across two machines:

| | Machine | Role here |
|---|---|---|
| **Player** (Strato/Alto) | TCP **10000** | Takes playback commands. Serves no useful HTTP. |
| **Server** (Terra) | HTTP **80** | Hosts the library. Rejects playback commands. |

The control protocol **cannot enumerate the library** — `GET_CONTENT_LIST`,
`GET_MOVIE_LIST`, `GET_LIBRARY_SIZE` all return "Invalid request". So the
library is scraped from the server's own web UI (`/movies?collection=All`) and
cached, then a spoken phrase is fuzzy-matched against it and the resulting
handle is played on the player.

Matching is local and instant — no LLM is involved in playing a named movie.
It is a lookup against a ~200-row table; a sentence match resolves it in
milliseconds, offline. See "Where an LLM actually helps" below.

## Install

**HACS** → three-dot menu → *Custom repositories* → add this repo as an
**Integration** → install → restart Home Assistant.

**Manually**: copy `custom_components/kaleidescape_voice/` into your
`config/custom_components/` and restart.

For the spoken phrases, also copy `custom_sentences/en/kaleidescape_voice.yaml`
into `config/custom_sentences/en/`. The integration works without it — the
services and the `voice_request` entry point do not need sentence templates —
but Assist will not understand "play Aladdin" until it is in place.

## Setup

Settings → Devices & Services → **Add Integration** → *Kaleidescape Voice*.

Enter the **player IP** and the **movie server IP**. Both are validated for real
before the entry is created: the player must answer `GET_DEVICE_INFO` on port
10000 and the server must return a parseable library. Swapping the two fields is
the easy mistake and it produces a system that looks configured and cannot play
anything, so the flow refuses it.

### Set the activity gate

`play {movie}` is a **bare wildcard** — it matches any "play …". Without a gate,
"play some jazz" gets looked up as a movie title. Set the options
**activity entity** (e.g. `input_select.av_activity`) and **activity state**
(`Watch Kaleidescape`) and every action intent declines unless the Kaleidescape
is the active source:

> "The Kaleidescape isn't the active source (it's Watch Apple TV).
> Switch to Watch Kaleidescape first."

It fails safe: if the entity is missing or unavailable, the gate is **shut**.
Leaving the entity unset disables gating entirely — fine for a single-purpose
install, wrong for a house with other media players.

## Voice commands

Handled by HA's **built-in** conversation agent via
`custom_sentences/en/kaleidescape_voice.yaml`. Fully local.

**Play** — `play …`, `put on …`, `put … on`, `start …`, `watch …`,
`let's watch …`, `I want to watch …`, `kaleidescape play …`

The title is a wildcard passed to the fuzzy matcher, so it tolerates how speech
recognition actually behaves: dropped articles (`bourne ultimatum`), spelled-out
numerals (`two thousand one a space odyssey`), missing subtitles
(`indiana jones crystal skull`), and joined words (`amazing spiderman 2`).

### Nothing plays unless one title matches confidently

If a phrase doesn't resolve to exactly one title, **nothing plays** — the
library is searched and the candidates come back instead:

```
"play james bond"  ->  "I found 27 matching james bond. The first 5 are:
                        1. A View to a Kill, 2. Die Another Day,
                        3. From Russia with Love, 4. Licence to Kill,
                        5. No Time to Die. Say a number to play one."
"play number 3"    ->  "Playing From Russia with Love."
```

This is the whole point of the confidence rule. "james bond" is not a title, it
matches 27 films, and picking one is a coin flip the user has to undo. Same for
`spy` (30 results) and `sean connery` (10).

Search covers **title, cast, director, genres and synopsis** — which is why
"james bond" works at all: no title contains it, but the Bond synopses do
("*Bond* and his Japanese counterparts…").

### Results go to a screen, not a speaker

Reading seventeen Bond titles aloud is not a usable answer, and a theater often
has no speaker free to say them anyway. The reply channel is whatever you are
already holding.

Results are published three ways:

| Surface | Use |
|---|---|
| `media_player.kaleidescape_voice_search` | A `source_list` of numbered results; selecting one plays it. Any remote or dashboard card that renders a live source list shows them with no extra work |
| `sensor.*_search_results` | Numbered results with handles, for HA dashboards |
| `kaleidescape_voice_search_results` event | For automations |

A `media_player` looks like an odd shape for a picker, and it is deliberate.
A results list is not known when the dashboard is written, so anything that
takes its options from layout config cannot show it. `source_list` is the one
widely-supported shape that is *live*, so results render on hardware whose
firmware you cannot change — which is a far bigger lift than adding an entity.

An on-screen display on the projector was tried first and rejected: it was modal,
had no usable line break, and vanished whenever the video path bypassed the
device drawing it. Feedback that disappears during a source change is worse than
no feedback.

Two ranking rules earn their keep:

- **No edit-distance on titles during search.** It made "james bond" rank
  *Jason Bourne* first, "kubrick" pull in *Jurassic Park*, and "batman" pull in
  *Ant-Man*.
- **Partial-title confidence is proportional to coverage.** A flat bonus meant
  "spy" scored 0.94 against *The Spy Who Loved Me* and played it. Now "spy"
  searches, while "indiana jones crystal skull" — which covers most of its
  title — still plays directly.

**Transport** — `pause/resume/stop the movie`, `next`, `previous`, `replay`,
`fast forward`, `rewind`, `details`, `menu`, `movie covers`, `movie list`,
`change subtitles`, `change audio track`.

Note: there is deliberately **no bare `pause` sentence** — that word has to keep
meaning "pause whatever HA is playing" for every other media player in the
house. Route bare transport words with the activity gate instead.

**Search** — `search for …`, `find …`, `look for …`, `do I have …`,
`show me … movies`, then `play number 3` / `play the second one`

**Ask** — `what's playing`, `what movie is this`, `what are we watching`

**Browse** — `what sci-fi movies do I have`, `what movies do I have by <director>`,
`what movies do I have with <actor>`, `what PG-13 movies do I have`,
`what movies do I have from <year>`

**Random** — `surprise me`, `play something`, `pick a movie`,
`play a random action movie`

## Services

| Service | Purpose |
|---|---|
| `kaleidescape_voice.play_movie` | `title:` (fuzzy) or `handle:` (exact) |
| `kaleidescape_voice.search` | `query:` — returns matches; what a vague play falls back to |
| `kaleidescape_voice.play_result` | `index:` — play the Nth result of the last search |
| `kaleidescape_voice.refresh_library` | Re-scrape + enrich now (also runs every 12 h) |
| `kaleidescape_voice.send_command` | Raw protocol command, for diagnostics |

## Descriptive requests (Claude Haiku 4.5)

Naming a title never calls out. But local search only matches literal strings in
the scraped metadata, so it structurally cannot answer:

| Request | Resolved to |
|---|---|
| "the one where the president fights terrorists on a plane" | Air Force One |
| "a heist movie set in dreams" | Inception |
| "something with talking toys for the kids" | Toy Story 1–4 |
| "something by the director of Jaws" | E.T., Indiana Jones ×3 |
| "…like The Godfather" (not owned) | *nothing* — correctly declines |

Set a **Claude API key** in the integration's options to enable this. Leave it
empty and everything stays local.

**Grounding, not fine-tuning.** The catalog goes in the prompt (~12.7k tokens),
not into a trained model — 202 titles is far too small to train on, it would go
stale the moment you buy a movie, and it can't be updated incrementally.

**Model: `claude-haiku-4-5`** ($1/$5 per MTok) — the cheapest, and the right
tier: this is a constrained lookup over a list you supply, not reasoning.
About **$0.013 per query**, 1–6s, and only vague requests reach it at all.

Two Haiku-4.5 constraints are load-bearing and easy to trip:

- **`output_config.effort` is rejected on Haiku 4.5** — it's an Opus/Sonnet
  parameter. Adding it "for quality" 400s the request.
- **The prompt-cache minimum is 4096 tokens on Haiku 4.5** (higher than newer
  models). A title-only catalog lands near it and would silently never cache;
  including synopses both makes descriptive queries work *and* clears the floor.

### Two rules that make it safe

1. **The model returns handles, never titles.** A hallucinated title is
   plausible and hard to spot; a hallucinated handle just isn't in the library
   and is dropped (and logged).
2. **It can never auto-play.** Only a confident *local* title match plays
   directly. Resolver output is always a list you tap, so the worst case is a
   suggestion you ignore.

That second rule matters because Haiku sometimes stretches: "the one with the
shark" returns *Jurassic Park* when *Jaws* isn't in the library, rather than
declining. Since nothing plays without a tap, that costs a glance.

## The protocol, and why the code looks paranoid

`PLAY_MOVIE` is **undocumented** — it appears nowhere in the Rev 17 control
protocol manual, whose full 388-token command vocabulary contains no
play-by-handle command at all. Recovered from a packet capture and verified on a
Strato E:

```
#<serial-without-leading-zero>/<seq>/PLAY_MOVIE:<handle>:<bookmark>[;<opt>=<val>…]::
#70300002028/1/PLAY_MOVIE:26-0.0-S_c449dd9b:26-0.NUL;158-0=1::
```

Three quirks, each of which makes it fail **silently** (status `000`, no playback):

1. It must be addressed **by serial** with `#`. The ordinary `01/0/` local
   address that every other command uses returns `000` and does nothing.
2. The trailing `::` is **mandatory** (three fields — handle,
   `bookmark;options`, empty). Without it: `011 Invalid number of parameters`.
3. The options field is **mandatory**. `…:26-0.NUL::` is accepted and ignored;
   `…:26-0.NUL;158-0=1::` plays.

`26-0.NUL` starts from the beginning. The `26-0.` prefix is a library id (the
web UI hands out bare `0-S_…` handles) and is **discovered at runtime**, not
hardcoded.

Because this device returns `000` for commands it ignores, **success codes are
never trusted**: `async_play_handle` waits for the `TITLE_NAME` / `PLAY_STATUS`
events that only real playback produces, and raises otherwise.

A voice request often arrives while the theater is idle, and the player then
answers `020 Device is in standby` and does nothing — so `async_play_handle`
detects that, sends `LEAVE_STANDBY` (near-instant on a Strato) and retries once.

Full notes, including the three quirks that make it fail silently:
**[docs/play-by-name.md](docs/play-by-name.md)**.

Kaleidescape's own Control Protocol Reference Manual is *not* redistributed here
— ask Kaleidescape for it. PLAY_MOVIE is not in it anyway.

## Testing

```bash
# library parsing + spoken-title matching, no playback
docker exec homeassistant python3 \
  /config/scripts/kscape_library_selftest.py 192.0.2.11

# enrichment + library-wide search ranking, no playback
docker exec homeassistant python3 \
  /config/scripts/kscape_search_selftest.py 192.0.2.11

# the low-confidence path: vague ask -> list -> "play number 3" (plays a movie)
docker exec homeassistant python3 \
  /config/scripts/kscape_ambiguous_e2e.py

# full chain: phrase -> match -> play -> stop (plays a real movie)
docker exec homeassistant python3 \
  /config/scripts/kscape_voice_e2e.py 192.0.2.10 192.0.2.11 "aladdin"
```
