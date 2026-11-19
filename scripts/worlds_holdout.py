"""Materialize complete 2026 Worlds series for an out-of-archive comparison."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fearless.model_features import ChampionCatalog, evidence_date
from fearless.rules import schedule, series_locks, validate_action

STAGES = ("Play-In", "Main Event")
CUTOFF = "2026-10-14"
ALIASES = {"Cloud9": "Cloud9 Kia", "Gen.G": "Gen.G Esports", "LYON (2024 American Team)": "LYON"}
TABLES = {
    "score": ("ScoreboardGames", "GameId,MatchId,Team1,Team2,Patch,N_GameInMatch=number,DateTime_UTC=start"),
    "duration": ("ScoreboardGames", "GameId,Gamelength=duration"),
    "schedule": ("MatchScheduleGame", "GameId,Blue,Red,FirstPick"),
    "draft": ("PicksAndBansS7", "GameId,Team1,Team2,IsComplete,IsFilled," + ",".join(
        f"Team{team}{kind}{n}" for team in (1, 2) for kind in ("Ban", "Pick") for n in range(1, 6))),
    "series": ("MatchSchedule", "MatchId,BestOf,Team1Score,Team2Score,DisabledChampions,IsNullified"),
}


def cached_table(directory, stage, key):
    path = directory / (stage.replace(" ", "-") + "-" + key + ".json")
    if not path.exists():
        table, fields = TABLES[key]
        params = dict(action="cargoquery", format="json", tables=table, fields=fields,
                      where=f"OverviewPage='2026 Season World Championship/{stage}'", limit=500)
        req = Request("https://lol.fandom.com/api.php?" + urlencode(params),
                      headers={"User-Agent": "FearlessRehearsal/1.0 (Worlds retrospective)"})
        for delay in (5, 15, 30, 60, 90):
            try:
                with urlopen(req, timeout=45) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                if exc.code != 429:
                    raise
            else:
                if "cargoquery" in payload:
                    rows = [entry["title"] for entry in payload["cargoquery"]]
                    if not rows or len(rows) >= 500:
                        raise ValueError(f"Unexpected {stage} {key} table size.")
                    path.write_text(json.dumps(rows, sort_keys=True))
                    break
                if payload.get("error", {}).get("code") != "ratelimited":
                    raise ValueError(f"Cargo request failed: {payload.get('error')}")
            sleep(delay)
        else:
            raise RuntimeError("Leaguepedia rate limited the query; cached tables can be reused.")
    return json.loads(path.read_text())


def identities(archive):
    names = defaultdict(set)
    for game in archive:
        if not game.get("eligible"):
            continue
        if evidence_date(game) >= CUTOFF:
            raise ValueError("Feature history overlaps the holdout period.")
        for key, name in game["team_names"].items():
            names[name.casefold()].add(key)
    return {name: next(iter(keys)) for name, keys in names.items() if len(keys) == 1}


def parse_series(metadata, games, draft_by_id, schedule_by_id, patch_map, known, *, earliest=CUTOFF, region="International"):
    games = sorted(games, key=lambda row: int(row["number"]))
    size = len(games)
    wins = [int(metadata[key]) for key in ("Team1Score", "Team2Score")]
    if metadata.get("IsNullified") == "1" or not size or sum(wins) != size or max(wins) != int(metadata["BestOf"]) // 2 + 1:
        raise ValueError("unfinished or inconsistent series score")
    if [int(game["number"]) for game in games] != list(range(1, size + 1)):
        raise ValueError("noncontiguous game numbers")
    catalog = ChampionCatalog.from_json(patch_map)
    previous, result = [], []
    participants = None
    for game in games:
        key, day = game["GameId"], game["start"][:10]
        start = datetime.fromisoformat(game["start"]).replace(tzinfo=timezone.utc)
        minutes, seconds = map(int, (game.get("duration") or "").split(":"))
        if minutes < 0 or not 0 <= seconds < 60 or minutes + seconds == 0:
            raise ValueError("invalid game duration")
        completion = start + timedelta(minutes=minutes, seconds=seconds)
        if day < earliest:
            raise ValueError("date overlaps archive")
        draft, selection = draft_by_id[key], schedule_by_id[key]
        names = [draft["Team1"], draft["Team2"]]
        if draft["IsFilled"] != "1" or draft["IsComplete"] != "1" or set(names) != {game["Team1"], game["Team2"]}:
            raise ValueError("incomplete draft or inconsistent participants")
        if selection["FirstPick"] != names[0] or set(names) != {selection["Blue"], selection["Red"]}:
            raise ValueError("first selection disagrees with the draft")
        major, minor = map(int, game["Patch"].split("."))
        patch = f"{major - 10 if major >= 25 else major}.{minor}"
        raw_disables = metadata.get("DisabledChampions") or ""
        disabled = {catalog.canonical(name.strip()) for name in raw_disables.split(",") if name.strip()}
        if disabled - set(patch_map[patch]):
            raise ValueError("unrecognized disabled champion")
        pool = [name for name in patch_map[patch] if name not in disabled]
        first, second = [known.get(ALIASES.get(n, n).casefold(), "leaguepedia:" + n) for n in names]
        if first == second or participants is not None and {first, second} != participants:
            raise ValueError("inconsistent series participants")
        participants = {first, second}
        locks = series_locks(previous)
        position, actions = Counter(), []
        for team, kind in schedule(first, second):
            position[team, kind] += 1
            field = f"Team{1 if team == first else 2}{kind.title()}{position[team, kind]}"
            value = draft[field]
            if not value:
                raise ValueError("missing draft action")
            champion = None if kind == "ban" and value.casefold() in ("none", "no ban") else catalog.canonical(value)
            action = dict(team=team, kind=kind, champion=champion)
            validate_action(pool, locks, actions, first, second, **action)
            actions.append(action)
        result.append(dict(series_id=game["MatchId"], game_id=key, game_number=int(game["number"]), series_games=size,
                           date=day, drafted_at=start.isoformat(), finished_at=completion.isoformat(),
                           patch=patch, region=region, first=first, second=second,
                           actions=actions, locked=sorted(locks), disabled=sorted(disabled),
                           fearless=True, eligible=True, finished=True))
        previous.append(actions)
    return result


def keyed(rows, field):
    result = {row[field]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate {field} in source table")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "catalog", "cache", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    known = identities(map(json.loads, args.archive.read_text().splitlines()))
    patch_map = json.loads(args.catalog.read_text())
    rows, excluded, hashes = [], {}, {}
    total = series_count = 0
    for stage in STAGES:
        tables = {key: cached_table(args.cache, stage, key) for key in TABLES}
        for key in tables:
            raw = args.cache / (stage.replace(" ", "-") + "-" + key + ".json")
            hashes[raw.name] = sha256(raw.read_bytes()).hexdigest()
        games = defaultdict(list)
        durations = keyed(tables["duration"], "GameId")
        for game in tables["score"]:
            games[game["MatchId"]].append({**game, "duration": durations.get(game["GameId"], {}).get("duration", "")})
        total += len(tables["score"])
        series_count += len(tables["series"])
        drafts, selections = keyed(tables["draft"], "GameId"), keyed(tables["schedule"], "GameId")
        metadata = keyed(tables["series"], "MatchId")
        if set(games) - set(metadata):
            raise ValueError("Scoreboard has a series absent from the schedule")
        for sid, meta in metadata.items():
            try:
                rows.extend(parse_series(meta, games[sid], drafts, selections, patch_map, known))
            except (ValueError, KeyError) as exc:
                excluded[sid] = str(exc)
    rows.sort(key=lambda row: (row["date"], row["series_id"], row["game_number"]))
    if not rows:
        raise ValueError("No whole eligible series")
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    report = dict(source="Leaguepedia, CC BY-SA 3.0", source_games=total, source_series=series_count,
                  games=len(rows), series=len({r["series_id"] for r in rows}), excluded=excluded,
                  dates=[rows[0]["date"], rows[-1]["date"]], hashes=hashes, sha256=sha256(data.encode()).hexdigest())
    for path in (args.output, args.audit):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(data)
    args.audit.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['games']}/{total} games, {report['series']}/{series_count} series")


if __name__ == "__main__":
    main()
