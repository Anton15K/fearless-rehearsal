"""Freeze pair predictors, replay a locked cohort, or log real shadow forecasts."""
import argparse
from collections import Counter
import json
from os.path import relpath
from pathlib import Path

from fearless.adaptive_pairs import PAIR_WINDOWS
from fearless.event_forecaster import digest
from fearless.event_series import completed_series, read_event_games
from fearless.model_features import ROLES
from fearless.pair_trial import METHODS, ROOT, PairTrial, identities
from scripts.compare_challengers import context, write_json
from scripts.evaluate_adaptation import cache_history, intervals, windows

ARTIFACTS = ("bundle", "parent", "adaptive", "roles", "events", "pools")
PROTOCOL = ROOT / "docs" / "role-pair-trial.md"


def read(path):
    return json.loads(Path(path).read_text())


def freeze(args):
    paths = {k: getattr(args, k).resolve() for k in ARTIFACTS}
    models = identities(**paths)
    value = {"version": 1, "protocol_sha256": digest(PROTOCOL), "models": models,
             "files": {key: relpath(path, args.output.resolve().parent) for key, path in paths.items()}}
    if args.output.exists() and read(args.output) != value:
        raise ValueError("A different freeze already exists at this location.")
    write_json(args.output, value)
    print(json.dumps(models, indent=2))


def load_freeze(path):
    data = read(path)
    files = {k: path.resolve().parent / data["files"][k] for k in ARTIFACTS}
    if (data["version"] != 1 or data["models"] != identities(**files)
            or data["protocol_sha256"] != digest(PROTOCOL)):
        raise ValueError("Inputs, predictor implementation or protocol differ from the frozen trial.")
    return data, files


def lock_study(args):
    manifest, paths = load_freeze(args.freeze)
    cohort = read(args.cohort)
    source_pools, serving = read(args.source_catalog), read(paths["pools"])
    if (cohort["source_catalog_sha256"] != digest(args.source_catalog)
            or any(set(names) != set(serving.get(patch, [])) for patch, names in source_pools.items())):
        raise ValueError("The reserved champion pool differs from the trial.")
    value = {**cohort, "models": manifest["models"], "freeze_sha256": digest(args.freeze)}
    if args.output.exists() and read(args.output) != value:
        raise ValueError("The confirmation lock differs from the existing lock.")
    write_json(args.output, value)


def truth_roles(game, team, target):
    roster = game.get("rosters", {}).get(team, [])
    mapping = {r.get("champion"): r.get("role") for r in roster}
    assigned = tuple(sorted(mapping[c] for c in target if mapping.get(c) in ROLES))
    return assigned if len(set(assigned)) == 2 else None


def summarize(rows):
    def scores(name):
        rank = [row["ranks"][name] for row in rows]
        n = len(rank)
        return dict(turns=n, hit1=sum(i == 1 for i in rank), hits=sum(i > 0 for i in rank),
                    hit3=sum(i > 0 for i in rank) / n, mrr3=sum(1 / i for i in rank if i) / n)
    comparisons = {}
    for name in ("adaptive", "frequency"):
        paired = [(r["event"], r["series"], {"candidate": (int(r["ranks"]["role_first"] > 0),),
                   "incumbent": (int(r["ranks"][name] > 0),)}) for r in rows]
        comparisons[name] = intervals(paired, 0)
    labeled = [row for row in rows if row["role_hit"] is not None]
    return {"scores": {name: scores(name) for name in METHODS}, "delta_ci95": comparisons,
            "role_top3": {"hits": sum(r["role_hit"] for r in labeled), "labeled": len(labeled)}}


def replay(args):
    manifest, paths = load_freeze(args.freeze)
    audit = read(args.audit)
    rows = read_event_games(args.games)
    grouped = completed_series(rows)
    events = Counter(g["event"] for g in rows)
    if audit["sha256"] != digest(args.games) or (audit["games"], audit["series"]) != (len(rows), len(grouped)):
        raise ValueError("Cohort membership disagrees with the audit.")
    known_rows = {g["game_id"]: g for g in read_event_games(paths["events"])}
    if args.lock:
        lock = read(args.lock)
        if (lock["games_sha256"] != digest(args.games) or lock["audit_sha256"] != digest(args.audit)
                or lock["models"] != manifest["models"] or lock["events"] != dict(events)
                or lock["freeze_sha256"] != digest(args.freeze) or set(known_rows).intersection(g["game_id"] for g in rows)):
            raise ValueError("Independent cohort or models disagree with the frozen lock.")
    elif not all(known_rows.get(g["game_id"]) == g for g in rows):
        raise ValueError("Development replay accepts only previously examined rows.")
    trial = PairTrial.load(**{k: paths[k] for k in ARTIFACTS if k != "pools"}, extra=rows if args.lock else ())
    pools = read(paths["pools"])
    results = []
    advance = cache_history(trial.frozen)
    previous = None
    for game in windows(grouped):
        if game["date"] != previous:
            advance(game["date"])
            previous = game["date"]
        ctx = context(game, pools)
        for slot in PAIR_WINDOWS:
            forecasts, roles = trial.predict(ctx, game["actions"][:slot], event=game["event"],
                                               series_id=game["series_id"], include_roles=True)
            actions = game["actions"][slot:slot + 2]
            pair = tuple(sorted(a["champion"] for a in actions))
            team = actions[0]["team"]
            if actions[1]["team"] != team or any(a["kind"] != "pick" for a in actions):
                raise ValueError("Invalid target pair window.")
            assigned = truth_roles(game, team, pair)
            results.append({"event": game["event"], "series": game["series_id"], "window": slot,
                            "ranks": {k: v.index(pair) + 1 if pair in v else 0 for k, v in forecasts.items()},
                            "role_hit": assigned in [tuple(sorted(r["roles"])) for r in roles[:3]] if assigned else None})
    overall = summarize(results)
    report = {"evaluation": "locked retrospective" if args.lock else "development diagnostic",
              "games": len(rows), "series": len(grouped), "games_sha256": digest(args.games),
              "freeze_sha256": digest(args.freeze), "lock_sha256": digest(args.lock) if args.lock else None,
              "models": manifest["models"], "overall": overall,
              "by_event": {e: summarize([r for r in results if r["event"] == e]) for e in sorted(events)},
              "by_window": {s: summarize([r for r in results if r["window"] == s]) for s in PAIR_WINDOWS},
              "passed_retrospective_gate": all(ci[0] > 0 for ci in overall["delta_ci95"].values()) if args.lock else None}
    write_json(args.output, report)
    print(json.dumps({"games": len(rows), "overall": overall, "criterion": report["passed_retrospective_gate"]}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("freeze")
    for key in (*ARTIFACTS, "output"):
        build.add_argument("--" + key, type=Path, required=True)
    build.set_defaults(func=freeze)
    lock = sub.add_parser("lock")
    for key in ("freeze", "cohort", "source-catalog", "output"):
        lock.add_argument("--" + key, type=Path, required=True)
    lock.set_defaults(func=lock_study)
    study = sub.add_parser("replay")
    for key in ("freeze", "games", "audit", "output"):
        study.add_argument("--" + key, type=Path, required=True)
    group = study.add_mutually_exclusive_group(required=True)
    group.add_argument("--lock", type=Path)
    group.add_argument("--development", action="store_true")
    study.set_defaults(func=replay)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
