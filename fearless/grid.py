"""Read the draft evidence in a local GRID event stream."""

import io
import json
import shutil
import subprocess
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import orjson
except ImportError:
    orjson = json


@contextmanager
def open_feed(path):
    if path.suffix != ".zst":
        with path.open("rb") as stream:
            yield stream
        return
    try:
        from compression import zstd
    except ImportError:
        try:
            import zstandard as zstd
        except ImportError:
            zstd = None
    if zstd is not None:
        with io.BufferedReader(zstd.open(path, "rb")) as stream:
            yield stream
        return
    if not shutil.which("zstd"):
        raise RuntimeError("Reading .zst feeds requires Python 3.14+, zstandard, or zstd on PATH")
    with subprocess.Popen(["zstd", "-qdc", "--", str(path)], stdout=subprocess.PIPE) as process:
        yield process.stdout
        if process.wait():
            raise OSError(f"Could not decompress {path}")


def timestamp(value):
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def identity(value):
    if isinstance(value, dict):
        return identity(value.get("id"))
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def ordinal(value):
    value = identity(value)
    return int(value) if value.isascii() and value.isdecimal() and len(value) < 10 else None


def objects(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def spelling(value):
    return "".join(character for character in str(value).casefold() if character.isalnum())


def champion_name(value, character_names):
    if isinstance(value, dict):
        name = value.get("name") or (value.get("state") or {}).get("name")
        character_id = identity(value)
    else:
        name, character_id = None, identity(value)
    if spelling(name or character_id) in {"", "none", "noban", "empty", "null", "0"}:
        return None
    return character_names.get(spelling(character_id)) or character_names.get(spelling(name)) or name


def decode_action(raw, character_names, *, kind=None):
    if kind is None:
        kind = raw.get("type", raw.get("kind"))
        team = identity(raw.get("drafter") or raw.get("teamId") or raw.get("team"))
        target_key = next((key for key in ("draftable", "character", "champion", "characterId") if key in raw), None)
    else:
        team = identity(raw.get("actor"))
        target_key = "target" if "target" in raw else None
    if kind not in ("pick", "ban"):
        return None, True
    target = raw.get(target_key)
    if isinstance(target, dict) and target.get("type") not in (None, "character"):
        return None, True
    champion = champion_name(target, character_names)
    empty = target is None or spelling(identity(target) or (target.get("name") if isinstance(target, dict) else "") or "") in {
        "", "none", "noban", "empty", "null", "0",
    }
    unresolved = not team or (champion is None and (kind == "pick" or target_key is None or not empty))
    return {"team": team, "kind": kind, "champion": champion}, unresolved


def snapshot_actions(raw_actions, character_names):
    actions = []
    issues = set()
    numbered = {}
    for raw in raw_actions:
        if not isinstance(raw, dict):
            issues.add("unresolved_draft_action")
            continue
        action, unresolved = decode_action(raw, character_names)
        if unresolved:
            issues.add("unresolved_draft_action")
        if action is None:
            continue
        sequence = ordinal(raw.get("sequenceNumber"))
        if sequence is None:
            actions.append(action)
        elif sequence in numbered and numbered[sequence] != action:
            issues.add("conflicting_draft_slots")
        else:
            numbered[sequence] = action
    if numbered:
        ordered = sorted(numbered)
        if ordered[0] not in (0, 1) or any(second != first + 1 for first, second in zip(ordered, ordered[1:])):
            issues.add("missing_draft_slots")
        if actions:
            issues.add("mixed_draft_order")
        actions = [numbered[sequence] for sequence in ordered] + actions
    return actions, issues


@dataclass
class FeedGame:
    state: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    issues: set = field(default_factory=set)
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class Feed:
    games: dict = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    ruleset: str | None = None
    best_of: int | None = None
    series_teams: list = field(default_factory=list)


DRAFT_TYPES = {
    "team-picked-character": ("pick", False), "team-banned-character": ("ban", False),
    "team-!picked-character": ("pick", True), "team-!banned-character": ("ban", True),
}
GAME_TYPES = {"series-started-game", "series-!started-game", "series-ended-game"}
RELEVANT_EVENTS = tuple(f'"{kind}"'.encode() for kind in (*DRAFT_TYPES, *GAME_TYPES, "grid-ended-feed"))


def envelope_header(line):
    # GRID writes the sequence metadata before events. Parse only that prefix for
    # ordinary telemetry. The full decoder still supports other JSON key orders.
    marker = line.find(b'"events"')
    if marker >= 0 and not any(event_type in line for event_type in RELEVANT_EVENTS):
        prefix = line[:marker].strip().removesuffix(b",")
        if prefix.startswith(b"{"):
            try:
                header = orjson.loads(prefix + b"}")
                if "sequenceNumber" in header and "seriesId" in header:
                    return {**header, "events": []}
            except ValueError:
                pass
    return orjson.loads(line)


def event_game_id(event):
    game_id = identity(event.get("gameId"))
    if game_id:
        return game_id
    for role in ("target", "actor"):
        entity = event.get(role) or {}
        if entity.get("type") == "game":
            return identity(entity)
    changes = objects((event.get("seriesStateDelta") or {}).get("games"))
    if len(changes) == 1:
        return identity(changes[0])
    return ""


def draft_game_id(event, active_id):
    game_id = event_game_id(event)
    if game_id:
        return game_id
    state = event.get("seriesState") or {}
    games = objects(state.get("games"))
    unfinished = [game for game in games if game.get("finished") is not True]
    if len(unfinished) == 1:
        return identity(unfinished[0])
    if active_id and any(identity(game) == active_id for game in unfinished):
        return active_id
    if len(games) == 1:
        return identity(games[0])
    return active_id if not games else ""


def capture_state(feed, state, rolled_back):
    fmt = state.get("format")
    if fmt in ("best-of-1", "best-of-3", "best-of-5"):
        feed.best_of = int(fmt[-1])
    if isinstance(state.get("teams"), list):
        feed.series_teams = objects(state["teams"])
    if state.get("draftFormat"):
        feed.ruleset = state["draftFormat"]
    if isinstance(state.get("fearless"), bool):
        feed.ruleset = "fearless" if state["fearless"] else "standard"
    for game in objects(state.get("games")):
        game_id = identity(game)
        if game_id and game_id not in rolled_back:
            feed.games.setdefault(game_id, FeedGame()).state.update(game)


def record_draft(feed_game, event, kind, undo, happened_at, character_names):
    action, unresolved = decode_action(event, character_names, kind=kind)
    if unresolved:
        feed_game.issues.add("unresolved_event_action")
    if action is None:
        return
    if not undo:
        feed_game.events.append((action, happened_at))
        return
    for index in range(len(feed_game.events) - 1, -1, -1):
        if feed_game.events[index][0] == action:
            del feed_game.events[index]
            return
    feed_game.issues.add("unmatched_draft_rollback")


def read_feed(path, series_id, character_names):
    feed = Feed()
    seen_envelopes, seen_events, rolled_back = set(), set(), set()
    active_id = ""
    previous_sequence = None
    last_state = {}
    with open_feed(path) as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                envelope = envelope_header(line)
            except ValueError:
                feed.counts["malformed_envelopes"] += 1
                continue
            if not isinstance(envelope, dict) or not isinstance(envelope.get("events"), list):
                feed.counts["malformed_envelopes"] += 1
                continue
            if identity(envelope.get("seriesId")) != series_id:
                feed.counts["foreign_envelopes"] += 1
                continue
            envelope_id = identity(envelope)
            if envelope_id and envelope_id in seen_envelopes:
                feed.counts["duplicate_envelopes"] += 1
                continue
            if envelope_id:
                seen_envelopes.add(envelope_id)
            sequence = ordinal(envelope.get("sequenceNumber"))
            if sequence is None or (previous_sequence is not None and sequence <= previous_sequence):
                feed.counts["unordered_envelopes"] += 1
                continue
            if previous_sequence is not None and sequence > previous_sequence + 1:
                feed.counts["envelope_sequence_gaps"] += sequence - previous_sequence - 1
            previous_sequence = sequence
            happened_at = timestamp(envelope.get("occurredAt"))
            feed.counts["envelopes"] += 1
            for event in objects(envelope["events"]):
                state = event.get("seriesState")
                if isinstance(state, dict) and state:
                    last_state = state
                event_type = event.get("type")
                if event_type in DRAFT_TYPES:
                    event_id = identity(event)
                    if event_id and event_id in seen_events:
                        feed.counts["duplicate_draft_events"] += 1
                        continue
                    if event_id:
                        seen_events.add(event_id)
                    game_id = draft_game_id(event, active_id)
                    if not game_id:
                        feed.counts["unassigned_draft_events"] += 1
                        continue
                    if game_id not in rolled_back:
                        kind, undo = DRAFT_TYPES[event_type]
                        record_draft(feed.games.setdefault(game_id, FeedGame()), event, kind, undo, happened_at, character_names)
                elif event_type in GAME_TYPES:
                    game_id = event_game_id(event)
                    if not game_id:
                        feed.counts["unassigned_game_events"] += 1
                        continue
                    if event_type == "series-!started-game":
                        rolled_back.add(game_id)
                        feed.games.pop(game_id, None)
                        if active_id == game_id:
                            active_id = ""
                    elif game_id not in rolled_back:
                        game = feed.games.setdefault(game_id, FeedGame())
                        if event_type == "series-ended-game":
                            game.finished_at = happened_at
                            if active_id == game_id:
                                active_id = ""
                        else:
                            active_id = game_id
                            game.started_at = happened_at
                    if isinstance(state, dict):
                        capture_state(feed, state, rolled_back)
    capture_state(feed, last_state, rolled_back)
    feed.counts["rolled_back_games"] = len(rolled_back)
    return feed, rolled_back
