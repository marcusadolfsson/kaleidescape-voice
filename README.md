# Kaleidescape Voice

Say a movie name, the theater plays it.

```
"Play Air Force One"
"Play Indiana Jones Crystal Skull"
"Surprise me with a sci-fi movie"
"What movies do I have by Kubrick?"
```

## How it works

Two jobs, which may or may not live on the same machine:

| Job | Port | Notes |
|---|---|---|
| **Playback** — a player (Strato, Alto…) | TCP **10000** | Takes playback commands. Serves no useful HTTP. |
| **The library** — a movie server (Terra…) | HTTP **80** | Hosts the library. Rejects playback commands. |

On a system with a separate server these are two boxes with two addresses. On a
player with its own storage they are **one box doing both**, and plenty of
systems are built that way. Setup handles either without asking you which you
have — see below.

The control protocol **cannot enumerate the library** — `GET_CONTENT_LIST`,
`GET_MOVIE_LIST`, `GET_LIBRARY_SIZE` all return "Invalid request". So the
library is scraped from the server's own web UI (`/movies?collection=All`) and
cached, then a spoken phrase is fuzzy-matched against it and the resulting
handle is played on the player.

Matching a **named** title is local and instant — no LLM involved. It is a
lookup against a ~200-row table, resolved in milliseconds, offline and free.
"Play Aladdin" should never cost a network round trip.

### The interesting half: asking for a film you can't name

The part worth stealing is what happens when you *can't* remember the title.
Local search matches literal strings in scraped metadata, so it structurally
cannot answer "the one where the president fights terrorists on a plane" — no
field contains those words. A model that knows what these films *are* can, and
it is given the whole library in the prompt:

```
"the one where the president fights terrorists on a plane"  → Air Force One      plays
"a heist movie set in dreams"                               → Inception          plays
"the one where the boy is left home alone"                  → Home Alone 1 and 2 asks
"the one with the shark"                                    → Jaws, not owned    declines
```

Three ideas do most of the work, and they generalise well beyond Kaleidescape:

**Ask the model how sure it is, and act on the number.** Every match carries a
calibrated `confidence`. Measured against a real 202-title library the two
populations barely touch — genuine identifications land at 0.95–1.00, while
"I'm offering you the nearest thing I have" lands at 0.25–0.45. That gap is
wide enough to hold a decision: above 0.90 the film starts playing on its own.

**Make the model name things semantically, never by identifier.** The obvious
design has it return the library's own handle. It doesn't work, and the reason
is worth knowing: every handle here shares the prefix `0-S_c4`, only 43 distinct
8-character prefixes exist across 202 titles, and the median handle differs from
its nearest neighbour by four characters. Copying one is a *transcription* task —
the thing LLMs are worst at — while identifying the film is reasoning, which they
are good at. It failed exactly that way, returning a handle for *Cars* while
plainly meaning *Air Force One*. And because a near-miss is another real handle,
"does this exist?" cannot catch it. So films are identified by **(title, year)**,
which is unique, semantic, and independently verifiable against the library.

**Distinguish "I found nothing" from "I couldn't ask".** An empty answer from
the model means the library genuinely has nothing, and weak local guesses are
then suppressed — asking for *Jaws* returns nothing rather than nine unrelated
films. A failed API call means no opinion was formed, so local results stand. Fold
those two into one value and a network blip starts reporting an empty library.

Full detail, including the auto-play rules, is in
[Descriptive requests](#descriptive-requests-claude-haiku-45) below. Leave the
API key empty and none of this runs; everything else still works.

## Install

**HACS** → three-dot menu → *Custom repositories* → add this repo as an
**Integration** → install → restart Home Assistant.

**Manually**: copy `custom_components/kaleidescape_voice/` into your
`config/custom_components/` and restart.

### Two ways to run it, and the choice matters

There are two entry points, and which one you use decides how much of your
house's vocabulary this integration lays claim to.

**Sharing Assist with everything else.** Copy
`custom_sentences/en/kaleidescape_voice.yaml` into `config/custom_sentences/en/`
and Assist starts understanding "play Aladdin". This is the drop-in option, and
the one to pick if Assist is already your assistant.

The cost is ambiguity: `play {movie}` is a bare wildcard, so it matches *any*
"play …" — including "play some jazz". **Set the activity gate** (next section)
and it only claims those while the Kaleidescape is the active source. Do not run
it ungated in a house with other media players.

Be aware of what a matched sentence means: it is **claimed**. If the gate is shut
the reply is "the Kaleidescape isn't the active source" rather than falling
through to whatever else might have answered. That is why the shipped templates
are anchored on distinctive lead-ins and a closed genre list rather than broad
wildcards — a catch-all `{query}` sentence was tried during development and
swallowed the house, because Home Assistant prefers a matching wildcard over a
more specific built-in intent. With it live, "turn off the office light" and
"what is the weather" both became library searches.

**Bound to the source, alongside another assistant.** Skip the sentence file
entirely and call the service directly:

```yaml
action: kaleidescape_voice.voice_request
data:
  query: "{{ whatever_was_said }}"
```

`voice_request` takes **any phrasing** — there is no sentence template in front
of it — and decides for itself whether to play or offer a list. Route audio to it
only while the Kaleidescape is the active source, and this integration never sees
an utterance meant for anything else. Nothing is claimed, nothing is ambiguous,
and your existing assistant keeps the whole house.

That is how the author runs it: a remote's voice key routes by activity — one
source to Siri, Kaleidescape to `voice_request`, and neither active means the
press is dropped without being transcribed at all.

## Setup

Settings → Devices & Services → **Add Integration** → *Kaleidescape Voice*.

**Enter one address — any component of the system.** The rest is discovered.

That works because the control protocol routes by device: connect to any
component's port 10000, address a command to another device with `#<serial>/`,
and it is forwarded. So `GET_AVAILABLE_DEVICES_BY_SERIAL_NUMBER` enumerates the
whole system from a single connection.

Each device is then classified by whether it has any **movie zones** — a player
has at least one, a server reports none. That is the distinction that matters,
rather than the model name, which changes with every product generation. The
library is read from a device with no movie zones; if there isn't one, the
system is a single box and the address you entered is used for both.

Players are named from the friendly name already set in the Kaleidescape app, so
targeting ("play it in the living room") uses a name you chose rather than one
invented during setup.

The library is fetched for real before the entry is created, so a wrong address
fails at setup rather than the first time someone speaks.

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
"play james bond"  ->  "I found 17 matching james bond. The first 5 are:
                        1. No Time to Die, 2. Die Another Day,
                        3. Tomorrow Never Dies, 4. Licence to Kill,
                        5. A View to a Kill. Say a number to play one."
"play number 3"    ->  "Playing Tomorrow Never Dies."
```

This is the whole point of the confidence rule. "james bond" is not a title, it
matches 17 films, and picking one is a coin flip the user has to undo. Same for
`spy` (30 results) and `sean connery` (7).

Results are ordered **newest first within a relevance tier** — not by year
alone. Plain year ordering is right when the hits are peers, and wrong the moment
one is decisively better: searching "top gun" put three newer, weakly-matching
films above *Top Gun* itself, because their director is James **Gunn**.

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
| `kaleidescape_voice.voice_request` | `query:` — **the whole voice path in one call.** Takes any phrasing, plays it or returns the candidates. Use this to run alongside another assistant |
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

### What has to be true before anything plays

The model can start a film without you tapping anything, so three independent
conditions have to hold. Each exists because removing it produced a real wrong
answer during development.

**1. The film must be identified semantically.** Matches come back as
`(title, year)` and are looked up in the library. An invented film simply isn't
there, so the failure is "no match" rather than a different movie. Title alone
is not enough — libraries contain *Beauty and the Beast* (1991 **and** 2017) and
*The Lion King* (1994 **and** 2019) — hence the year. Where a title is unique
the year is only a checksum and a mismatch is logged and ignored; where it is a
duplicate the year is the only discriminator, so one matching neither is dropped
rather than guessed.

**2. Confidence must be high, and the candidate must be alone.** ≥0.90 to play,
and exactly one candidate scoring ≥0.50. Both halves matter:

- "the one with the shark" scores *Jurassic Park* at 0.45 when *Jaws* isn't
  owned — the model knows it is offering a substitute and says so. It lists.
- "the one where the boy is left home alone" returns *Home Alone* at 0.95 **and**
  *Home Alone 2* at 0.85. The first clears any threshold, but both genuinely fit
  what was asked, so choosing one is a coin flip you have to undo. It lists.
  Adding "in New York" collapses it to one match, and that plays.

**3. You must have asked for playback.** Confidence is not intent. These resolve
to the same film with the same certainty:

```
"watch the one where the boy is left home alone in new york"  → plays
"which movie is a child left alone in new york city"          → lists
```

Only the first is asking for it to start. Answering the second by playing it is
obnoxious even though the match is right, so the phrasing is classified before
the leading verb is stripped — stripping is what destroys the evidence.

Anything that fails these lands in the results list instead, which is a tap away
from playing and costs a glance.

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
