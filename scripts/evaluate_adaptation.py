"""Select on Worlds, lock artifacts, then confirm a private draft recipe on unopened events."""
import argparse
from collections import Counter, defaultdict
from itertools import groupby
import json
from math import fsum, log
from os.path import relpath
from pathlib import Path

import numpy as np

from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_forecaster import EventForecaster, RECIPES, digest
from fearless.event_series import completed_series, read_event_games
from fearless.model_features import History, evidence_date
from fearless.rules import legal_champions
from scripts.compare_challengers import BOOTSTRAP_SEED, context, distribution, pick_pairs, write_json


def windows(series):
    games = (g for s in series for g in s)
    return sorted(games, key=lambda g: (g["date"], g["drafted_at"], g["game_id"]))


def cache_history(model):
    history = History(model.catalog)
    pos = 0
    def advance(day):
        nonlocal pos
        start = pos
        while pos < len(model.games) and evidence_date(model.games[pos]) < day:
            pos += 1
        history.before(day, model.games[start:pos])
        model._histories.clear()
        model._histories[day] = history
    return advance


def quality(probs, target):
    names = list(probs)
    return (int(target in names[:3]), -log(probs[target]), probs[names[0]], int(names[0] == target))


def metrics(values):
    bins = defaultdict(list)
    for _, _, confidence, right in values:
        bins[min(9, int(10 * confidence))].append(confidence - right)
    return dict(turns=len(values), hits=sum(v[0] for v in values), hit3=fsum(v[0] for v in values)/len(values),
                nll=fsum(v[1] for v in values)/len(values),
                ece=fsum(abs(fsum(v)) for v in bins.values())/len(values))


def score_summary(rows):
    report = {}
    for k in rows[0][2]:
        report[k] = metrics([row[2][k] for row in rows])
    return report


def intervals(rows, field):
    groups = defaultdict(lambda: defaultdict(list))
    for event, sid, values in rows:
        groups[event][sid].append(values["candidate"][field] - values["incumbent"][field])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    samples = np.zeros((5000, 2))
    for event in sorted(groups):
        series = groups[event]
        totals = np.array([(len(series[s]), fsum(series[s])) for s in sorted(series)])
        samples += totals[rng.integers(0, len(totals), size=(5000, len(totals)))].sum(axis=1)
    return np.quantile(samples[:, 1] / samples[:, 0], [.025, .975]).tolist()


def top_pairs(model, ctx, prefix, event, sid, original):
    alternatives = model.rank_pairs(**ctx, current=prefix, event=event, series_id=sid, limit=3)
    if not alternatives:
        return original
    legal = set(legal_champions(ctx["pool"], ctx["locked"], prefix))
    pairs = [tuple(sorted(row["champions"])) for row in alternatives]
    if (not 1 <= len(pairs) <= 3 or len(pairs) != len(set(pairs))
            or any(len(p) != 2 or p[0] == p[1] or not set(p) <= legal for p in pairs)):
        raise ValueError("Invalid candidate pair shortlist.")
    return pairs


def replay(parent, series, catalog, models, *, pairs=False):
    picks, doubles, coverage = [], [], Counter()
    rankers = {id(model): model for model in [parent.frozen, *(m.updated for m in models.values() if m.updated)]}
    caches = [cache_history(r) for r in rankers.values()]
    for day, games in groupby(windows(series), key=lambda g: g["date"]):
        for advance in caches:
            advance(day)
        for game in games:
            sid, event = game["series_id"], game["event"]
            ctx = context(game, catalog)
            enhanced = dict(**ctx, event=event, series_id=sid)
            for i, action in enumerate(game["actions"]):
                prefix = game["actions"][:i]
                if action["kind"] != "pick":
                    if pairs:
                        # The selected composition must continue delegating every ban to its parent.
                        before = parent.forecast(**ctx, current=prefix)
                        after = models["candidate"].forecast(**enhanced, current=prefix)
                        if before != after:
                            raise ValueError("An event component modified the existing ban policy.")
                    continue
                target = action["champion"]
                base, _ = distribution(parent, ctx, prefix)
                row = {"incumbent": quality(base, target)}
                for name, model in models.items():
                    probs, meta = distribution(model, enhanced, prefix)
                    row[name] = quality(probs, target)
                    coverage[name, meta["method"]] += 1
                picks.append((event, sid, row))
                if pairs and i + 1 < len(game["actions"]):
                    other = game["actions"][i + 1]
                    if (other["team"], other["kind"]) == (action["team"], "pick"):
                        observed = tuple(sorted((target, other["champion"])))
                        original = pick_pairs(parent, ctx, prefix)
                        ranked = top_pairs(models["candidate"], ctx, prefix, event, sid, original)
                        doubles.append((event, sid, {"incumbent": (int(observed in original),),
                                                     "candidate": (int(observed in ranked),)}))
    return picks, doubles, {k: {method: n for (label, method), n in coverage.items() if label == k} for k in models}


def artifact(path):
    return {"path": str(path), "sha256": digest(path)}


def verified_rows(path, audit, *, worlds=False):
    info = json.loads(audit.read_text())
    if digest(path) != info["sha256"]:
        raise ValueError("Event file differs from its ingestion audit.")
    rows = read_event_games(path)
    if worlds:
        rows = [{**g, "event": "Worlds 2026"} for g in rows]
    if any(not g.get("event") for g in rows):
        raise ValueError("Every game must have a resolved event identity.")
    series = completed_series(rows)
    if len(series) != info["series"] or len(rows) != info["games"]:
        raise ValueError("Event totals disagree with their audit.")
    return rows, series


def pick_model(path, bundle):
    from fearless.patch_picks import PatchPickModel

    return PatchPickModel.load(path, bundle)


def pair_model(path, bundle):
    from fearless.adaptive_pairs import AdaptivePairModel

    return AdaptivePairModel.load(path, bundle)


def develop(args, parent, catalog):
    rows, series = verified_rows(args.games, args.audit, worlds=True)
    if (len(series), len(rows)) != (46, 98):
        raise ValueError("Expected the complete Worlds development cohort.")
    fusion = pick_model(args.pick, args.bundle) if args.pick else None
    recipes = RECIPES[1:] if fusion else ("refresh",)
    models = {r: EventForecaster(parent, rows, recipe=r, pick=fusion if "fusion" in r else None) for r in recipes}
    picks, _, coverage = replay(parent, series, catalog, models)
    scores = score_summary(picks)
    best = min((r for r in ("incumbent", *recipes) if scores[r]["nll"] <= scores["incumbent"]["nll"]),
               key=lambda r: (-scores[r]["hits"], scores[r]["nll"], ("incumbent", *recipes).index(r)))
    selection = "fixed" if best == "incumbent" else best
    report = {"recipe": selection, "pick_scores": scores, "coverage": coverage, "worlds_sha256": digest(args.games),
              "incumbent": incumbent_signature(args.bundle), "parent_sha256": digest(args.parent),
              "artifacts": {key: digest(path) for key, path in (("pick", args.pick), ("pair", args.pair)) if path},
              "finalized": bool(args.pick and args.pair)}
    write_json(args.output, report)
    print(json.dumps(report, indent=2))


def confirm(args, parent, catalog):
    choice = json.loads(args.selection.read_text())
    if (not choice["finalized"] or choice["incumbent"] != incumbent_signature(args.bundle)
            or choice["parent_sha256"] != digest(args.parent)
            or choice["artifacts"] != {"pick": digest(args.pick), "pair": digest(args.pair)}):
        raise ValueError("Recipes or parameter artifacts differ from development selection.")
    rows, series = verified_rows(args.games, args.audit)
    if any(g["event"] == "Worlds 2026" for g in rows):
        raise ValueError("The development event is in confirmation data.")
    recipe = choice["recipe"]
    model = EventForecaster(parent, rows, recipe=recipe, pick=pick_model(args.pick, args.bundle) if "fusion" in recipe else None,
                            pair=pair_model(args.pair, args.bundle))
    picks, pairs, coverage = replay(parent, series, catalog, {"candidate": model}, pairs=True)
    if len(picks) != len(rows) * 10 or len(pairs) != len(rows) * 3:
        raise ValueError("Draft opportunity totals differ from the complete event schedule.")
    def summarize(ps, ds):
        return {"pick": score_summary(ps), "pairs": {"turns": len(ds),
                **{k: sum(row[2][k][0] for row in ds) for k in ("incumbent", "candidate")}}}
    events = sorted({row[0] for row in picks})
    scores = summarize(picks, pairs)
    by_event = {e: summarize([r for r in picks if r[0] == e], [r for r in pairs if r[0] == e]) for e in events}
    ci = {"pick_hit3": intervals(picks, 0), "pick_nll": intervals(picks, 1), "pair_hit3": intervals(pairs, 0)}
    passed = {"pick": ci["pick_hit3"][0] > 0 and scores["pick"]["candidate"]["nll"] <= scores["pick"]["incumbent"]["nll"],
              "pair": ci["pair_hit3"][0] > 0}
    report = {"recipe": recipe, "promoted": passed, "overall": scores, "by_event": by_event, "coverage": coverage,
              "intervals": ci, "games": len(rows), "series": len(series), "sha256": digest(args.games),
              "selection_sha256": digest(args.selection), "unchanged_bans": 10*len(rows), "illegal_predictions": 0}
    write_json(args.output, report)
    print(json.dumps(report, indent=2))


def package(args, parent, catalog):
    choice = json.loads(args.selection.read_text())
    result = json.loads(args.result.read_text())
    if (result["selection_sha256"] != digest(args.selection) or choice["parent_sha256"] != digest(args.parent)
            or choice["incumbent"] != incumbent_signature(args.bundle)):
        raise ValueError("Confirmation and selected policy disagree.")
    _, series = verified_rows(args.games, args.audit)
    files = {"events": args.games}
    recipe = choice["recipe"] if result["promoted"]["pick"] else "fixed"
    if "fusion" in recipe:
        files["pick"] = args.pick
    if result["promoted"]["pair"]:
        files["pair"] = args.pair
    if any(digest(path) != choice["artifacts"][key] for key, path in files.items() if key != "events"):
        raise ValueError("A fitted model differs from its confirmation artifact.")
    policy = {"version": 1, "incumbent": choice["incumbent"], "parent_sha256": choice["parent_sha256"],
              "recipe": recipe, "files": {key: {**artifact(path), "path": relpath(path.resolve(), args.output.parent.resolve())}
                                        for key, path in files.items()}}
    write_json(args.output, policy)
    print(f"Packaged {len(series)} completed series, pick recipe {recipe}, pair model {'pair' in files}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    for name in ("develop", "confirm", "package"):
        cmd = cmds.add_parser(name)
        for arg in ("bundle", "parent", "games", "audit", "catalog", "output", "pick", "pair"):
            cmd.add_argument("--" + arg, type=Path, required=arg not in ("pick", "pair") or name != "develop")
        if name in ("confirm", "package"):
            cmd.add_argument("--selection", type=Path, required=True)
        if name == "package":
            cmd.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    parent = DraftChallengers.load(args.bundle, args.parent)
    {"develop": develop, "confirm": confirm, "package": package}[args.command](args, parent, json.loads(args.catalog.read_text()))


if __name__ == "__main__":
    main()
