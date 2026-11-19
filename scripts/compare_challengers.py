"""Replay frozen private challengers and apply the separate whole-series promotion gates."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import groupby
import json
from math import fsum, isclose, log
from os.path import relpath
from pathlib import Path

import numpy as np

from fearless.calibration import CalibrationSidecar, ScoredPick, bundle_fingerprint
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.model import DraftForecaster
from fearless.model_features import History, evidence_date, prepare_games
from fearless.outlook import lookahead
from fearless.rules import legal_champions, schedule

REFRESH_THROUGH = "2026-10-25"
BOOTSTRAP_SEED = 10429


def json_file(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def write_policy(bundle, output, components):
    write_json(output, {"version": 1, "incumbent": incumbent_signature(bundle),
                        "components": {key: relpath(path.resolve(), output.parent.resolve())
                                       for key, path in components.items()}})


def whole_games(path, frozen):
    games = prepare_games(frozen.catalog.normalize_game(json.loads(line))
                          for line in path.read_text().splitlines() if line.strip())
    if not games or min(g["date"] for g in games) <= max(map(evidence_date, frozen.games)):
        raise ValueError("Confirmation games must be newer than the frozen history.")
    dates = defaultdict(list)
    members = defaultdict(list)
    for g in games:
        if not g.get("eligible") or not g.get("finished"):
            raise ValueError("Confirmation requires eligible, finished games.")
        if not g.get("finished_at"):
            raise ValueError("A verified completion date is required for every confirmation game.")
        dates[g["series_id"]].extend([g["date"], evidence_date(g)])
        members[g["series_id"]].append(g)
    for group in members.values():
        if ({g.get("series_games") for g in group} != {len(group)}
                or sorted(g["game_number"] for g in group) != list(range(1, len(group) + 1))
                or len({g["game_id"] for g in group}) != len(group)):
            raise ValueError("Incomplete series in confirmation data.")
    if any(min(days) <= REFRESH_THROUGH < max(days) for days in dates.values()):
        raise ValueError("Series spanning the refresh cutoff cannot be split.")
    return games


def dated_games(games, frozen):
    # Cache the production history incrementally. Both methods use the same evidence.
    history = History(frozen.catalog)
    source = iter(frozen.games)
    pending = next(source, None)
    for day, matches in groupby(games, lambda game: game["date"]):
        previous = []
        while pending is not None and evidence_date(pending) < day:
            previous.append(pending)
            pending = next(source, None)
        history.before(day, previous)
        frozen._histories.clear()
        frozen._histories[day] = history
        yield from matches


def context(game, catalog):
    return dict(date=game["date"], patch=game["patch"], region=game["region"], first=game["first"],
                second=game["second"], game_number=game["game_number"], locked=game["locked"],
                pool=[c for c in catalog[game["patch"]] if c not in game["disabled"]], fearless=True)


def distribution(forecaster, ctx, prefix):
    pool = legal_champions(ctx["pool"], ctx["locked"], prefix)
    result = forecaster.forecast(**ctx, current=prefix, limit=len(pool))
    choices = result["choices"]
    probabilities = {row["champion"]: row["probability"] for row in choices}
    values = list(probabilities.values())
    if (len(choices) != len(pool) or set(probabilities) != set(pool) or min(values) <= 0
            or not isclose(fsum(values), 1.0, abs_tol=1e-7) or values != sorted(values, reverse=True)):
        raise ValueError("Invalid full legal distribution.")
    return probabilities, result


def refresh(args):
    frozen = DraftForecaster.load(args.bundle)
    games = whole_games(args.games, frozen)
    fit_games = [g for g in games if g["date"] <= REFRESH_THROUGH]
    end = {}
    for game in fit_games:
        sid = game["series_id"]
        end[sid] = max(end.get(sid, ""), evidence_date(game))
    catalog = json_file(args.catalog)
    def picks():
        for game in dated_games(fit_games, frozen):
            for i, action in enumerate(game["actions"]):
                if action["kind"] == "pick":
                    probs, _ = distribution(frozen, context(game, catalog), game["actions"][:i])
                    target = list(probs).index(action["champion"])
                    yield ScoredPick(game["date"], end[game["series_id"]], game["patch"], game["series_id"],
                                     game["game_id"], "refresh", tuple(probs.values()), target, position=i)
    old = CalibrationSidecar.load(args.previous, model_id=bundle_fingerprint(args.bundle))
    updated = old.refreshed(picks(), through=REFRESH_THROUGH)
    updated.save(args.output)


def pick_pairs(forecaster, ctx, prefix):
    ranker = getattr(forecaster, "rank_pairs", None)
    if ranker is not None:
        alternatives = ranker(**ctx, current=prefix, limit=3)
        if alternatives:
            output = [tuple(sorted(p["champions"])) for p in alternatives]
            legal = set(legal_champions(ctx["pool"], ctx["locked"], prefix))
            if (len(output) > 3 or len(set(output)) != len(output)
                    or any(len(pair) != 2 or pair[0] == pair[1] or not set(pair) <= legal for pair in output)):
                raise ValueError("Illegal or duplicate pair alternative.")
            return output
    actor, _ = schedule(ctx["first"], ctx["second"])[len(prefix)]
    fallback = getattr(forecaster, "frozen", forecaster)
    result = lookahead(pool=ctx["pool"], locked=ctx["locked"], current=prefix, first=ctx["first"],
                       second=ctx["second"], coach_team=ctx["second"] if ctx["first"] == actor else ctx["first"],
                       forecast=lambda p: fallback.forecast(**ctx, current=p, limit=3))
    if not result["pairs"]:
        raise ValueError("Expected a double-pick opportunity.")
    return [tuple(x["champions"]) for x in result["pairs"][:3]]


def ci(series_values):
    totals = np.array([(len(series_values[key]), fsum(series_values[key])) for key in sorted(series_values)])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    sums = totals[rng.integers(0, len(totals), size=(5000, len(totals)))].sum(axis=1)
    return np.quantile(sums[:, 1] / sums[:, 0], [.025, .975]).tolist()


def compare(rows, is_pair=False):
    summary = {}
    for idx, label in enumerate(("incumbent", "candidate")):
        outcomes = [row[1 + idx] for row in rows]
        summary[label] = {"hit3": sum(r[0] for r in outcomes) / len(outcomes)}
        if not is_pair:
            bins = defaultdict(list)
            for _, nll, confidence, hit1 in outcomes:
                bins[min(int(confidence * 10), 9)].append(confidence - hit1)
            summary[label].update(nll=fsum(r[1] for r in outcomes) / len(outcomes),
                                  ece=fsum(abs(fsum(bucket)) for bucket in bins.values()) / len(outcomes))
    for index, metric in [(0, "hit3")] if is_pair else [(0, "hit3"), (1, "nll")]:
        by_series = defaultdict(list)
        for sid, left, right in rows:
            by_series[sid].append(right[index] - left[index])
        summary[metric + "_delta_ci95"] = ci(by_series)
    return {**summary, "turns": len(rows), "series": len({row[0] for row in rows})}


def score(args):
    candidate = DraftChallengers.load(args.bundle, args.policy)
    old = candidate.frozen
    games = whole_games(args.games, old)
    catalog = json_file(args.catalog)
    rows = defaultdict(list)
    all_turns = defaultdict(list)
    calibration_counts = Counter()
    for game in dated_games(games, old):
        ctx = context(game, catalog)
        sid = game["series_id"]
        for i, action in enumerate(game["actions"]):
            kind, target = action["kind"], action["champion"]
            prefix = game["actions"][:i]
            following = game["actions"][i + 1:i + 2]
            if kind == "pick" and following and (following[0]["team"], following[0]["kind"]) == (action["team"], "pick"):
                pair = tuple(sorted((target, game["actions"][i + 1]["champion"])))
                a = pick_pairs(old, ctx, prefix)
                b = pick_pairs(candidate, ctx, prefix)
                rows["pair"].append((sid, (int(pair in a),), (int(pair in b),)))
            left, _ = distribution(old, ctx, prefix)
            right, meta = distribution(candidate, ctx, prefix)
            if kind == "ban" and i < 12 and left != right:
                raise ValueError("Opening ban forecast changed.")
            if not target:
                continue
            all_turns[kind].append((int(target in list(left)[:3]), int(target in list(right)[:3])))
            if kind == "ban" and i < 12:
                continue
            if kind == "pick":
                if list(left) != list(right):
                    raise ValueError("Pick ordering changed under temperature correction.")
                if game["date"] <= REFRESH_THROUGH:
                    continue
                calibration_counts["adjusted" if meta.get("pick_calibration") else "identity"] += 1
            def quality(probs):
                names = list(probs)
                return int(target in names[:3]), -log(probs[target]), probs[names[0]], int(names[0] == target)
            rows[kind].append((sid, quality(left), quality(right)))
    results = {key: compare(turns, is_pair=key == "pair") for key, turns in rows.items()}
    picks, bans = results["pick"], results["ban"]
    decisions = {"pair": results["pair"]["hit3_delta_ci95"][0] > 0,
                 "ban": bans["hit3_delta_ci95"][0] > 0 and bans["candidate"]["nll"] <= bans["incumbent"]["nll"],
                 "calibration": picks["nll_delta_ci95"][1] < 0 and picks["candidate"]["ece"] <= picks["incumbent"]["ece"]}
    report = dict(results=results, directions_passed=decisions, calibration_coverage=dict(calibration_counts),
                  cohort_hit3={kind: {"turns": len(turns), "incumbent_hits": sum(a for a, _ in turns),
                                      "candidate_hits": sum(b for _, b in turns)} for kind, turns in all_turns.items()},
                  games=len(games), series=len({g["series_id"] for g in games}), opening_bans_unchanged=True,
                  pick_order_unchanged=True, illegal_suggestions=0,
                  incumbent=incumbent_signature(args.bundle), cohort_sha256=sha256(args.games.read_bytes()).hexdigest())
    write_json(args.output, report)
    if args.selected_policy:
        supplied = json_file(args.policy)["components"]
        write_policy(args.bundle, args.selected_policy,
                     {key: args.policy.parent / value for key, value in supplied.items() if decisions[key]})
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("configure")
    for arg in ("bundle", "output"):
        build.add_argument("--" + arg, type=Path, required=True)
    for arg in ("pair", "ban", "calibration"):
        build.add_argument("--" + arg, type=Path)
    for name in ("refresh", "evaluate"):
        sub = commands.add_parser(name)
        for arg in ("bundle", "games", "catalog", "output", "previous" if name == "refresh" else "policy"):
            sub.add_argument("--" + arg, type=Path, required=True)
        if name == "evaluate":
            sub.add_argument("--selected-policy", type=Path)
    args = parser.parse_args()
    if args.command == "configure":
        write_policy(args.bundle, args.output, {name: getattr(args, name) for name in ("pair", "ban", "calibration")
                                               if getattr(args, name)})
    elif args.command == "refresh":
        refresh(args)
    else:
        score(args)


if __name__ == "__main__":
    main()
