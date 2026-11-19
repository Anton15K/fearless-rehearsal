"""Evaluate ban refresh and fixed pick temperature on a separately locked transfer cohort."""

import argparse
from collections import Counter
from itertools import groupby, pairwise
import json
from math import fsum, isclose, isfinite
from pathlib import Path

from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_bans import EventBanRefresh
from fearless.event_confidence import EventConfidence
from fearless.event_forecaster import EventForecaster, digest
from fearless.event_series import completed_series, read_event_games
from fearless.rules import legal_champions
from scripts.compare_challengers import context, write_json
from scripts.evaluate_adaptation import cache_history, intervals, metrics, quality, windows


BAN_GATE = "Positive lower 95% Hit@3 bound, non-increasing overall NLL, and non-increasing NLL in either phase."
CONFIDENCE_GATE = "Negative upper 95% NLL bound, lower top-1 ECE, and identical full ranking."


def verify_cohort(path, lock, *, strata=3):
    if digest(path) != lock.get("sha256"):
        raise ValueError("Games digest differs from the locked eligible cohort.")
    rows = read_event_games(path)
    series = completed_series(rows)
    if (len(rows), len(series)) != (lock.get("games"), lock.get("series")) or not rows:
        raise ValueError("Eligible game or series counts differ from the locked cohort.")
    events = {game.get("event") for game in rows}
    if None in events or "" in events or len(events) != strata:
        raise ValueError(f"Expected exactly {strata} nonempty event strata.")
    return series


def probabilities(choices, legal):
    result = {row["champion"]: row["probability"] for row in choices}
    values = list(result.values())
    if (len(choices) != len(legal) or set(result) != legal or not values
            or any(not isfinite(p) or p <= 0 for p in values)
            or not isclose(fsum(values), 1.0, abs_tol=1e-7) or values != sorted(values, reverse=True)):
        raise ValueError("Expected a normalized, stably ordered full legal distribution.")
    return result


def score(baseline, candidate, target):
    return {"incumbent": quality(baseline, target), "candidate": quality(candidate, target)}


def replay(parent, series, catalog, confidence):
    source = EventForecaster(parent, (game for group in series for game in group), recipe="refresh")
    bans = EventBanRefresh(source)
    advance = [cache_history(model) for model in (parent.frozen, source.updated)]
    ban_rows = {"opening": [], "late": []}
    pick_rows, coverage = [], Counter()
    for day, games in groupby(windows(series), key=lambda game: game["date"]):
        for cache in advance:
            cache(day)
        for game in games:
            event, sid = game["event"], game["series_id"]
            ctx = context(game, catalog)
            for index, action in enumerate(game["actions"]):
                prefix = game["actions"][:index]
                legal = set(legal_champions(ctx["pool"], ctx["locked"], prefix))
                target = action["champion"]
                if target not in legal:
                    raise ValueError("A recorded ban or pick is outside the locked legal choice set.")
                base_context = dict(**ctx, current=prefix, limit=len(legal))
                extended = dict(**base_context, event=event, series_id=sid)
                if action["kind"] == "ban":
                    before = parent.forecast(**base_context)
                    after = bans.forecast(**extended)
                    original = probabilities(before["choices"], legal)
                    refreshed = probabilities(after["choices"], legal)
                    if evidence := after.get("event_evidence"):
                        if evidence["series_through"] >= day:
                            raise ValueError("Ban evidence does not strictly predate the forecast.")
                        coverage["refreshed_bans"] += 1
                    elif before != after:
                        raise ValueError("A cold ban differed from the promoted policy.")
                    phase = "opening" if index < 6 else "late"
                    ban_rows[phase].append((event, sid, score(original, refreshed, target)))
                    continue

                before = source.forecast(**extended)
                if bans.forecast(**extended) != before:
                    raise ValueError("Ban refresh modified an existing pick forecast.")
                original = probabilities(before["choices"], legal)
                if before["method"] == "refreshed_pick":
                    if before["event_evidence"]["series_through"] >= day:
                        raise ValueError("Pick evidence does not strictly predate the forecast.")
                    calibrated_choices = confidence.apply(before["choices"])
                    calibrated = probabilities(calibrated_choices, legal)
                    pairs = pairwise(zip(original.values(), calibrated.values(), strict=True))
                    if list(original) != list(calibrated) or any(old_a == old_b and new_a != new_b
                                                                  for (old_a, new_a), (old_b, new_b) in pairs):
                        raise ValueError("Pick temperature changed ranking or broke a probability tie.")
                    coverage["calibrated_picks"] += 1
                else:
                    calibrated = original
                pick_rows.append((event, sid, score(original, calibrated, target)))
                coverage["unchanged_pick_rankings"] += 1
    games = sum(map(len, series))
    if (len(ban_rows["opening"]), len(ban_rows["late"]), len(pick_rows)) != (6 * games, 4 * games, 10 * games):
        raise ValueError("Forecast turn counts differ from the full locked cohort.")
    if any(row[2]["incumbent"][0] != row[2]["candidate"][0] for row in pick_rows):
        raise ValueError("Pick temperature changed top-three hits.")
    return ban_rows, pick_rows, dict(coverage)


def summary(rows):
    return {"incumbent": metrics([row[2]["incumbent"] for row in rows]),
            "candidate": metrics([row[2]["candidate"] for row in rows]),
            "hit3_delta_ci95": intervals(rows, 0), "nll_delta_ci95": intervals(rows, 1),
            "series": len({row[1] for row in rows})}


def reports(bans, picks):
    by_phase = {phase: summary(rows) for phase, rows in bans.items()}
    return {"ban": {"all": summary([*bans["opening"], *bans["late"]]), **by_phase}, "confidence": summary(picks)}


def passed(report):
    all_bans, confidence = report["ban"]["all"], report["confidence"]
    phases_safe = all(row["candidate"]["nll"] <= row["incumbent"]["nll"] for row in report["ban"].values())
    return {"ban": all_bans["hit3_delta_ci95"][0] > 0 and phases_safe,
            "confidence": (confidence["nll_delta_ci95"][1] < 0
                           and confidence["candidate"]["ece"] < confidence["incumbent"]["ece"]
                           and confidence["candidate"]["hits"] == confidence["incumbent"]["hits"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("bundle", "parent", "games", "catalog", "lock", "confidence", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    parser.add_argument("--confidence-sha256", required=True)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text())
    confidence_hash = digest(args.confidence)
    if confidence_hash != args.confidence_sha256:
        raise ValueError("Confidence artifact differs from the frozen candidate digest.")
    catalog_hash = digest(args.catalog)
    if catalog_hash != lock.get("catalog_sha256"):
        raise ValueError("Legal champion catalog differs from the locked version.")
    parent = DraftChallengers.load(args.bundle, args.parent)
    confidence = EventConfidence.load(args.confidence, args.bundle)
    series = verify_cohort(args.games, lock)
    ban_rows, picks, coverage = replay(parent, series, json.loads(args.catalog.read_text()), confidence)
    events = sorted({row[0] for row in picks})
    overall = reports(ban_rows, picks)
    by_event = {event: reports({phase: [row for row in rows if row[0] == event]
                                for phase, rows in ban_rows.items()}, [row for row in picks if row[0] == event])
                for event in events}
    report = {"evaluation": "retrospective event transfer", "manifest_sha256": digest(args.lock),
              "games_sha256": lock["sha256"], "catalog_sha256": catalog_hash, "parent_sha256": digest(args.parent),
              "incumbent": incumbent_signature(args.bundle), "confidence_sha256": confidence_hash,
              "temperature": confidence.temperature, "games": lock["games"], "series": len(series), "strata": events,
              "games_by_event": dict(Counter(group[0]["event"] for group in series for _ in group)),
              "gates": {"ban": BAN_GATE, "confidence": CONFIDENCE_GATE}, "promoted": passed(overall),
              "overall": overall, "by_event": by_event, "coverage": coverage, "illegal_predictions": 0}
    write_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
