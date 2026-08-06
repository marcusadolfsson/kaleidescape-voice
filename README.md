# Kaleidescape Voice

Say a movie name and the theater plays it — or describe one you can't name, and
let a model work it out.

```
"Play Air Force One"
"The one where the president fights terrorists on a plane"
"What movies do I have by Kubrick?"
"Surprise me with a sci-fi movie"
```

## How it works

```
  "play aladdin"                    Home Assistant
  "the one with the ...."   ─────▶  ┌──────────────────────────────────┐
                                    │ custom sentences  or             │
                                    │ voice_request (any phrasing)     │
                                    └────────────────┬─────────────────┘
                                                     ▼
                                    ┌──────────────────────────────────┐
                                    │ cached library  (~200 titles)    │
                                    │ fuzzy match, offline, ~1 ms      │
                                    └────────┬────────────────┬────────┘
                                    confident │                │ weak
                                              │                ▼
                                              │      ┌────────────────────┐
                                              │      │ Claude Haiku 4.5   │
                                              │      │ catalog in prompt  │
                                              │      └─────────┬──────────┘
                                              ▼                ▼
                                    ┌──────────────────────────────────┐
                                    │ play  │  or publish a result list│
                                    └────────────────┬─────────────────┘
                                                     ▼
                                     PLAY_MOVIE over TCP 10000
```

### The Home Assistant layer

One config entry per system. It owns a cached copy of the library, a client per
player, and two ways in: HA's built-in conversation agent via the shipped
sentence file, or the `voice_request` service, which takes any wording and is
what you call if you'd rather route audio yourself.

Everything else is ordinary HA surface — services for playback and search, a
`media_player` carrying search results as a `source_list`, sensors for the
library size and last results, and an event when results change. Nothing here
needs a cloud account; the config entry's only optional setting is a Claude API
key.

### Talking to the Kaleidescape

Two jobs, which may live on one machine or two — a player with its own storage
does both, and setup handles either without asking:

| Job | Port | |
|---|---|---|
| **Playback** — a player (Strato, Alto…) | TCP **10000** | Takes playback commands. Serves no useful HTTP. |
| **The library** — a server (Terra…) | HTTP **80** | Hosts the library. Rejects playback commands. |

**Reading the library is a scrape, because it has to be.** The control protocol
cannot enumerate content at all — `GET_CONTENT_LIST`, `GET_MOVIE_LIST` and
`GET_LIBRARY_SIZE` all answer "Invalid request". So the library comes from the
server's own web UI, is cached to disk, and is enriched with cast, director and
synopsis from each title's details page. That enrichment is what makes "james
bond" work: no *title* contains those words, but the synopses do.

**Playing is a single undocumented command** sent to the player on TCP 10000,
addressed by serial. The connection is opened per command rather than held open,
and the reply is not believed on its own — this device returns success for
commands it silently ignores, so playback is confirmed by polling the play
status afterwards. Details in [docs/play-by-name.md](docs/play-by-name.md).

### Where Claude comes in

Naming a title never leaves the house. It is a fuzzy match against the cached
table — milliseconds, offline, free — and "play Aladdin" resolves there.

A model is consulted only when that match comes back **weak**, judged on the best
local score rather than the number of hits. That distinction matters: counting
treats one perfect hit and one bad guess alike, so it both wastes calls on
answers already in hand and skips the queries that most need help. Ten unrelated
films scoring 0.31 is exactly the case worth asking about.

When it is asked, the whole catalog goes in the prompt and the model returns
matching films with a confidence score — enough to play something outright, or
to hand back a list. Leave the API key empty and this path never runs; naming
titles still works.

That's the interesting part, and it's covered properly in
[Describing a film you can't name](#describing-a-film-you-cant-name).

## Install

**HACS** → three-dot menu → *Custom repositories* → add this repo as an
**Integration** → install → restart.
**Manually**: copy `custom_components/kaleidescape_voice/` into
`config/custom_components/` and restart.

Then pick how it should listen — the choice decides how much of your house's
vocabulary this claims.

**A. Share Assist.** Copy `custom_sentences/en/kaleidescape_voice.yaml` into
`config/custom_sentences/en/` and Assist understands "play Aladdin". Drop-in,
and right if Assist is already your assistant.

The catch: `play {movie}` is a bare wildcard, so it matches *any* "play …",
including "play some jazz". **Set the activity gate below** — don't run it
ungated in a house with other media players. And note that a matched sentence is
*claimed*: with the gate shut you get "the Kaleidescape isn't the active source"
rather than a fall-through to whatever else might have answered.

**B. Bind it to the source.** Skip the sentence file and call the service:

```yaml
action: kaleidescape_voice.voice_request
data:
  query: "{{ whatever_was_said }}"
```

`voice_request` takes **any phrasing** — no sentence template in front of it —
and decides itself whether to play or offer a list. Route audio to it only while
the Kaleidescape is active and it never sees an utterance meant for anything
else, so your existing assistant keeps the whole house. That's how the author
runs it: a remote's voice key routes by activity, and with no source active the
press is dropped without being transcribed.

## Setup

Settings → Devices & Services → **Add Integration** → *Kaleidescape Voice*.

**Enter one address — any component.** The rest is discovered: the protocol
routes by device, so a single connection can enumerate the whole system with
`GET_AVAILABLE_DEVICES_BY_SERIAL_NUMBER`. Each device is classified by whether
it has **movie zones** (a player has at least one, a server none) rather than by
model name, which changes every product generation. The library is read from a
device with no movie zones; if there isn't one, it's a single box and your
address is used for both. The library is fetched for real before the entry is
created, so a wrong address fails now rather than mid-sentence later.

Players take the friendly name already set in the Kaleidescape app, so "play it
in the living room" uses a name you chose.

### The activity gate

Set **activity entity** (e.g. `input_select.av_activity`) and **activity state**
(`Watch Kaleidescape`) in options, and every action declines unless the
Kaleidescape is the active source. It fails safe — a missing or unavailable
entity means shut. Leaving it unset disables gating: fine for a single-purpose
install, wrong otherwise.

## Voice commands

Local, via HA's built-in agent. The title is a wildcard handed to the fuzzy
matcher, so it tolerates how speech recognition actually behaves: dropped
articles, spelled-out numerals (`two thousand one a space odyssey`), missing
subtitles (`indiana jones crystal skull`), joined words (`amazing spiderman 2`).

| | |
|---|---|
| **Play** | `play …`, `put on …`, `start …`, `watch …`, `I want to watch …` |
| **Search** | `search for …`, `find …`, `do I have …`, `show me … movies` |
| **Pick** | `play number 3`, `play the second one` |
| **Transport** | `pause/resume/stop the movie`, `next`, `previous`, `replay`, `fast forward`, `details`, `menu`, `change subtitles`, `change audio track` |
| **Ask** | `what's playing`, `what movie is this` |
| **Browse** | `what sci-fi movies do I have`, `what movies do I have by <director>` / `with <actor>` / `from <year>`, `show me my comedies` |
| **Random** | `surprise me`, `play something`, `play a random action movie` |

There is deliberately **no bare `pause`** sentence — that word has to keep
meaning "pause whatever HA is playing" for every other player in the house.

### Nothing plays on a vague ask

If a phrase doesn't resolve to one title, nothing plays and the candidates come
back instead:

```
"play james bond"  ->  "I found 17 matching james bond. The first 5 are:
                        1. No Time to Die, 2. Die Another Day, 3. Tomorrow
                        Never Dies, 4. Licence to Kill, 5. A View to a Kill."
"play number 3"    ->  "Playing Tomorrow Never Dies."
```

"james bond" is not a title, it matches 17 films, and picking one is a coin flip
you have to undo. Same for `spy` (30) and `sean connery` (7).

Results are ordered **newest first within a relevance tier**, not by year alone:
plain year ordering put three newer, weakly-matching films above *Top Gun* in a
search for it, because their director is James **Gunn**.

## Describing a film you can't name

Local search matches literal strings, so it cannot answer "the one where the
president fights terrorists on a plane" — no field contains those words. Set a
**Claude API key** in options and those requests go to a model holding your whole
library in its prompt. Leave it empty and everything stays local.

| Request | Outcome |
|---|---|
| "the one where the president fights terrorists on a plane" | Air Force One — plays |
| "a heist movie set in dreams" | Inception — plays |
| "the one where the boy is left home alone" | Home Alone *and* Home Alone 2 — asks |
| "the one with the shark" | *Jaws*, not owned — declines |

Three ideas do the work, and they generalise past this hardware.

**Ask for confidence and act on the number.** Each match carries a calibrated
`confidence`. Measured on a real 202-title library the populations barely touch:
genuine identifications land at 0.95–1.00, "here's the nearest thing I have" at
0.25–0.45. Wide enough to hold a decision.

**Identify semantically, never by identifier.** The obvious design returns the
library's own handle, and it doesn't work: every handle here shares the prefix
`0-S_c4`, only 43 distinct 8-character prefixes exist across 202 titles, and the
median handle differs from its nearest neighbour by four characters. Copying one
is *transcription* — what models are worst at — while identifying the film is
reasoning, which they're good at. It failed exactly that way, returning the
handle for *Cars* while plainly meaning *Air Force One*, and since a near-miss is
another real handle, "does this exist?" can't catch it. Films are identified by
**(title, year)**, which is unique, semantic, and verifiable against the library.

**Keep "found nothing" apart from "couldn't ask".** An empty answer means the
library genuinely has nothing, so weak local guesses are suppressed — asking for
*Jaws* returns nothing rather than nine unrelated films. A failed API call means
no opinion was formed, so local results stand. Collapse the two and a network
blip starts reporting an empty library.

### Before anything plays by itself

Three conditions, each of which produced a real wrong answer when it was missing:

1. **Identified semantically.** Matches are looked up in the library, so an
   invented film yields "no match" rather than a different movie. Title alone
   isn't enough — libraries hold *Beauty and the Beast* (1991 **and** 2017) —
   hence the year. Where a title is unique the year is a checksum and a mismatch
   is logged and ignored; where it's a duplicate the year is the only
   discriminator, so one matching neither is dropped.
2. **Confident and alone.** ≥0.90, and exactly one candidate ≥0.50. "The one
   with the shark" scores *Jurassic Park* 0.45 when *Jaws* isn't owned — the
   model knows it's substituting. "The one where the boy is left home alone"
   returns Home Alone 0.95 **and** Home Alone 2 0.85: both fit, so it asks.
   Add "in New York" and it plays.
3. **You asked for playback.** Confidence is not intent:
   `"watch the one where the boy is left home alone in new york"` plays;
   `"which movie is a child left alone in new york city"` lists. Same film, same
   certainty, different request.

Anything failing these lands in the results list — one tap from playing.

**Cost.** `claude-haiku-4-5`, ~$0.013 per query, 1–6 s, and only vague requests
reach it. The catalog is grounded in the prompt (~12.7k tokens) rather than
fine-tuned: 202 titles is far too small to train on and would go stale the moment
you buy a film. Two Haiku-4.5 traps: `output_config.effort` is rejected (an
Opus/Sonnet parameter — adding it 400s the request), and the prompt-cache
minimum is 4096 tokens, which a title-only catalog would sit under and silently
never cache.

## Results, and where they go

Reading seventeen Bond titles aloud isn't a usable answer, so results are
published for a screen:

| Surface | Use |
|---|---|
| `media_player.kaleidescape_voice_search` | `source_list` of numbered results; select one to play it |
| `sensor.*_search_results` | Numbered results with handles, for dashboards |
| `kaleidescape_voice_search_results` event | For automations |

A `media_player` is a deliberate shape for a picker: a results list isn't known
when the dashboard is written, so anything taking options from layout config
can't show it. `source_list` is the one widely-supported attribute that's live,
so results render on hardware whose firmware you can't change.

## Services

| Service | |
|---|---|
| `voice_request` | `query:` — the whole voice path in one call. Any phrasing; plays or returns candidates |
| `play_movie` | `title:` (fuzzy) or `handle:` (exact) |
| `search` | `query:` — returns matches |
| `play_result` | `index:` — play the Nth result |
| `refresh_library` | Re-scrape + enrich now (also runs every 12 h) |
| `send_command` | Raw protocol command, for diagnostics |

## The protocol

`PLAY_MOVIE` is **undocumented** — it appears nowhere in the control protocol
manual, whose command vocabulary contains no play-by-handle command at all.

```
#70300002028/1/PLAY_MOVIE:26-0.0-S_c449dd9b:26-0.NUL;158-0=1::
```

Three quirks each make it fail **silently** — status `000`, no playback: it must
be addressed by serial with `#`, the trailing `::` is mandatory, and so is the
options field. Which is why success codes are never trusted, as above.

Full notes: **[docs/play-by-name.md](docs/play-by-name.md)**. Kaleidescape's own
reference manual is not redistributed here — ask Kaleidescape. `PLAY_MOVIE`
isn't in it anyway.

## Testing

Run inside the Home Assistant container. The last two play a real movie.

```bash
docker exec homeassistant python3 /config/scripts/kscape_library_selftest.py <server-ip>
docker exec homeassistant python3 /config/scripts/kscape_search_selftest.py <server-ip>
docker exec homeassistant python3 /config/scripts/kscape_ambiguous_e2e.py
docker exec homeassistant python3 /config/scripts/kscape_voice_e2e.py <player-ip> <server-ip> "aladdin"
```

## Licence

Apache-2.0. Not affiliated with or endorsed by Kaleidescape.
