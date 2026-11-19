"""Attach historical role observations from the game's own early-lane telemetry."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import orjson
import zstandard

PETS = {"Gustwalker Hatchling", "Scorchclaw Pup", "Mosstomper Seedling"}
SUPPORT = {"World Atlas", "Runic Compass", "Bounty of Worlds", "Celestial Opposition",
           "Bloodsong", "Dream Maker", "Solstice Sleigh", "Zaz'Zak's Realmspike"}


def lane(position):
    if not position:
        return None
    x, y = position.get("x"), position.get("y")
    if x is None or y is None:
        return None
    if x < 4400 and y > 10200:
        return "top"
    if x > 10200 and y < 4400:
        return "bot"
    if abs(x - y) < 1800 and 4900 < x < 10800 and 4900 < y < 10800:
        return "mid"
    return None


def match_roles(path):
    """Use completed games only downstream. Missing or conflicting observations stay unknown."""
    seen, item_roles, lane_votes = set(), defaultdict(set), defaultdict(Counter)
    with zstandard.open(path, "rt") as stream:
        for line in stream:
            if '"currentSeconds"' not in line:
                continue
            for event in orjson.loads(line)["events"]:
                for game in (event.get("seriesState") or {}).get("games", [])[-1:]:
                    sec = (game.get("clock") or {}).get("currentSeconds") or 0
                    if not 150 <= sec <= 660:
                        continue
                    for team in game.get("teams", []):
                        for player in team.get("players", []):
                            key = (game["id"], team["id"], player["id"])
                            items = {item.get("name") for item in
                                     (player.get("inventory") or {}).get("items", [])
                                     if item.get("quantity", 1) > 0}
                            if items & PETS:
                                item_roles[key].add("jungle")
                            if items & SUPPORT:
                                item_roles[key].add("support")
                            zone = lane(player.get("position"))
                            bucket = (*key, sec // 30)
                            if zone and bucket not in seen:
                                seen.add(bucket)
                                lane_votes[key][zone] += 1
    candidates = {}
    for key in item_roles.keys() | lane_votes.keys():
        items = item_roles.get(key, set())
        votes = lane_votes[key]
        if len(items) == 1:
            candidates[key] = (next(iter(items)), "observed role item")
        elif not items and votes:
            role, count = votes.most_common(1)[0]
            if count >= 3 and count / votes.total() >= 0.7:
                candidates[key] = (role, "inferred from repeated early lane positions")
    # Two teammates with the same inferred role is ambiguous. Preserve direct item evidence.
    frequency = Counter((game, team, role) for (game, team, _), (role, _) in candidates.items())
    return {key: evidence for key, evidence in candidates.items()
            if frequency[(*key[:2], evidence[0])] == 1 or evidence[1] == "observed role item"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-dir", type=Path, required=True)
    parser.add_argument("--games", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.games.resolve() == args.output.resolve():
        parser.error("supply a distinct output file")
    with (args.grid_dir / "lol/series.csv").open() as stream:
        paths = [args.grid_dir / row["file"] for row in csv.DictReader(stream)]
    observations = {}
    with ProcessPoolExecutor(args.workers) as pool:
        for result in pool.map(match_roles, paths, chunksize=4):
            observations.update(result)
    counts = Counter()
    tmp = args.output.with_suffix(".roles.tmp")
    with args.games.open() as games, tmp.open("w") as output:
        for line in games:
            game = json.loads(line)
            for team, players in game["rosters"].items():
                for player in players:
                    key = (game["game_id"], team, player.get("player_id") or player["player"])
                    if not player.get("role") and key in observations:
                        player["role"], player["role_source"] = observations[key]
                    counts[player.get("role") or "unknown"] += 1
            output.write(json.dumps(game, separators=(",", ":")) + "\n")
    tmp.replace(args.output)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
