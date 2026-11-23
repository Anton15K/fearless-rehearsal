"""Validate released tournament drafts before they become evidence for another series."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from fearless.rules import DRAFT_LENGTH, series_locks, validate_action


def utc(value):
    if not isinstance(value, str):
        raise ValueError("An explicit UTC timestamp is required.")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Completion and draft timestamps must have an explicit time zone.")
    return timestamp.astimezone(timezone.utc)


def completion_day(series):
    return max(utc(game["finished_at"]) for game in series).date().isoformat()


def completed_series(rows):
    """Return whole series ordered by completion; declared size comes from the match result."""
    groups = defaultdict(list)
    game_ids = set()
    for game in rows:
        if not game.get("series_id") or not game.get("game_id") or game["game_id"] in game_ids:
            raise ValueError("Series and unique game identifiers are required.")
        game_ids.add(game["game_id"])
        groups[game["series_id"]].append(game)
    complete = []
    for members in groups.values():
        series = tuple(sorted(members, key=lambda g: g["game_number"]))
        size = len(series)
        if (not 1 <= size <= 5 or any(type(g.get("series_games")) is not int or g["series_games"] != size for g in series)
                or [g["game_number"] for g in series] != list(range(1, size + 1))):
            raise ValueError("Incomplete or inconsistent series size.")
        players = {series[0]["first"], series[0]["second"]}
        formats = {g["best_of"] for g in series if g.get("best_of") is not None}
        if len(formats) > 1:
            raise ValueError("Conflicting series formats.")
        best_of = next(iter(formats), None)
        if best_of is not None:
            if type(best_of) is not int or best_of not in (1, 3, 5) or not best_of // 2 + 1 <= size <= best_of:
                raise ValueError("Game count is inconsistent with the series format.")
            wins = Counter()
            target = best_of // 2 + 1
            for index, game in enumerate(series):
                if winner := game.get("winner"):
                    if winner not in players:
                        raise ValueError("Unrecognized series participant in game results.")
                    wins[winner] += 1
                    if index < size - 1 and wins[winner] >= target:
                        raise ValueError("Game played after the series was clinched.")
            if sum(wins.values()) == size and max(wins.values()) != target:
                raise ValueError("Game winners do not establish a completed series.")
        event = series[0].get("event")
        history, last_end = [], None
        for game in series:
            if not all(game.get(key) is True for key in ("eligible", "finished", "fearless")):
                raise ValueError("Event evidence requires completed eligible Fearless games.")
            start, end = utc(game["drafted_at"]), utc(game["finished_at"])
            if (end <= start or start.date().isoformat() != game["date"]
                    or last_end is not None and start < last_end):
                raise ValueError("Invalid game timestamps within a completed series.")
            last_end = end
            if {game["first"], game["second"]} != players or game.get("event") != event:
                raise ValueError("Teams and event must agree across the series.")
            locked = series_locks(history)
            if set(game["locked"]) != locked or len(game["actions"]) != DRAFT_LENGTH:
                raise ValueError("Incomplete draft or inconsistent series locks.")
            available = {a["champion"] for a in game["actions"] if a["champion"]} - set(game.get("disabled", ()))
            actions = []
            for action in game["actions"]:
                validate_action(available, locked, actions, game["first"], game["second"],
                                team=action["team"], kind=action["kind"], champion=action["champion"])
                actions.append(action)
            history.append(actions)
        complete.append(series)
    return tuple(sorted(complete, key=lambda series: (completion_day(series), str(series[0]["series_id"]))))


def read_event_games(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf8").splitlines() if line.strip()]
