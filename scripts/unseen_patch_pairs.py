"""Lock and score the 2026-11-23 unseen patch pair confirmation."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
from subprocess import check_output

from fearless.adaptive_pairs import PAIR_WINDOWS
from fearless.event_forecaster import digest
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model_features import ChampionCatalog, _patch_key
from fearless.pair_trial import METHODS, ROOT, PairTrial, validate_prefix
from scripts.compare_challengers import context, write_json
from scripts.evaluate_adaptation import cache_history, windows
from scripts.event_drafts import CARGO, TABLES, Cohort, cached_table, cargo_query, normalize
from scripts.event_rosters import attach, fetch_players, player_history
from scripts.pair_trial import ARTIFACTS, load_freeze, read, summarize, truth_roles

TRIAL_COMMIT = "81af126b8582162b587096878e312cc97274a5c8"
PROTOCOL = Path(__file__).resolve().parents[1] / "docs" / "unseen-patch-pairs.md"
FREEZE_SHA = "6807bd977a799e4836941c6985409602af2150f25ea95c2b74a976c3d7f6b263"
REFERENCES = {
    "grid": (1741, "754f944da83653d4da5953ccb11fcf56b5651c6c28550f55584ffdf1948a6b73"),
    "known": (599, "812f36be75c106a90c5e58840ee1c6f9d4bd3b003dbd80b1c9c045de1bf29725"),
    "korea": (101, "6215b473b79425f87c5da1ed6c904a7936e25141318e5a65cfadd657577ed7d2"),
}
PATCH = "16.18"
STAGES = {
    "2026 World Star Challengers Invitational": ("WSCI 2026", "International"),
    "2026 Demacia Cup Global Invitational": ("Demacia Cup Global Invitational 2026", "International"),
}
RULES = {"International": [
    "https://www.leagueoflegends.com/en-us/news/esports/world-star-challengers-invitational-2026/",
    "https://www.huya.com/video/play/1127047248.html",
    "https://navi.gg/en/tournaments/2026-demacia-cup-global-invitational/groups",
]}
COHORT = Cohort(STAGES, RULES, "2026-05-01", before="2026-11-23", previous_holdouts=2)


def frozen_trial(freeze):
    head = check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if head != TRIAL_COMMIT or ROOT != Path.cwd().resolve() or digest(freeze) != FREEZE_SHA:
        raise ValueError("Run in the frozen predictor checkout with the original freeze manifest.")
    return load_freeze(freeze)


def store_once(path, data):
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Existing registered material differs: {path}")
    else:
        with path.open("xb") as stream:
            stream.write(data)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def checked_reference(label, path):
    expected, sha = REFERENCES[label]
    if digest(path) != sha:
        raise ValueError(f"Unexpected {label} reference fingerprint.")
    rows = read_event_games(path)
    if len(rows) != expected:
        raise ValueError(f"Unexpected {label} reference count.")
    return rows


def patches_in(rows):
    return {_patch_key(str(game.get("patch", ""))) for game in rows} - {None}


def counts(rows, field):
    sets = defaultdict(set)
    for game in rows:
        sets[game[field]].add(game["series_id"])
    return {key: {"games": games, "series": len(sets[key])}
            for key, games in sorted(Counter(game[field] for game in rows).items())}


def binding(manifest, pools, games):
    return {"version": 1, "models": manifest["models"], "trial_commit": TRIAL_COMMIT,
            "freeze_sha256": FREEZE_SHA, "games_sha256": digest(games), "frozen_pools_sha256": digest(pools),
            "protocol_sha256": digest(PROTOCOL), "evaluator_sha256": digest(Path(__file__))}


def prepare(args):
    manifest, paths = frozen_trial(args.freeze)
    archive, known, korea = (checked_reference(label, path) for label, path in
                            (("grid", args.archive), ("known", paths["events"]), ("korea", args.korea)))
    observed = patches_in((*archive, *known, *korea))
    pools, frozen = read(args.source_catalog), read(paths["pools"])
    if set(pools) != {PATCH} or set(pools[PATCH]) != set(frozen[PATCH]) or _patch_key(PATCH) in observed:
        raise ValueError("Candidate patch was already observed or its exact pool differs from the freeze.")
    args.work.mkdir(mode=0o700, parents=True, exist_ok=True)
    wire = args.work / "wire"
    wire.mkdir(mode=0o700, exist_ok=True)
    tables = {key: cached_table(wire, key, COHORT) for key in TABLES}
    if any({row["OverviewPage"] for row in records} != STAGES.keys() for records in tables.values()):
        raise ValueError("Incomplete registered stage set in source tables.")
    rows, audit = normalize(tables, archive, known, pools, cohort=COHORT, additional_previous=(korea,))
    groups = completed_series(rows)
    if not groups or any({game["patch"] for game in group} != {PATCH} for group in groups):
        raise ValueError("Expected complete series on the registered unseen patch.")
    players = wire / "players.json"
    if not players.exists():
        fetch_players(STAGES, players)
    by_game = defaultdict(list)
    for row in read(players):
        by_game[row["GameId"]].append(row)
    catalog = ChampionCatalog.from_json(pools)
    player_ids = player_history(archive)
    rows = [attach(game, by_game[game["game_id"]], catalog, player_ids) for game in rows]
    scores = {row["GameId"]: row for row in tables["schedule"]}
    first_side = Counter("blue" if scores[game["game_id"]]["FirstPick"] == scores[game["game_id"]]["Blue"]
                         else "red" for game in rows)
    audit.update(source="Leaguepedia, CC BY-SA 3.0", cargo_url=CARGO,
                 cargo_queries={key: cargo_query(key, COHORT) for key in TABLES},
                 exclusions_by_reason=dict(Counter(reason.split(":")[0] for reason in audit["excluded"].values())))
    games = args.work / "games.jsonl"
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode()
    store_once(games, data)
    private_audit = args.work / "audit.json"
    store_once(private_audit, json_bytes(audit))
    lock = {**binding(manifest, paths["pools"], games), "source_catalog_sha256": digest(args.source_catalog),
            "audit_sha256": digest(private_audit), "source_sha256": {p.name: digest(p) for p in sorted(wire.glob("*.json"))},
            "reference_sha256": {key: value[1] for key, value in REFERENCES.items()},
            "source_games": audit["source_games"], "source_series": audit["source_series"],
            "games": len(rows), "series": len(groups), "by_patch": counts(rows, "patch"), "by_event": counts(rows, "event"),
            "verified_roles": sum(bool(g.get("rosters")) for g in rows), "first_pick_map_side": first_side,
            "completion_dates": [completion_day(groups[0]), completion_day(groups[-1])]}
    store_once(args.lock, json_bytes(lock))
    print(json.dumps({k: lock[k] for k in ("by_patch", "by_event", "verified_roles", "first_pick_map_side")}, indent=2))


def replay_arm(paths, groups, *, rolling):
    rows = windows(groups)
    trial = PairTrial.load(**{k: paths[k] for k in ARTIFACTS if k != "pools"}, extra=rows if rolling else ())
    pools = read(paths["pools"])
    advance = cache_history(trial.frozen)
    first_release = {}
    for group in groups:
        first_release.setdefault(group[0]["patch"], completion_day(group))
    previous, results = None, []
    for game in rows:
        day = game["date"]
        if previous != day:
            advance(day)
            previous = day
        ctx = context(game, pools)
        validate_prefix(ctx, game["actions"])
        for slot in PAIR_WINDOWS:
            prefix = game["actions"][:slot]
            forecast, roles = trial.predict(ctx, prefix, event=game["event"], series_id=game["series_id"], include_roles=True)
            actions = game["actions"][slot:slot + 2]
            pair = tuple(sorted(a["champion"] for a in actions))
            if len({a["team"] for a in actions}) != 1 or {a["kind"] for a in actions} != {"pick"}:
                raise ValueError("Unexpected target window.")
            label = truth_roles(game, actions[0]["team"], pair)
            role_hit = label in [tuple(sorted(r["roles"])) for r in roles[:3]] if label else None
            results.append({"event": game["event"], "patch": game["patch"], "series": game["series_id"],
                            "window": slot, "role_hit": role_hit, "cold": not rolling or day <= first_release[game["patch"]],
                            "ranks": {name: forecast[name].index(pair) + 1 if pair in forecast[name] else 0 for name in METHODS}})
    return results


def arm_summary(rows):
    sections = {f"by_{field}": {key: summarize([row for row in rows if row[field] == key])
                                 for key in sorted({row[field] for row in rows})} for field in ("patch", "event", "window")}
    stratified = [{**row, "event": f"{row['patch']}::{row['event']}"} for row in rows]
    return {**sections, "overall": summarize(stratified), "no_prior_patch_turns": sum(row["cold"] for row in rows)}


def replay(args):
    manifest, paths = frozen_trial(args.freeze)
    lock = read(args.lock)
    rows = read_event_games(args.games)
    groups = completed_series(rows)
    if (not groups or any(lock.get(k) != v for k, v in binding(manifest, paths["pools"], args.games).items())
            or lock["games"] != len(rows) or lock["series"] != len(groups)
            or lock["by_patch"] != counts(rows, "patch") or lock["by_event"] != counts(rows, "event")):
        raise ValueError("Cohort or frozen predictors differ from the registry.")
    arms = {}
    for label, rolling in (("strict_cold", False), ("rolling", True)):
        arms[label] = arm_summary(replay_arm(paths, groups, rolling=rolling))
        print(f"{label} complete", flush=True)
    coverage = (len(lock["by_patch"]) >= 3 and len(groups) >= 24
                and all(patch["series"] >= 2 for patch in lock["by_patch"].values()))
    lift = all(ci[0] > 0 for arm in arms.values() for ci in arm["overall"]["delta_ci95"].values())
    report = {"evaluation": "locked retrospective unseen patch confirmation", "lock_sha256": digest(args.lock),
              "games": len(rows), "series": len(groups), "by_patch": lock["by_patch"], "models": lock["models"],
              "legal": True, "arms": arms, "passed_coverage": coverage, "passed_lift": lift,
              "passed_promotion_gate": coverage and lift}
    write_json(args.output, report)
    print(json.dumps({"overall": {name: arm["overall"] for name, arm in arms.items()},
                      "passed_promotion_gate": report["passed_promotion_gate"]}, indent=2))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, fields, fn in (("prepare", ("freeze", "archive", "korea", "source-catalog", "work", "lock"), prepare),
                             ("replay", ("freeze", "games", "lock", "output"), replay)):
        command = commands.add_parser(name)
        for field in fields:
            command.add_argument(f"--{field}", type=Path, required=True)
        command.set_defaults(run=fn)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
