"""Collect the completed Game 1 draft for the registered Legends Premier League opener."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fearless.event_series import utc
from fearless.model_features import ChampionCatalog
from fearless.rules import schedule, validate_action
from scripts.event_drafts import CARGO, PASSED_BANS
from scripts.worlds_holdout import TABLES

STAGE = "Legends Premier League/Season 5"
MATCH_ID = STAGE + "_Round 1_1"
GAME_ID = MATCH_ID + "_1"
SCHEDULED_START = "2026-11-20T10:00:00+00:00"
TEAMS = {"Forsaken (Polish Team)", "Colossal Gaming"}
OFFICIAL_SCHEDULE = "https://docs.google.com/spreadsheets/d/1vTxypUCtYqdDUHwvC9B90nLVG50ZRAl07A4haHOLUcI/edit"
OFFICIAL_RULES = "https://docs.google.com/document/d/13zPaGwzccwMiRlrkTMj9DI5gGFfXfGz4BE6P_8f61mY/edit"
STREAM = "https://www.twitch.tv/legendpremierleague"


class PendingResult(Exception):
    """The source has yet to publish enough completed evidence."""


def cargo_row(key, where):
    table, fields = TABLES[key]
    if key == "series":
        fields += ",Team1,Team2,DateTime_UTC=start"
    query = urlencode(dict(action="cargoquery", format="json", tables=table,
                           fields="OverviewPage," + fields, where=where, limit=2))
    request = Request(CARGO + "?" + query, headers={"User-Agent": "FearlessRehearsal/1.0 fixture verification"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 429 or 500 <= error.code < 600:
            raise PendingResult(f"Leaguepedia temporarily unavailable for {key} (HTTP {error.code}).") from error
        raise
    except (TimeoutError, URLError) as error:
        raise PendingResult(f"Leaguepedia temporarily unavailable for {key}.") from error
    if payload.get("error", {}).get("code") == "ratelimited":
        raise PendingResult(f"Leaguepedia rate limited the {key} query.")
    if "cargoquery" not in payload:
        raise ValueError(f"Leaguepedia rejected the {key} query: {payload.get('error')}")
    rows = payload["cargoquery"]
    if not rows:
        raise PendingResult(f"The {key} record is not yet published.")
    if len(rows) != 1:
        raise ValueError(f"Expected one record for the registered fixture in {key}.")
    return rows[0]["title"]


def collect():
    stage = f"OverviewPage='{STAGE}'"
    match = f"{stage} AND MatchId='{MATCH_ID}'"
    series = cargo_row("series", match)
    score = cargo_row("score", match + " AND N_GameInMatch=1")
    game_id = score.get("GameId") or ""
    if game_id != GAME_ID:
        raise ValueError("Scoreboard identifier differs from the registered fixture.")
    game = f"{stage} AND GameId='{game_id}'"
    tables = {"series": series, "score": score, **{key: cargo_row(key, game) for key in ("duration", "schedule", "draft")}}
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "tables": tables}


def cargo_utc(row):
    if not row.get("start") or row.get("start__precision", "0") != "0":
        raise PendingResult("The source has yet to record a UTC time of day.")
    return datetime.strptime(row["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def reconcile(source, patch_map):
    raw = source["tables"]
    series, score, duration, selection, draft = (raw[key] for key in ("series", "score", "duration", "schedule", "draft"))
    game_id = score["GameId"]
    if (series["MatchId"] != MATCH_ID or score["MatchId"] != MATCH_ID or score["number"] != "1"
            or game_id != GAME_ID
            or any(row["OverviewPage"] != STAGE for row in raw.values())
            or any(row["GameId"] != game_id for row in (duration, selection, draft))
            or series["BestOf"] != "3" or {series["Team1"], series["Team2"]} != TEAMS):
        raise ValueError("Source metadata differs from the registered fixture.")
    if series.get("IsNullified") == "1":
        raise ValueError("The registered series was nullified.")
    if draft.get("IsFilled") != "1" or draft.get("IsComplete") != "1":
        raise PendingResult("The source draft is incomplete.")
    first, second = draft["Team1"], draft["Team2"]
    if not selection.get("FirstPick"):
        raise PendingResult("The source has yet to record an explicit first pick.")
    if (selection["FirstPick"] != first or {first, second} != TEAMS or {selection["Blue"], selection["Red"]} != TEAMS
            or {score["Team1"], score["Team2"]} != TEAMS):
        raise ValueError("Recorded first pick disagrees with the draft or fixture teams.")

    patch_label = score.get("Patch") or ""
    if not re.fullmatch(r"(16|26)\.\d+", patch_label):
        raise PendingResult("The source has yet to record a valid competitive patch.")
    patch = f"16.{int(patch_label.split('.')[1])}"
    if patch not in patch_map:
        raise ValueError(f"Competitive patch {patch} is absent from the supplied exact catalog.")
    started = cargo_utc(score)
    length = re.fullmatch(r"(\d+):([0-5]\d)", duration.get("duration") or "")
    if not length or not any(map(int, length.groups())):
        raise PendingResult("The source has yet to record a positive game duration.")
    minutes, seconds = map(int, length.groups())
    finished = started + timedelta(minutes=minutes, seconds=seconds)
    collected = utc(source["fetched_at"])
    if finished > collected:
        raise PendingResult("Scoreboard duration and collection time do not yet establish completion.")

    catalog = ChampionCatalog.from_json(patch_map)
    disabled = {catalog.canonical(name.strip()) for name in (series.get("DisabledChampions") or "").split(",") if name.strip()}
    if disabled - set(patch_map[patch]):
        raise ValueError("Unrecognized tournament-disabled champion.")
    pool = set(patch_map[patch]) - disabled
    actions, slots = [], Counter()
    for team, kind in schedule(first, second):
        slots[team, kind] += 1
        side = 1 if team == first else 2
        name = draft.get(f"Team{side}{kind.title()}{slots[team, kind]}")
        if not name:
            raise PendingResult("The source has yet to record every draft action.")
        champion = None if kind == "ban" and name.casefold() in PASSED_BANS else catalog.canonical(name)
        action = dict(team=team, kind=kind, champion=champion)
        validate_action(pool, (), actions, first, second, **action)
        actions.append(action)
    return dict(
        game_id=game_id, series_id=MATCH_ID, game_number=1, best_of=3, stage=STAGE,
        first=first, second=second, blue=selection["Blue"], red=selection["Red"], patch=patch,
        source_patch=patch_label, actions=actions, disabled=sorted(disabled), fearless=None,
        registered_start=SCHEDULED_START, scheduled_start=cargo_utc(series).isoformat(),
        game_started_at=started.isoformat(), estimated_finished_at=finished.isoformat(),
        collected_at=collected.isoformat(), target_lock_times={},
        source=dict(
            cargo=CARGO, schedule=OFFICIAL_SCHEDULE, rules=OFFICIAL_RULES, broadcast=STREAM,
            first_pick="MatchScheduleGame.FirstPick agrees with PicksAndBansS7.Team1",
            timings="ScoreboardGames UTC game start and duration; no draft action timing is available.",
        ),
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Exit 75 means completed source evidence is pending.")
    for name in ("catalog", "snapshot", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    cached = args.snapshot.exists()
    try:
        snapshot = json.loads(args.snapshot.read_text()) if cached else collect()
        result = reconcile(snapshot, json.loads(args.catalog.read_text()))
    except PendingResult as error:
        parser.exit(75, f"Pending result: {error}\n")
    if not cached:
        write_json(args.snapshot, snapshot)
    result["source_sha256"] = sha256(args.snapshot.read_bytes()).hexdigest()
    write_json(args.output, result)
    print(f"Reconciled {result['game_id']} on patch {result['patch']}.")


if __name__ == "__main__":
    main()
