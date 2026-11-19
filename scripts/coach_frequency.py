"""Replay a patch frequency baseline on an already scored draft cohort.

Single actions count all earlier named picks or bans of the matching kind.
Pairs count earlier unordered double picks across all three draft windows.
"""

import argparse
from collections import Counter, defaultdict
from itertools import groupby
import json
from pathlib import Path

from fearless.adaptive_pairs import AdaptivePairModel
from fearless.challengers import DraftChallengers
from fearless.event_forecaster import EventForecaster, digest, verify_new_games
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model import DraftForecaster
from fearless.model_features import _patch_key, evidence_date
from fearless.pair_model import pair_turns
from fearless.rules import legal_champions
from scripts.compare_challengers import context, pick_pairs
from scripts.evaluate_adaptation import cache_history, intervals, top_pairs


def archive_timeline(games):
    grouped = defaultdict(list)
    for game in games:
        grouped[game["series_id"]].append(game)
    return [(max(map(evidence_date, series)), game) for series in grouped.values() for game in series]


def counts():
    return {kind: Counter() for kind in ("pick", "ban", "pair")}


def update(table, actions):
    for action in actions:
        if action["champion"] is not None:
            table[action["kind"]][action["champion"]] += 1
    table["pair"].update(pair for _, _, pair in pair_turns(actions))


def summarize(rows):
    groups = defaultdict(Counter)
    for event, kind, source, hit, size in rows:
        for group in (groups["all", kind], groups[event, kind]):
            group["turns"] += 1
            group[source] += 1
            group["hits"] += hit
            group["shortlists_below_three"] += size < 3
    summary = defaultdict(dict)
    for (event, kind), row in groups.items():
        summary[event][kind] = {**row, "hit3": row["hits"] / row["turns"]}
    return dict(summary)


def pair_summary(rows):
    return {"turns": len(rows), "simple_hits": sum(row[2]["incumbent"][0] for row in rows),
            "adaptive_hits": sum(row[2]["candidate"][0] for row in rows), "adaptive_minus_simple_ci95": intervals(rows, 0)}


def replay(frozen, cohort, prior, pools, kind, *, candidate=None):
    events = completed_series([*prior, *cohort])
    verify_new_games(frozen, events)
    historical = archive_timeline(frozen.games)
    historical.extend((completion_day(series), game) for series in events for game in series)
    historical.sort(key=lambda entry: entry[0])
    canonical = frozen.catalog.canonical
    overall, patch_counts, patch_games = counts(), defaultdict(counts), Counter()
    rows, pair_comparison = [], []
    advance = cache_history(frozen) if candidate else None
    index = 0
    for day, games in groupby(sorted(cohort, key=lambda game: game["date"]), key=lambda game: game["date"]):
        if advance:
            advance(day)
        while index < len(historical) and historical[index][0] < day:
            _, raw = historical[index]
            game = frozen.catalog.normalize_game(raw)
            patch = _patch_key(game["patch"]) or game["patch"]
            update(overall, game["actions"])
            update(patch_counts[patch], game["actions"])
            patch_games[patch] += 1
            index += 1

        for raw in games:
            game = frozen.catalog.normalize_game(raw)
            patch = _patch_key(game["patch"]) or game["patch"]
            disabled = set(map(canonical, game.get("disabled", ())))
            pool = {canonical(name) for name in pools[game["patch"]]} - disabled
            actions, locks = game["actions"], game["locked"]
            event = game["event"]
            source = "same_patch" if patch_games[patch] else "all_earlier_patches"
            evidence = patch_counts[patch] if patch_games[patch] else overall
            for slot, action in enumerate(actions):
                if action["kind"] != kind:
                    continue
                target = action["champion"]
                if target is None and kind == "ban":
                    continue
                legal = legal_champions(pool, locks, actions[:slot])
                if target not in legal:
                    raise ValueError("A named target lies outside the dated game pool.")
                choices = sorted(legal, key=lambda name: (-evidence[kind][name], name))[:3]
                rows.append((event, kind, source, target in choices, len(choices)))
                if kind == "ban":
                    rows.append((event, "opening_ban" if slot < 6 else "late_ban", source, target in choices, len(choices)))
            if kind == "pick":
                ctx = context(raw, pools) if candidate else None
                for slot, _, pair in pair_turns(actions):
                    legal = set(legal_champions(pool, locks, actions[:slot]))
                    observed = (other for other in evidence["pair"] if set(other) <= legal)
                    choices = sorted(observed, key=lambda other: (-evidence["pair"][other], other))[:3]
                    rows.append((event, "pair", source, pair in choices, len(choices)))
                    if candidate:
                        prefix, sid = raw["actions"][:slot], game["series_id"]
                        original = pick_pairs(candidate.parent, ctx, prefix)
                        adaptive = top_pairs(candidate, ctx, prefix, event, sid, original)
                        recorded = tuple(sorted(action["champion"] for action in raw["actions"][slot:slot + 2]))
                        pair_comparison.append((event, sid, {"incumbent": (int(pair in choices),),
                                                            "candidate": (int(recorded in adaptive),)}))
    paired = {}
    if pair_comparison:
        paired = {"all": pair_summary(pair_comparison),
                  **{event: pair_summary([row for row in pair_comparison if row[0] == event])
                     for event in sorted({row[0] for row in pair_comparison})}}
    return summarize(rows), paired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "games", "lock", "catalog"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--prior-events", type=Path)
    parser.add_argument("--kind", choices=("pick", "ban"), required=True)
    parser.add_argument("--parent", type=Path, help="Serving policy for the optional exact-pair comparison")
    parser.add_argument("--pair-model", type=Path, help="Evaluated adaptive pair artifact")
    args = parser.parse_args()
    if bool(args.parent) != bool(args.pair_model) or args.pair_model and args.kind != "pick":
        parser.error("The optional pick-pair comparison needs both --parent and --pair-model.")
    lock = json.loads(args.lock.read_text())
    if digest(args.games) != lock["sha256"] or digest(args.catalog) != lock["catalog_sha256"]:
        raise ValueError("The cohort or legal champion catalog differs from its lock.")
    cohort = read_event_games(args.games)
    if (len(cohort), len(completed_series(cohort))) != (lock["games"], lock["series"]):
        raise ValueError("The replay counts differ from its locked cohort.")
    prior_hash = digest(args.prior_events) if args.prior_events else None
    if prior_hash != lock.get("prior_sha256"):
        raise ValueError("Prior event evidence differs from the evaluated history.")
    prior = read_event_games(args.prior_events) if args.prior_events else []
    candidate = None
    if args.pair_model:
        parent = DraftChallengers.load(args.bundle, args.parent)
        model = AdaptivePairModel.load(args.pair_model, args.bundle)
        candidate = EventForecaster(parent, [*prior, *cohort], recipe="refresh", pair=model)
    frozen = candidate.frozen if candidate else DraftForecaster.load(args.bundle)
    scores, paired = replay(frozen, cohort, prior, json.loads(args.catalog.read_text()), args.kind, candidate=candidate)
    report = {"games": len(cohort), "series": lock["series"], "games_sha256": lock["sha256"],
              "catalog_sha256": lock["catalog_sha256"], "prior_sha256": prior_hash, "scores": scores}
    if paired:
        report.update(paired_exact_pairs=paired, parent_sha256=digest(args.parent), pair_model_sha256=digest(args.pair_model))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
