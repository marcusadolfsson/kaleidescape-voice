"""Kaleidescape library: enumeration, metadata and spoken-title matching.

The control protocol has no way to enumerate the library -- GET_CONTENT_LIST,
GET_MOVIE_LIST, GET_LIBRARY_SIZE and friends all return "Invalid request". The
only enumeration is the movie server's own web UI, so we scrape it:

    http://<server>/movies?collection=All

Rows look like:

    <tr class="movie_container" selection_handle="0-S_c449dd9b" ...>
      <td class="movie_title"><a ...>Air Force One</a></td>
      <td class="movie_genre">Action</td> ...

Note the server hands out BARE handles (`0-S_...`); the player needs them
qualified with the library prefix (`26-0.0-S_...`). Qualification lives in
KaleidescapePlayer.qualify().

Cast, genres and synopsis come from the server's per-title /details page
(async_enrich). They are not needed to PLAY a movie, but a search without them is
worse than useless rather than merely thin. Measured on this library, the scraped
table leaves only title, genre and director as text -- so "james bond" matches
seven films on the DIRECTOR'S FIRST NAME (James Gunn, James Mangold, James
Cameron) and returns Guardians of the Galaxy, Terminator and Titanic, with not
one Bond film among them. No title contains "bond" at all. Enriched, the same
query finds 17 via the synopses ("James Bond battles a mad industrialist...").
Enrichment is one request per title, cached to disk, and skipped for titles
already done.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import aiohttp

from .const import MATCH_ACCEPT_RATIO, MATCH_CANDIDATE_RATIO

_LOGGER = logging.getLogger(__name__)

_ROW_RE = re.compile(r'<tr class="movie_container"([^>]*)>(.*?)</tr>', re.S)
_HANDLE_RE = re.compile(r'selection_handle="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")

# Detail page rows: <th class="details_name">Cast</th><td class="details_value…">…</td>
_DETAIL_ROW_RE = re.compile(
    r'<th class="details_name">(.*?)</th>\s*<td class="details_value[^"]*">(.*?)</td>',
    re.S,
)
# "Rated PG; 4K Dolby Vision download, 1967, 117 mins"
_RUNTIME_RE = re.compile(r"(\d+)\s*mins")

# How much a hit in each field counts toward a search score. Title dominates;
# a synopsis hit is a weak signal on its own but is what makes franchise
# searches ("james bond") work at all, since the library table has no notion
# of a franchise.
_FIELD_WEIGHTS: dict[str, float] = {
    "title": 1.00,
    "actors": 0.88,
    "director": 0.88,
    "genres": 0.74,
    "collections": 0.74,
    "synopsis": 0.62,
}

# Spoken titles rarely include a leading article, and "and" / "&" vary.
_ARTICLES = ("the ", "a ", "an ")
_STOPWORDS = {"movie", "film", "the", "a", "an", "please", "part"}

_ROMAN = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}


def _strip_tags(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).replace("\xa0", " ").strip()


def normalize(text: str) -> str:
    """Fold a title (or an utterance) into a comparable form."""
    text = html.unescape(text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article):]
            break
    words = [_ROMAN.get(word, word) for word in text.split()]
    return " ".join(words).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in normalize(text).split() if w not in _STOPWORDS}


# Filler that a wildcard slot sweeps up along with the actual query. A sentence
# template can only strip what it names literally, and people do not phrase
# requests the way templates are written -- "show me {query} movies" matched
# "show me all my james bond movies" with query="all my james bond", and those
# two stray words pulled Shrek and Terminator to the top of a Bond search.
# Templates alone cannot fix this: any wildcard will always catch some of these,
# so the query is cleaned here where every path goes through it.
_QUERY_FILLER = {
    "all", "my", "me", "mine", "our", "some", "any", "of", "please",
    "movie", "movies", "film", "films", "titles", "collection", "library",
    "show", "list", "got", "have", "own", "there",
    # Question framing: "what kubrick movies do i have" should label itself
    # "kubrick", not "what kubrick movies do i".
    "what", "which", "who", "do", "does", "did", "i", "you", "me",
}


def clean_query(query: str) -> str:
    """Strip filler words a wildcard slot swept up, keeping the real query.

    Only drops filler from the EDGES -- an interior word is likely meaningful
    ("the man with the golden gun"), and stripping it would break titles. If
    everything is filler ("show me all my movies") the original is returned
    rather than an empty query, so the caller still has something to search.
    """
    words = (query or "").split()
    while words and words[0].strip(",.").lower() in _QUERY_FILLER:
        words.pop(0)
    while words and words[-1].strip(",.").lower() in _QUERY_FILLER:
        words.pop()
    return " ".join(words) if words else (query or "").strip()


# Verbs a spoken request opens with, both "start it" and "look for it" kinds.
#
# On the voice path there is no sentence template in front of this, so nothing
# else strips them -- and left in place they are searched for: "find james bond"
# matched 4 films instead of 17, because "find" is a word that appears across
# 202 synopses and dragged unrelated titles up the ranking.
_POLITE = r"^(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"

# "Put it on." Asking for the film to START.
_PLAY_PREFIX = re.compile(
    _POLITE + r"(?:play|watch|put on|put|start playing|start|"
    r"i want to watch|i wanna watch|let's watch|lets watch|"
    r"turn on)\s+",
    re.IGNORECASE,
)

# "What have I got?" Asking to be SHOWN, which is not the same request.
_SEARCH_PREFIX = re.compile(
    _POLITE + r"(?:find me|find|search for|search|look for|look up|"
    r"show me|show|list|pull up|bring up|get me)\s+",
    re.IGNORECASE,
)

# Interrogatives. "which movie is a child left alone in new york city" is a
# question about the library, not an instruction to start playing something.
_QUESTION_LEAD = re.compile(
    _POLITE + r"(?:which|what|whats|what's|who|whose|when|where|why|how|"
    r"is there|are there|do i|do we|does|did|have i|name the|name a|"
    r"tell me)\b",
    re.IGNORECASE,
)

_COMMAND_PREFIX = re.compile(
    f"(?:{_PLAY_PREFIX.pattern}|{_SEARCH_PREFIX.pattern})", re.IGNORECASE
)


def strip_command(spoken: str) -> str:
    """Drop a leading command verb: 'play aladdin' -> 'aladdin'."""
    return _COMMAND_PREFIX.sub("", (spoken or "").strip(), count=1).strip()


def wants_playback(spoken: str) -> bool:
    """True if this asks for a film to START, rather than asking ABOUT films.

    Auto-play should follow the request, not just the confidence. "watch the one
    where the boy is left home alone in new york" and "which movie is a child
    left alone in new york city" resolve to the SAME film with the same
    certainty, but only the first is asking for it to start; answering the
    second by playing it is obnoxious even though the match is right.

    Three cases, in order:
      * an explicit play verb  -> yes
      * an explicit search verb, or a question -> no
      * neither -- a bare title or description ("aladdin", "the one where the
        president fights terrorists on a plane") -> yes. Said into a remote in
        front of a theater, a naked title is a request to put it on.
    """
    text = (spoken or "").strip()
    if not text:
        return False
    if _PLAY_PREFIX.match(text):
        return True
    if _SEARCH_PREFIX.match(text) or _QUESTION_LEAD.match(text):
        return False
    return not text.rstrip().endswith("?")


def names_title(spoken: str, title: str, *, need: float = 0.6) -> bool:
    """True if `spoken` is mostly THIS TITLE, rather than a description of it.

    A fuzzy title match alone is not enough to justify playing something
    outright. "the one where the boy is left home alone" contains the literal
    words "home alone", so the local matcher returns Home Alone with high
    confidence -- and playing it skips the resolver entirely, which is wrong
    twice over: the phrasing is a description (the resolver's job), and Home
    Alone 2 fits it just as well, so it should have offered a choice.

    So require the title to ACCOUNT FOR most of what was said. "aladdin" and
    "indiana jones crystal skull" are almost entirely title words and play
    immediately; a nine-word description in which two words happen to be a title
    is not, and falls through to the resolver.
    """
    said = _tokens(spoken)
    if not said:
        return False
    return len(said & _tokens(title)) / len(said) >= need


def by_release_year(movies: list) -> list:
    """Newest first. Unknown/blank years sort last rather than as year zero."""
    def key(movie):
        year = (movie.year or "").strip()
        return (0, -int(year)) if year.isdigit() else (1, 0)

    return sorted(movies, key=key)


def rank_matches(matches: list) -> list:
    """Newest first WITHIN a relevance tier. Returns Movies.

    Sorting purely by year is right when the hits are peers -- "james bond" or
    "star wars" return films that all match equally well, and release order is
    then the only useful ordering. It is wrong the moment one hit is decisively
    better than the rest: "top gun" scores Top Gun at 1.00 and then three
    Guardians of the Galaxy films at 0.44 (their director is James *Gunn*), and
    a plain year sort answered a search for Top Gun with three Guardians films
    above it, because they are newer.

    So year sorting happens inside score tiers, not across them. Scores are
    bucketed to one decimal, which is coarse enough that genuine peers land
    together -- measured on this library, "star wars" and "kubrick" and "tom
    hanks" each collapse to a single tier and so keep a pure year order, while
    "top gun" separates 1.00 from 0.44 and keeps the real answer on top.
    """
    def key(match):
        year = (match.movie.year or "").strip()
        tier = round(match.score * 10)
        return (-tier, 0, -int(year)) if year.isdigit() else (-tier, 1, 0)

    return [m.movie for m in sorted(matches, key=key)]


@dataclass
class Movie:
    """One title in the library."""

    handle: str
    title: str
    genre: str = ""
    rating: str = ""
    director: str = ""
    year: str = ""
    media: str = ""
    # Filled in by async_enrich() from the server's /details page.
    actors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    synopsis: str = ""
    running_time: str = ""
    enriched: bool = False

    @property
    def normalized(self) -> str:
        """Comparable form of the title."""
        return normalize(self.title)

    def as_result(self) -> dict[str, str]:
        """Compact form for service responses, events and dashboards."""
        return {
            "handle": self.handle,
            "title": self.title,
            "year": self.year,
            "genre": self.genre,
            "director": self.director,
            "rating": self.rating,
            "runtime": self.running_time,
        }

    def searchable(self) -> dict[str, str]:
        """Per-field text that search() scores against."""
        return {
            "title": self.title,
            "actors": ", ".join(self.actors),
            "director": self.director,
            "genres": ", ".join(self.genres) or self.genre,
            "collections": ", ".join(self.collections),
            "synopsis": self.synopsis,
        }

    def describe(self) -> str:
        """One-line description, for prompts and for speaking back."""
        bits = [self.title]
        if self.year:
            bits.append(f"({self.year})")
        if self.director:
            bits.append(f"dir. {self.director}")
        if self.genre:
            bits.append(self.genre)
        return " ".join(bits)


@dataclass
class Match:
    """A candidate resolution of a spoken title."""

    movie: Movie
    score: float


class KaleidescapeLibrary:
    """Scrapes and indexes the movie server's library."""

    def __init__(self, session: aiohttp.ClientSession, server_host: str) -> None:
        """Initialise the library."""
        self._session = session
        self._server = server_host
        self._movies: list[Movie] = []
        self._by_handle: dict[str, Movie] = {}
        self._doc_freq: dict[str, int] = {}

    @property
    def movies(self) -> list[Movie]:
        """Every title currently known."""
        return list(self._movies)

    def __len__(self) -> int:
        """Number of titles."""
        return len(self._movies)

    def get(self, handle: str) -> Movie | None:
        """Look a title up by its bare handle."""
        return self._by_handle.get(handle)

    async def async_refresh(self) -> int:
        """Re-scrape the library. Returns the number of titles found."""
        url = f"http://{self._server}/movies?collection=All"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            resp.raise_for_status()
            # The page declares iso-8859-1; let aiohttp not guess wrong.
            body = await resp.text(encoding="iso-8859-1", errors="replace")

        movies: list[Movie] = []
        seen: set[str] = set()
        for attrs, row in _ROW_RE.findall(body):
            handle_match = _HANDLE_RE.search(attrs)
            if not handle_match:
                continue
            handle = handle_match.group(1)
            title = self._cell(row, "movie_title")
            if not title or handle in seen:
                # Quality-variant sub-rows repeat the parent handle; the parent
                # row plays the default variant, so the duplicates are dropped.
                continue
            seen.add(handle)
            movies.append(
                Movie(
                    handle=handle,
                    title=title,
                    genre=self._cell(row, "movie_genre"),
                    rating=self._cell(row, "movie_rating"),
                    director=self._cell(row, "movie_director"),
                    year=self._cell(row, "movie_year"),
                    media=self._cell(row, "movie_media_type"),
                )
            )

        if not movies:
            raise ValueError(f"no titles parsed from {url}")

        self._movies = movies
        self._by_handle = {m.handle: m for m in movies}
        self._build_index()
        _LOGGER.info("Kaleidescape library: %d titles", len(movies))
        return len(movies)

    @staticmethod
    def _cell(row: str, css_class: str) -> str:
        match = re.search(
            rf'<td class="{css_class}[^"]*"[^>]*>(.*?)</td>', row, re.S
        )
        return _strip_tags(match.group(1)) if match else ""

    # ------------------------------------------------------------------
    # enrichment
    # ------------------------------------------------------------------

    async def async_enrich(
        self, *, concurrency: int = 8, only_missing: bool = True
    ) -> int:
        """Fetch cast / genres / synopsis for each title.

        The library table has no cast or plot, so without this a search for a
        franchise ("james bond") or an actor has literally nothing to match --
        it would fall back to edit distance on titles and return nonsense.

        One HTTP request per title, bounded concurrency, and skipping titles
        already enriched, so a normal refresh costs nothing.
        """
        pending = [m for m in self._movies if not (only_missing and m.enriched)]
        if not pending:
            return 0

        semaphore = asyncio.Semaphore(concurrency)
        done = 0

        async def _one(movie: Movie) -> None:
            nonlocal done
            async with semaphore:
                try:
                    await self._async_enrich_movie(movie)
                    done += 1
                except Exception as err:  # noqa: BLE001 - one bad title must not stop the sweep
                    _LOGGER.debug("enrich failed for %s: %s", movie.title, err)

        await asyncio.gather(*(_one(m) for m in pending))
        self._build_index()
        _LOGGER.info("Kaleidescape library: enriched %d/%d titles", done, len(pending))
        return done

    async def _async_enrich_movie(self, movie: Movie) -> None:
        """Populate one title from the server's details page."""
        url = f"http://{self._server}/details?id={movie.handle}&callback=true"
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            resp.raise_for_status()
            body = await resp.text(encoding="iso-8859-1", errors="replace")

        for raw_name, raw_value in _DETAIL_ROW_RE.findall(body):
            name = _strip_tags(raw_name).lower()
            value = _strip_tags(raw_value)
            if not value:
                continue
            if name == "cast":
                movie.actors = [p.strip() for p in value.split(",") if p.strip()]
            elif name == "director":
                movie.director = movie.director or value
            elif name == "genres":
                movie.genres = [p.strip() for p in value.split(",") if p.strip()]
            elif name == "synopsis":
                movie.synopsis = value
            elif name.startswith("in the following collections"):
                movie.collections = [p.strip() for p in value.split(",") if p.strip()]

        misc = re.search(r'id="misc_text"[^>]*>(.*?)</div>', body, re.S)
        if misc and (runtime := _RUNTIME_RE.search(_strip_tags(misc.group(1)))):
            movie.running_time = runtime.group(1)

        movie.enriched = True

    def load_enrichment(self, cached: dict[str, dict]) -> int:
        """Re-apply previously saved enrichment (avoids 200 refetches on restart)."""
        applied = 0
        for handle, data in cached.items():
            movie = self._by_handle.get(handle)
            if movie is None:
                continue
            movie.actors = data.get("actors", [])
            movie.genres = data.get("genres", [])
            movie.collections = data.get("collections", [])
            movie.synopsis = data.get("synopsis", "")
            movie.running_time = data.get("running_time", "")
            movie.director = movie.director or data.get("director", "")
            movie.enriched = True
            applied += 1
        self._build_index()
        return applied

    def dump_enrichment(self) -> dict[str, dict]:
        """Serialise enrichment for the store."""
        return {
            m.handle: {
                "actors": m.actors,
                "genres": m.genres,
                "collections": m.collections,
                "synopsis": m.synopsis,
                "running_time": m.running_time,
                "director": m.director,
            }
            for m in self._movies
            if m.enriched
        }

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------

    def match(self, spoken: str, limit: int = 5) -> list[Match]:
        """Rank library titles against a spoken phrase, best first.

        Speech-to-text mangles titles in predictable ways (dropped articles,
        "two" for "II", missing subtitles after a colon), so this scores on the
        normalized form and rewards prefix and whole-word containment rather
        than relying on edit distance alone.
        """
        query = normalize(spoken)
        if not query:
            return []
        query_tokens = _tokens(spoken)

        scored: list[Match] = []
        for movie in self._movies:
            target = movie.normalized
            if not target:
                continue

            if target == query:
                scored.append(Match(movie, 1.0))
                continue

            ratio = SequenceMatcher(None, query, target).ratio()

            # A partial title only earns confidence in PROPORTION to how much of
            # the real title it covers. A flat bonus here meant "spy" scored 0.94
            # against "The Spy Who Loved Me" and played it, instead of showing
            # the 32 spy films -- while "indiana jones crystal skull", which
            # covers most of its title, must still play directly.
            if target.startswith(query) or query.startswith(target):
                coverage = min(len(query), len(target)) / max(len(target), 1)
                ratio = max(ratio, 0.55 + 0.45 * coverage)
            elif query in target or target in query:
                coverage = min(len(query), len(target)) / max(len(target), 1)
                ratio = max(ratio, 0.50 + 0.40 * coverage)

            if query_tokens:
                target_tokens = _tokens(movie.title)
                if target_tokens and query_tokens <= target_tokens:
                    coverage = len(query_tokens) / len(target_tokens)
                    ratio = max(ratio, 0.55 + 0.45 * coverage)
                elif target_tokens:
                    overlap = len(query_tokens & target_tokens) / len(query_tokens)
                    ratio = max(ratio, overlap * 0.8)

            if ratio >= MATCH_CANDIDATE_RATIO:
                scored.append(Match(movie, round(ratio, 3)))

        scored.sort(key=lambda m: (-m.score, m.movie.title))
        return scored[:limit]

    def best(self, spoken: str) -> Match | None:
        """Return a confidently-matched title, or None if it is ambiguous."""
        matches = self.match(spoken, limit=2)
        if not matches:
            return None
        top = matches[0]
        if top.score < MATCH_ACCEPT_RATIO:
            return None
        # Two near-identical scores means we genuinely cannot tell them apart
        # (e.g. a numbered sequel) -- better to ask than to play the wrong film.
        if len(matches) > 1 and top.score - matches[1].score < 0.04:
            return None
        return top

    def _build_index(self) -> None:
        """Count how many titles each word appears in (for IDF weighting)."""
        counts: dict[str, int] = {}
        for movie in self._movies:
            seen: set[str] = set()
            for text in movie.searchable().values():
                seen |= _tokens(text)
            for token in seen:
                counts[token] = counts.get(token, 0) + 1
        self._doc_freq = counts

    @staticmethod
    def _movie_matches(fields: dict[str, str], token: str) -> bool:
        """True if `token` appears in any searchable field of one title."""
        for text in fields.values():
            if not text:
                continue
            haystack = _tokens(text)
            if token in haystack or any(w.startswith(token) for w in haystack):
                return True
        return False

    def _any_match(self, token: str) -> bool:
        """True if any title in the library contains `token`."""
        return any(
            self._movie_matches(movie.searchable(), token) for movie in self._movies
        )

    def _idf(self, token: str) -> float:
        """Rarity weight: common words count for little, rare words carry the query."""
        total = max(len(self._movies), 1)
        freq = self._doc_freq.get(token, 0)
        return math.log((total + 1) / (freq + 1)) + 1.0

    def search(self, query: str, limit: int = 25) -> list[Match]:
        """Search the whole library, not just titles.

        This is what a low-confidence title match falls back to. "james bond"
        matches no title in the library and only weakly resembles a few
        ("Jason Bourne"), so title matching alone returns confident nonsense --
        the whole reason the play path refuses to guess and searches instead.
        Here the same phrase hits the Bond films through their synopses and the
        Spy genre.
        """
        tokens = _tokens(query)
        if not tokens:
            return []

        # Weight each query word by how rare it is. Without this, "james bond"
        # ranked 24 films that merely have a *James* in the cast (0.44) above 10
        # actual Bond films whose synopsis says only "Bond" (0.31) -- because
        # both were "one of two words matched" and cast outweighs synopsis.
        # "james" is common across 202 casts; "bond" is not, so it should carry
        # the query.
        weights = {token: self._idf(token) for token in tokens}
        total_weight = sum(weights.values()) or 1.0

        # The rarest word in the query is what the query is ABOUT. For
        # "james bond" that is "bond" (17 titles) rather than "james" (31, mostly
        # cast members called James), so requiring it drops Guardians of the
        # Galaxy and keeps every Bond film. IDF alone could not do this: the two
        # scores were 2.85 vs 3.42, close enough that a cast hit on the common
        # word still outranked a synopsis hit on the rare one.
        required = max(tokens, key=lambda t: weights[t]) if len(tokens) > 1 else None
        if required and not self._any_match(required):
            # Nobody has the key word -- fall back to scoring everything, rather
            # than confidently returning nothing.
            required = None

        results: list[Match] = []
        for movie in self._movies:
            fields = movie.searchable()
            if required and not self._movie_matches(fields, required):
                continue
            # For each query word, credit it at the weight of the best field it
            # appears in -- a word matched in the title counts for more than the
            # same word buried in a synopsis.
            score_sum = 0.0
            for token in tokens:
                best_field = 0.0
                for field_name, text in fields.items():
                    if not text:
                        continue
                    haystack = _tokens(text)
                    if token in haystack or any(w.startswith(token) for w in haystack):
                        best_field = max(best_field, _FIELD_WEIGHTS[field_name])
                if best_field:
                    score_sum += weights[token] * best_field
            best_score = score_sum / total_weight

            # Deliberately NO edit-distance on the title here. Search is about
            # relevance, not spelling: fuzzy titles made "james bond" rank
            # "Jason Bourne" first, "kubrick" pull in "Jurassic Park", and
            # "batman" pull in "Ant-Man". Token coverage on the title field
            # above already handles real title hits ("star wars", "batman").

            # A franchise search often only lands one token in one synopsis
            # ("Bond and his Japanese counterparts..."), which is a genuine but
            # weak hit -- hence the low floor.
            if best_score >= 0.28:
                results.append(Match(movie, round(best_score, 3)))

        results.sort(key=lambda m: (-m.score, m.movie.title))
        return results[:limit]

    def filter(
        self,
        *,
        genre: str | None = None,
        director: str | None = None,
        actor: str | None = None,
        year: str | None = None,
        rating: str | None = None,
    ) -> list[Movie]:
        """Return titles matching simple metadata criteria."""
        results = self._movies
        if genre:
            needle = normalize(genre)
            results = [
                m for m in results
                if needle in normalize(m.genre)
                or any(needle in normalize(g) for g in m.genres)
            ]
        if director:
            needle = normalize(director)
            results = [m for m in results if needle in normalize(m.director)]
        if actor:
            needle = normalize(actor)
            results = [
                m for m in results
                if any(needle in normalize(a) for a in m.actors)
            ]
        if year:
            results = [m for m in results if m.year == str(year)]
        if rating:
            needle = rating.upper()
            results = [m for m in results if m.rating.upper() == needle]
        return results

    def catalog_lines(self) -> list[str]:
        """Compact one-line-per-title catalog, for grounding an LLM."""
        return [
            f"{m.handle}\t{m.title}\t{m.year}\t{m.genre}\t{m.director}"
            for m in self._movies
        ]
