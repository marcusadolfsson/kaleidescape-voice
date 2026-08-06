"""Claude-backed resolution for requests a title match can't answer.

The local matcher in `library.py` handles anything that names a title, and it
does so in milliseconds, offline, for free. What it structurally cannot do is
use knowledge that isn't in the scraped metadata:

    "the one where the president fights terrorists on a plane"
    "something like Inception"
    "a Pixar movie"
    "something funny for the kids, under two hours"

Those need a model that knows what these films *are*. So this runs only where
`library.best()` gave up -- never on the "play Aladdin" path.

## Grounding, not fine-tuning

The catalog goes in the prompt, not into a trained model. 202 titles is far too
small to fine-tune on, it would go stale the moment a movie is bought, and it
cannot be updated incrementally. Grounding costs a few thousand tokens per call
and is always current.

## Two rules that make this safe

1. **A film is identified by (title, year), never by its handle.** Handles are
   opaque hex and the model cannot reliably copy them. Measured on this library:
   all 202 begin with the identical `0-S_c4`, there are only 43 distinct
   8-character prefixes, and the median handle differs from its nearest
   neighbour by 4 characters -- so the discriminating information is two to four
   nibbles at the tail of a stub shared with dozens of rows. That is a
   transcription task, not a reasoning one, and it failed as such:

       "where they kidnap the president" -> 0-S_c449ad2e (Cars), meant ...dd9b
       "the one with the talking cars"   -> 0-S_c449ac0d (Finding Nemo)

   Both wrong handles are REAL entries, 17-28 rows from the intended film, so
   "does this handle exist?" cannot catch it. Title and year are *semantic* --
   the model knows them about the film rather than copying a string -- and the
   pair is unique across all 202. Title alone is not: Beauty and the Beast
   (1991/2017) and The Lion King (1994/2019) are why the year is carried.

   A wrong year is survivable where it doesn't matter and fatal where it does,
   deliberately: for the 198 unique titles the year is only a checksum and a
   mismatch is logged and ignored, while for the 4 duplicates it is the only
   discriminator, so a year matching neither is dropped rather than guessed.
2. **Nothing reaches the theater unverified.** Every match is looked up in the
   library; an invented film simply isn't there. The worst case is "no match" --
   never a different movie.

## Model choice

`claude-haiku-4-5` ($1/$5 per MTok) -- the cheapest model, and the right tier:
this is a constrained lookup over a list supplied in the prompt, not a reasoning
task. Two Haiku-4.5-specific constraints are load-bearing here:

* **`output_config.effort` is rejected on Haiku 4.5** -- it is an Opus/Sonnet
  parameter. Don't add it "for quality"; the request will 400.
* **The prompt-cache minimum is 4096 tokens on Haiku 4.5** (higher than the
  newer models). A title-only catalog lands near that and would silently never
  cache. Including synopses both makes descriptive queries work *and* pushes the
  prefix clear of the minimum.

Caching is requested but not relied on: home voice queries arrive minutes or
hours apart and the default TTL is 5 minutes, so most calls pay full price
(~$0.012). Only vague queries reach this path at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, NamedTuple

from .const import (
    LLM_AUTOPLAY_CONFIDENCE,
    LLM_MAX_RESULTS,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_PLAUSIBLE_CONFIDENCE,
    LLM_SYNOPSIS_CHARS,
)

_LOGGER = logging.getLogger(__name__)


class Suggestion(NamedTuple):
    """One resolver match, with how sure the model is that it's THE film."""

    movie: Any
    confidence: float


def decide_playback(
    suggestions: list[Suggestion],
    *,
    play_at: float = LLM_AUTOPLAY_CONFIDENCE,
    plausible_at: float = LLM_PLAUSIBLE_CONFIDENCE,
) -> Any | None:
    """Return the movie to play outright, or None to show the list instead.

    Two conditions, and the second is the one that is easy to get wrong:

    1. The match must be held with high confidence. Measured on this library the
       populations are cleanly separated -- real identifications come back at
       0.95-1.00 ("the one where the president fights terrorists on a plane" ->
       Air Force One, 0.98) and stretches at 0.25-0.45 ("the one with the shark"
       -> Jurassic Park, 0.45, because Jaws isn't owned). Nothing lands between.

    2. It must be the ONLY plausible candidate. "the one where the boy is left
       home alone" returns Home Alone at 0.99 AND Home Alone 2 at 0.75; the
       first clears any sane threshold, but both films genuinely fit what was
       asked, so picking one is a coin flip the viewer has to undo. Sorting by
       confidence and taking the top would play it anyway -- counting plausible
       candidates first is what makes this show a list.

    Weak runners-up don't block: a 0.1 also-ran isn't a real candidate and
    shouldn't turn a decisive answer into a menu.
    """
    plausible = [s for s in suggestions if s.confidence >= plausible_at]
    if len(plausible) == 1 and plausible[0].confidence >= play_at:
        return plausible[0].movie
    return None

SYSTEM_INSTRUCTIONS = """\
You match a spoken request to movies in a specific personal library.

You are given the complete library, one movie per line, tab-separated:
    handle<TAB>title<TAB>year<TAB>genres<TAB>director<TAB>cast<TAB>synopsis

Rules:
- Return only films that appear in the library above. Never invent one, and
  never return a movie that is not in the list, however well it fits the
  request.
- Identify each match by its TITLE and YEAR, copied from the same line of the
  library. Two films here share a title (Beauty and the Beast, The Lion King),
  so the year is what tells them apart -- copy it from the line rather than
  recalling it from memory. Include the handle too if you can copy it exactly;
  it is only checked for agreement, and the title and year decide.
- Order best match first.
- Return a single film when the request clearly identifies one. Return several
  when the request describes a set ("something funny", "a Pixar movie").
- SEQUELS AND SERIES: if the library holds other entries in the same series that
  ALSO fit what was described, return them too, each with its own confidence --
  do not silently pick the one you think is meant. "the one where the boy is
  left home alone" fits both Home Alone and Home Alone 2; the viewer should get
  the choice. Only a description that separates them ("the one in New York")
  should come back as a single film.
- Return an empty list if nothing in this library fits. That is a correct
  answer; a wrong movie is not. Prefer returning nothing over a loose thematic
  association: if the film the user is describing simply isn't here, say so by
  returning nothing rather than offering the nearest other film.
- Use what you know about these films, not just the text provided: the request
  may describe a plot, an actor, a franchise, a studio, or a vibe.
- Give every match a `confidence` from 0.0 to 1.0: how sure you are that it is
  the SPECIFIC film described, not merely a good suggestion. Be calibrated and
  honest -- this number decides whether the theater starts playing immediately
  or shows a list to choose from, so overstating it starts the wrong film.
    * above 0.9  -- the description unambiguously identifies this exact title
    * around 0.5 -- plausible, but you would not bet on it
    * below 0.4  -- the film they mean is probably NOT in this library and you
                    are offering the nearest relative. Say so with a low number
                    rather than a high one; a good film that isn't the one they
                    asked for is still the wrong answer.\
"""

# The film is identified by (title, year), NOT by its handle. Handles are opaque
# hex and the model cannot reliably copy them: measured on this library all 202
# begin with the identical `0-S_c4`, there are only 43 distinct 8-character
# prefixes, and the median handle differs from its nearest neighbour by 4
# characters. So the discriminating information is two to four nibbles buried at
# the tail of a stub shared with dozens of other rows -- a transcription task,
# not a reasoning one, and it slipped in exactly that way:
#
#   "where they kidnap the president" -> 0-S_c449ad2e (Cars), meant 0-S_c449dd9b
#   "the one with the talking cars"   -> 0-S_c449ac0d (Finding Nemo), meant Cars
#
# Both wrong handles are REAL library entries 17-28 rows from the intended one,
# so "does this handle exist?" can never catch it.
#
# (title, year) is unique across all 202 and both fields are semantic -- the
# model knows them about the film rather than copying them off a line. Title
# alone is not unique: Beauty and the Beast (1991/2017) and The Lion King
# (1994/2019) are why the year is here.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "description": "Matches, best first. Empty if nothing in the library fits.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title, copied verbatim from the library.",
                    },
                    "year": {
                        "type": "string",
                        "description": (
                            "The year from that same library line. This is how "
                            "two films sharing a title are told apart, so copy "
                            "it from the line rather than recalling it."
                        ),
                    },
                    "handle": {
                        "type": "string",
                        "description": (
                            "The handle from that line, if you can copy it "
                            "exactly. Only used to check agreement; the title "
                            "and year decide."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "0.0-1.0: how sure you are this is the SPECIFIC film "
                            "described, not merely a good suggestion. Above 0.9 "
                            "means unambiguous. Below 0.4 means the film they "
                            "mean is probably not in this library and this is "
                            "the nearest relative."
                        ),
                    },
                },
                "required": ["title", "year", "confidence"],
                "additionalProperties": False,
            },
        },
        "note": {
            "type": "string",
            "description": "One short phrase on why these match. For logs only.",
        },
    },
    "required": ["matches"],
    "additionalProperties": False,
}


class ClaudeResolver:
    """Resolves descriptive requests to library handles."""

    def __init__(self, api_key: str, model: str = LLM_MODEL) -> None:
        """Initialise the resolver."""
        self._api_key = api_key
        self._model = model
        self._client = None

    @property
    def available(self) -> bool:
        """True if an API key was configured."""
        return bool(self._api_key)

    async def _async_get_client(self):
        """Lazily build the async client, off the event loop.

        Both the import and the constructor block: AsyncAnthropic builds an SSL
        context, and `load_verify_locations` reads certifi's CA bundle from disk.
        Doing that inline trips HA's blocking-call detector, so it goes to the
        executor. Built once, then reused.
        """
        if self._client is None:

            def _build():
                from anthropic import AsyncAnthropic

                return AsyncAnthropic(api_key=self._api_key)

            loop = asyncio.get_running_loop()
            self._client = await loop.run_in_executor(None, _build)
        return self._client

    @staticmethod
    def build_catalog(movies: list) -> str:
        """Render the library as one tab-separated line per movie.

        Synopses are truncated: they carry most of the descriptive signal but
        would otherwise dominate the prompt.
        """
        lines = []
        for movie in movies:
            synopsis = (movie.synopsis or "")[:LLM_SYNOPSIS_CHARS]
            genres = "/".join(movie.genres) or movie.genre
            cast = ", ".join(movie.actors[:4])
            lines.append(
                "\t".join(
                    (
                        movie.handle,
                        movie.title,
                        movie.year,
                        genres,
                        movie.director,
                        cast,
                        synopsis,
                    )
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _normalise(title: str) -> str:
        """Fold a title for comparison: case, punctuation and spacing only."""
        return re.sub(r"[^a-z0-9]+", "", title.lower())

    @classmethod
    def _title_index(cls, library) -> dict[str, list]:
        """Map normalised title -> movies. Lists, because duplicates exist."""
        index: dict[str, list] = {}
        for movie in library.movies:
            index.setdefault(cls._normalise(movie.title), []).append(movie)
        return index

    async def async_resolve(self, query: str, library) -> list | None:
        """Return matches for a descriptive request, best first.

        The empty list and None mean DIFFERENT things, and the caller acts on
        the difference:

        * ``[]``   -- the resolver ran and judged that nothing in the library
          fits. That is an answer. Weak lexical hits underneath it are noise and
          should be suppressed, not shown as consolation.
        * ``None`` -- the resolver could not be consulted (no key, network blip,
          refusal, bad JSON). No opinion was formed, so the caller must keep
          whatever local search found; degrading to "nothing" because an API
          call failed would be worse than an imperfect list.
        """
        if not self.available:
            return None

        catalog = self.build_catalog(library.movies)
        try:
            client = await self._async_get_client()
            response = await client.messages.create(
                model=self._model,
                max_tokens=LLM_MAX_TOKENS,
                system=[
                    {"type": "text", "text": SYSTEM_INSTRUCTIONS},
                    {
                        "type": "text",
                        "text": f"LIBRARY ({len(library)} movies):\n{catalog}",
                        # Stable prefix; the query below is the only part that
                        # varies, so it must come after this breakpoint.
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}
                },
                messages=[{"role": "user", "content": query}],
            )
        except Exception as err:  # noqa: BLE001 - never break voice on an API problem
            _LOGGER.warning("Claude resolver failed for %r: %s", query, err)
            return None

        if response.stop_reason == "refusal":
            _LOGGER.warning("Claude declined to resolve %r", query)
            return None

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        try:
            payload = json.loads(text)
        except ValueError:
            _LOGGER.warning("Claude returned non-JSON for %r: %.120s", query, text)
            return None

        # Cross-check handle against title. Validating the handle alone only
        # catches invented handles; it cannot catch a handle copied off the
        # wrong line, because that handle really is in the library. When the two
        # disagree we believe the TITLE -- it is the field the model reasons
        # about and gets right, while the handle is opaque hex it merely
        # transcribes. Observed both ways round on this library.
        by_title = self._title_index(library)
        resolved, dropped = [], []
        for match in payload.get("matches", [])[:LLM_MAX_RESULTS]:
            claimed = (match.get("title") or "").strip()
            year = str(match.get("year") or "").strip()
            handle = (match.get("handle") or "").strip()
            try:
                confidence = float(match.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0

            candidates = by_title.get(self._normalise(claimed), ())
            movie = None

            if len(candidates) == 1:
                # Title is unique in the library (198 of 202 here), so it alone
                # identifies the film and the year is only a checksum. Accept a
                # disagreement rather than discarding a correct answer over it.
                movie = candidates[0]
                if year and movie.year and year != movie.year:
                    _LOGGER.debug(
                        "Claude said %r (%s) but the library has %s; title is "
                        "unique so using it",
                        claimed, year, movie.year,
                    )
            elif candidates:
                # Duplicate title -- the year is the ONLY thing separating them
                # (Beauty and the Beast 1991/2017, The Lion King 1994/2019), so
                # a year that matches neither leaves nothing to choose on.
                movie = next((m for m in candidates if m.year == year), None)
                if movie is None:
                    dropped.append(
                        f"{claimed!r} ({year}) - ambiguous, years are "
                        f"{[m.year for m in candidates]}"
                    )
                    continue

            if movie is None:
                # Not a title in this library. An invented film lands here, which
                # is the whole point: unknown identifier -> no match, never a
                # different film.
                dropped.append(f"{claimed!r} ({year})")
                continue

            # The handle is advisory now. Logged when it disagrees so the real
            # slip rate is measurable rather than assumed -- it is what this
            # used to be keyed on, and it is why it isn't any more.
            if handle and handle != movie.handle:
                _LOGGER.info(
                    "Claude's handle %r disagrees with %r (%s) = %s; ignored",
                    handle, movie.title, movie.year, movie.handle,
                )

            resolved.append(Suggestion(movie, confidence))

        if dropped:
            _LOGGER.warning(
                "Claude returned %d unusable match(es) for %r (dropped): %s",
                len(dropped),
                query,
                dropped,
            )
        _LOGGER.debug(
            "Claude resolved %r -> %s (%s)",
            query,
            [(s.movie.title, s.confidence) for s in resolved],
            payload.get("note", ""),
        )
        return resolved
