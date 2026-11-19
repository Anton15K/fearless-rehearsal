"""Rebuild the openly licensed Cloud9 showcase and its earlier LCS comparison games."""
import csv
import io
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

REV = "447d68cd5b5e770aba90979be9efe6b0c398a724"
BASE = f"https://raw.githubusercontent.com/DeusExMachina1993/LoL-Draft/{REV}/2026"
SHOWCASE = "994442840205359781"
EVENTS = {"LCS 2026 Lock-In", "LCS 2026 Spring", "LCS 2026 Spring Playoffs"}


def rows(url, delimiter):
    with urllib.request.urlopen(url, timeout=60) as response:
        yield from csv.DictReader(io.TextIOWrapper(response, encoding="utf8"), delimiter=delimiter)


def main():
    destination = Path(__file__).resolve().parents[1] / "fearless/assets/public_games.jsonl"
    source_games = {
        row["match_id"]: row for row in rows(f"{BASE}/games.csv", ",")
        if row["tournament_name"] in EVENTS or row["series_id"] == SHOWCASE
    }
    actions = defaultdict(list)
    for row in rows(f"{BASE}/all_patches/all_patches.inter", "\t"):
        game = source_games.get(row["match_id:token"])
        if game is None:
            continue
        side = "blue" if row["user_id:token"].endswith("_0") else "red"
        actions[row["match_id:token"]].append((int(float(row["timestamp:float"])), {
            "team":game[side + "_team"], "kind":row["action_type:token"],
            "champion":row["hero_name:token"].replace("_", " "),
        }))
    result = []
    for match_id, game in source_games.items():
        side = game["first_side"]
        first, second = game[side + "_team"], game[("red" if side == "blue" else "blue") + "_team"]
        draft = [action for _, action in sorted(actions[match_id])]
        if len(draft) != 20:
            raise ValueError(f"Unexpected draft length in public source: {match_id}")
        result.append({
            "series_id":game["series_id"], "game_id":match_id, "game_number":int(game["game_number"]),
            "date":game["start_time_utc"][:10], "patch":f"16.{int(game['patch']) % 100}",
            "event":game["tournament_name"], "region":"NA", "first":first,"second":second,
            "team_names":{first:first,second:second},"actions":draft,"rosters":{},"fearless":True,
            "eligible":True, "finished":True, "source":"Leaguepedia / LoL-Draft (CC BY-SA 4.0)"
        })
    result.sort(key=lambda g: (g["date"],g["series_id"],g["game_number"]))
    destination.write_text("".join(json.dumps(r, separators=(",", ":"))+"\n" for r in result))


if __name__ == "__main__":
    main()
