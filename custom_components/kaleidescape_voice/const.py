"""Constants for the Kaleidescape Voice integration."""

from __future__ import annotations

DOMAIN = "kaleidescape_voice"

# One address is enough: the protocol forwards to any device by serial, so
# setup discovers the rest (see system.py). CONF_PLAYER_HOST is gone.
CONF_HOST = "host"
CONF_SERVER_HOST = "server_host"
CONF_PLAYERS = "players"
CONF_DEFAULT_PLAYER = "default_player"
CONF_AGENT_ID = "agent_id"
CONF_API_KEY = "api_key"
CONF_ACTIVITY_ENTITY = "activity_entity"
CONF_ACTIVITY_STATE = "activity_state"

DEFAULT_PORT = 10000
DEFAULT_HTTP_PORT = 80

# `26-0.NUL` = start from the beginning (a real bookmark handle resumes).
BOOKMARK_START = "NUL"

# The options field is MANDATORY -- without it PLAY_MOVIE returns 000 and
# silently does nothing. 158-0 selects the audio stream; =1 is the default.
# See docs/play-by-name.md
DEFAULT_PLAY_OPTIONS = "158-0=1"

# Fallback library prefix if runtime discovery fails.
FALLBACK_HANDLE_PREFIX = "26-0."

LIBRARY_REFRESH_INTERVAL_HOURS = 12

SERVICE_PLAY_MOVIE = "play_movie"
SERVICE_REFRESH_LIBRARY = "refresh_library"
SERVICE_SEND_COMMAND = "send_command"
SERVICE_SEARCH = "search"
SERVICE_PLAY_RESULT = "play_result"

ATTR_TITLE = "title"
ATTR_HANDLE = "handle"
ATTR_COMMAND = "command"
ATTR_QUERY = "query"
ATTR_INDEX = "index"
ATTR_PLAYER = "player"

# Fired whenever a search produces results, so dashboards and the remote can
# render the list instead of the user having to listen to 30 titles.
EVENT_SEARCH_RESULTS = f"{DOMAIN}_search_results"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_metadata"

# How many results to actually speak. The rest are in the event/sensor payload.
MAX_SPOKEN_RESULTS = 5
MAX_RESULTS = 40

# Confidence floor for accepting a fuzzy title match without asking.
MATCH_ACCEPT_RATIO = 0.72
# Below this we do not even offer it as a candidate.
MATCH_CANDIDATE_RATIO = 0.45

# Claude-backed fallback for descriptive requests. Haiku 4.5 is the cheapest
# model and the right tier for a constrained lookup over a supplied list.
# NOTE: `output_config.effort` is REJECTED on Haiku 4.5 -- do not add it.
# How good the best LOCAL hit must be before we stop and skip the resolver.
#
# Measured across the real 202-title library, the two populations barely overlap:
#   confident local hits   0.62 - 1.00  (james bond .62, batman/aladdin 1.00)
#   things local can't do  0.00 - 0.44  ("a feel good movie" .44, shark .31)
# 0.55 sits in the gap. Re-measure with scripts/kscape_gate_selftest.py if the
# scoring in library.search() is ever retuned -- this number is downstream of it.
LOCAL_CONFIDENT_SCORE = 0.55

LLM_MODEL = "claude-haiku-4-5"
# Each match now carries title + year + handle + confidence, several times the
# size of the old handle-only reply. At 1024 a 10-match answer ("marvel") was
# truncated mid-JSON, which parses as a failure and silently fell back to local
# search -- intermittently, since it depends on how many matches come back.
LLM_MAX_TOKENS = 2048
# Caps a runaway list. 10 matches at ~60 tokens each sits well inside the cap.
LLM_MAX_RESULTS = 10
# Enough synopsis to identify a film, short enough that 202 of them stay cheap.
LLM_SYNOPSIS_CHARS = 160

# Auto-play thresholds for resolver answers. Measured on this library the two
# populations are cleanly separated, with nothing in between:
#   real identification   0.95 - 1.00  (Air Force One 0.98, Inception 0.95)
#   nearest relative      0.25 - 0.45  (Jurassic Park 0.45 for "the one with
#                                       the shark", because Jaws isn't owned)
# Re-measure with scripts/kscape_resolver_selftest.py before moving these.
LLM_AUTOPLAY_CONFIDENCE = 0.90
# Below this a suggestion isn't a real candidate, so it neither plays nor blocks
# a decisive answer by making the count look ambiguous.
LLM_PLAUSIBLE_CONFIDENCE = 0.50
SERVICE_VOICE_REQUEST = "voice_request"
