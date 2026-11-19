"""Attach observed lineups to completed external games for use as past evidence."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fearless.event_forecaster import digest
from fearless.event_series import completed_series, read_event_games
from fearless.model_features import ChampionCatalog, ROLES, evidence_date
from scripts.compare_challengers import write_json


def fetch_players(stages, output):
    rows = []
    for offset in range(0, 50000, 500):
        quoted = ["'" + stage.replace("'", "''") + "'" for stage in stages]
        params = dict(action="cargoquery", format="json", tables="ScoreboardPlayers",
                      fields="GameId,Team,Role,Champion,Link,Name", where=f"OverviewPage IN ({','.join(quoted)})",
                      order_by="GameId,Team,Role,Link", limit=500, offset=offset)
        req = Request("https://lol.fandom.com/api.php?" + urlencode(params),
                      headers={"User-Agent": "FearlessRehearsal/1.0 completed draft research"})
        for delay in (2, 8, 16, 32, 60):
            try:
                with urlopen(req, timeout=45) as response:
                    result = json.load(response)
                if "cargoquery" in result:
                    break
                if result.get("error", {}).get("code") != "ratelimited":
                    raise ValueError(f"Unexpected Cargo response: {result.get('error')}")
            except HTTPError as exc:
                if exc.code != 429:
                    raise
            sleep(delay)
        else:
            raise RuntimeError("Player source is rate limited.")
        batch = [item["title"] for item in result["cargoquery"]]
        rows.extend(batch)
        if len(batch) < 500:
            write_json(output, rows)
            return
    raise ValueError("Too many rows for the selected stages.")


def player_history(archive):
    by_name = defaultdict(dict)
    for game in archive:
        if game.get("eligible"):
            day = evidence_date(game)
            for team, roster in game.get("rosters", {}).items():
                for row in roster:
                    if row.get("player_name") and row.get("player"):
                        known = by_name[str(team), row["player_name"].casefold()]
                        pid = str(row["player"])
                        known[pid] = min(known.get(pid, day), day)
    return by_name


def attach(game, rows, catalog, known):
    sides = defaultdict(list)
    for row in rows:
        sides[row["Team"]].append(row)
    if len(sides) != 2 or len(rows) != 10:
        return game
    picked = {team: {a["champion"] for a in game["actions"] if a["team"] == team and a["kind"] == "pick"}
              for team in (game["first"], game["second"])}
    rosters, names = {}, {}
    for label, players in sides.items():
        champions = {catalog.canonical(p["Champion"]) for p in players}
        team = next((t for t, choices in picked.items() if choices == champions), None)
        if (not team or team in rosters or {p["Role"].lower() for p in players} != set(ROLES)
                or not all(p.get("Link") and p.get("Name") for p in players)
                or len({p["Link"] for p in players}) != 5):
            return game
        names[team] = label
        roster = []
        for p in sorted(players, key=lambda p: ROLES.index(p["Role"].lower())):
            matches = [pid for pid, day in known.get((str(team), p["Name"].casefold()), {}).items() if day < game["date"]]
            pid = matches[0] if len(matches) == 1 else "leaguepedia:player:" + p["Link"]
            roster.append(dict(player=pid, player_name=p["Name"], champion=catalog.canonical(p["Champion"]),
                               role=p["Role"].lower(), role_source="completed Leaguepedia scoreboard"))
        rosters[team] = roster
    if len({p["player"] for lineup in rosters.values() for p in lineup}) != 10:
        return game
    return {**game, "team_names": {**names, **game.get("team_names", {})}, "rosters": rosters}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--stage", action="append", required=True)
    fetch.add_argument("--output", type=Path, required=True)
    join = sub.add_parser("attach")
    for field in ("games", "input-audit", "archive", "catalog", "players", "output", "audit"):
        join.add_argument("--" + field, type=Path, required=True)
    join.add_argument("--event", help="Event name for rows whose source does not supply one.")
    args = parser.parse_args()
    if args.command == "fetch":
        return fetch_players(args.stage, args.output)
    audit = json.loads(args.input_audit.read_text())
    if audit["sha256"] != digest(args.games):
        raise ValueError("Source games differ from input audit.")
    by_game = defaultdict(list)
    for row in json.loads(args.players.read_text()):
        by_game[row["GameId"]].append(row)
    known = player_history(read_event_games(args.archive))
    catalog = ChampionCatalog.from_json(json.loads(args.catalog.read_text()))
    games = [attach(g, by_game[g["game_id"]], catalog, known) for g in read_event_games(args.games)]
    if args.event:
        games = [{**g, "event": g.get("event") or args.event} for g in games]
    if len(completed_series(games)) != audit["series"] or len(games) != audit["games"]:
        raise ValueError("Source series totals disagree.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    temp.write_text("".join(json.dumps(g, sort_keys=True, ensure_ascii=False) + "\n" for g in games))
    temp.replace(args.output)
    report = {**audit, "source_games_sha256": audit["sha256"], "sha256": digest(args.output),
              "player_source_sha256": digest(args.players), "verified_lineup_games": sum(bool(g.get("rosters")) for g in games)}
    write_json(args.audit, report)
    print(f"Verified played positions and identities for {report['verified_lineup_games']}/{len(games)} games.")


if __name__ == "__main__":
    main()
