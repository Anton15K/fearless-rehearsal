"""Fit or serve an independent, open-data draft frequency model."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import json
from math import exp, fsum, log
from pathlib import Path

from fearless.rules import DRAFT_LENGTH, legal_champions, schedule, series_locks, validate_action


SOURCE = "Leaguepedia / LoL-Draft (CC BY-SA 4.0)"
SHOWCASE_SERIES = "994442840205359781"
SHOWCASE_DATE = "2026-08-01"
ARTIFACT_NAME = "public_forecast.json"
METHOD = "public_frequencies"
VALIDATION_START = "2026-04-25"
HOLDOUT_START = "2026-05-23"
KINDS = ("pick", "ban")
PARAMETERS = {
    "recent_days": 35,
    "smoothing": {"global": 110, "region": 100, "recent": 90, "slot": 65},
    "weights": {"region": 0.45, "recent": 0.35, "slot": 0.20},
}


@dataclass(frozen=True)
class _Turn:
    day: str
    region: str
    index: int
    kind: str
    legal: list[str]
    champion: str


def _partition(day):
    if day < VALIDATION_START:
        return "train"
    if day < HOLDOUT_START:
        return "validation"
    return "holdout"


def _split_series(games):
    grouped = defaultdict(list)
    for game in games:
        if game.get("source") != SOURCE:
            raise ValueError("Fit the public model only on independently sourced public replay rows.")
        grouped[str(game["series_id"])].append(game)

    groups = {name: [] for name in ("train", "validation", "holdout")}
    exclusions = dict.fromkeys(("showcase", "on_or_after_showcase", "cross_boundary", "ineligible"), 0)
    for series_id, series in grouped.items():
        days = {date.fromisoformat(game["date"]).isoformat() for game in series}
        if series_id == SHOWCASE_SERIES:
            exclusions["showcase"] += 1
        elif max(days) >= SHOWCASE_DATE:
            exclusions["on_or_after_showcase"] += 1
        elif len({_partition(day) for day in days}) != 1:
            exclusions["cross_boundary"] += 1
        elif any(not game.get("eligible", True) or not game.get("finished", True) for game in series):
            exclusions["ineligible"] += 1
        else:
            groups[_partition(min(days))].append(sorted(series, key=lambda game: game["game_number"]))

    if any(not series for series in groups.values()):
        raise ValueError("Public model fitting needs whole training, validation and holdout series.")
    return groups, exclusions


def _turns(series_groups, catalog):
    for series in series_groups:
        completed = []
        for number, game in enumerate(series, start=1):
            if game["game_number"] != number or len(game["actions"]) != DRAFT_LENGTH:
                raise ValueError("Public fitting requires complete drafts and contiguous series game numbers.")
            enabled = catalog[game["patch"]]
            locked = series_locks(completed, fearless=game.get("fearless", True))
            current = []
            for index, action in enumerate(game["actions"]):
                validate_action(enabled, locked, current, game["first"], game["second"], **action)
                if action["champion"] is not None:
                    yield _Turn(game["date"], game["region"], index, action["kind"],
                                legal_champions(enabled, locked, current), action["champion"])
                current.append(action)
            completed.append(current)


def _statistics(turns, cutoff):
    opportunities = defaultdict(Counter)
    choices = defaultdict(Counter)
    for turn in turns:
        keys = [f"global:{turn.kind}", f"region:{turn.region}:{turn.kind}", f"slot:{turn.index}"]
        if turn.day >= cutoff:
            keys.append(f"recent:{turn.kind}")
        for key in keys:
            opportunities[key].update(turn.legal)
            choices[key][turn.champion] += 1
    return {
        key: {champion: [choices[key][champion], count] for champion, count in counter.items()}
        for key, counter in opportunities.items()
    }


def _scores(statistics, parameters, kind, region, index, legal):
    global_counts = statistics.get(f"global:{kind}", {})
    region_counts = statistics.get(f"region:{region}:{kind}", {})
    recent_counts = statistics.get(f"recent:{kind}", {})
    slot_counts = statistics.get(f"slot:{index}", {})
    smoothing, weights = parameters["smoothing"], parameters["weights"]
    uniform = 1 / max(1, len(legal))

    def rate(counts, champion, fallback, scope):
        chosen, exposed = counts.get(champion, (0, 0))
        strength = smoothing[scope]
        return (chosen + strength * fallback) / (exposed + strength)

    context, baseline = [], []
    for champion in legal:
        global_rate = rate(global_counts, champion, uniform, "global")
        regional = rate(region_counts, champion, global_rate, "region")
        recent = rate(recent_counts, champion, regional, "recent")
        slot = rate(slot_counts, champion, regional, "slot")
        context.append(log(weights["region"] * regional + weights["recent"] * recent + weights["slot"] * slot))
        baseline.append(log(global_counts.get(champion, (0, 0))[0] + 1))
    return {METHOD: context, "baseline": baseline}


def _distribution(scores, temperature):
    highest = max(scores)
    unnormalized = [exp((score - highest) / temperature) for score in scores]
    total = fsum(unnormalized)
    return [value / total for value in unnormalized]


def _examples(turns, statistics):
    rows = defaultdict(list)
    for turn in turns:
        scores = _scores(statistics, PARAMETERS, turn.kind, turn.region, turn.index, turn.legal)
        target = turn.legal.index(turn.champion)
        for method, vector in scores.items():
            rows[turn.kind, method].append((vector, target))
    return rows


def _temperature(examples):
    if not examples:
        raise ValueError("Validation must include both named actions.")

    def loss(log_temperature):
        temperature = exp(log_temperature)
        return -fsum(log(_distribution(scores, temperature)[target]) for scores, target in examples)

    # Optimize a single parameter on validation only; bound extreme concentration on sparse slices.
    low, high = log(0.25), log(4.0)
    for _ in range(18):
        left = (2 * low + high) / 3
        right = (low + 2 * high) / 3
        if loss(left) < loss(right):
            high = right
        else:
            low = left
    return exp((low + high) / 2)


def _metrics(examples, temperature):
    if not examples:
        raise ValueError("Evaluation must include both named actions.")
    hits, hits3, log_loss, brier = 0, 0, 0.0, 0.0
    bins = defaultdict(list)
    for scores, target in examples:
        rank = sum(score > scores[target] or (score == scores[target] and index < target)
                   for index, score in enumerate(scores))
        probabilities = _distribution(scores, temperature)
        confidence = max(probabilities)
        hit = rank == 0
        hits += hit
        hits3 += rank < 3
        log_loss -= log(probabilities[target])
        brier += (confidence - hit) ** 2
        bins[min(9, int(confidence * 10))].append((confidence, hit))
    turns = len(examples)
    calibration = fsum(abs(fsum(confidence - hit for confidence, hit in points)) for points in bins.values())
    return {
        "turns": turns, "hit_at_1": hits / turns, "hit_at_3": hits3 / turns,
        "log_loss": log_loss / turns, "top1_ece": calibration / turns, "top1_brier": brier / turns,
    }


def fit_public_model(games, patch_catalog):
    """Fit aggregate counts from earlier public series and independently assess held-out series."""
    catalog = {patch: sorted(set(names), key=lambda name: (name.casefold(), name))
               for patch, names in patch_catalog.items()}
    splits, exclusions = _split_series(games)
    partitions = {}
    for split, series in splits.items():
        days = [game["date"] for games in series for game in games]
        partitions[split] = {"series": len(series), "games": len(days), "from": min(days), "through": max(days)}

    train_through = date.fromisoformat(partitions["train"]["through"])
    recent_from = (train_through - timedelta(days=PARAMETERS["recent_days"] - 1)).isoformat()
    statistics = _statistics(_turns(splits["train"], catalog), recent_from)
    for kind in KINDS:
        partitions["train"][f"{kind}_turns"] = sum(chosen for chosen, _ in statistics[f"global:{kind}"].values())

    validation = _examples(_turns(splits["validation"], catalog), statistics)
    temperature = {kind: {method: _temperature(validation[kind, method]) for method in (METHOD, "baseline")}
                   for kind in KINDS}
    holdout = _examples(_turns(splits["holdout"], catalog), statistics)
    metrics = {
        split: {kind: {method: _metrics(examples[kind, method], temperature[kind][method])
                       for method in (METHOD, "baseline")} for kind in KINDS}
        for split, examples in (("validation", validation), ("holdout", holdout))
    }

    unique_pools = sorted(set(tuple(names) for names in catalog.values()))
    return {
        "schema": 1, "source": SOURCE, "license": "CC BY-SA 4.0",
        "showcase": {"series_id": SHOWCASE_SERIES, "date": SHOWCASE_DATE},
        "split_boundaries": {"validation_start": VALIDATION_START, "holdout_start": HOLDOUT_START},
        "partitions": partitions, "exclusions": exclusions, "recent_from": recent_from, "parameters": PARAMETERS,
        "statistics": statistics, "temperature": temperature, "metrics": metrics,
        "patches": {patch: unique_pools.index(tuple(names)) for patch, names in catalog.items()},
        "pools": unique_pools,
    }


class PublicForecaster:
    """Serve the open model without external packages, history records or a network connection."""

    def __init__(self, artifact):
        if artifact.get("schema") != 1 or artifact.get("source") != SOURCE:
            raise ValueError("Unrecognized public forecast artifact.")
        self.artifact = artifact

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf8")))

    def forecast(self, *, date, patch, region, first, second, game_number, current, locked=(),
                 pool=None, rosters=None, limit=3, fearless=True):
        if len(current) >= DRAFT_LENGTH:
            return {"status": "complete", "choices": [], "legal_candidates": 0}
        artifact = self.artifact
        validation_through = artifact["partitions"]["validation"]["through"]
        if date <= validation_through:
            return {"status": "unavailable", "choices": [], "legal_candidates": 0}

        index = len(current)
        team, kind = schedule(first, second)[index]
        pool_number = artifact["patches"].get(patch)
        enabled = artifact["pools"][pool_number] if pool_number is not None else ()
        if pool is not None:
            requested = set(pool)
            enabled = [champion for champion in enabled if champion in requested]
        legal = legal_champions(enabled, locked if fearless else (), current)
        model_stats = artifact["statistics"]
        result = {
            "status": "scored" if legal else "no_candidates", "kind": kind, "team": team,
            "model_source": "public", "method": METHOD, "as_of": artifact["partitions"]["train"]["through"],
            "calibrated": False, "probability_quality": "estimated", "calibration_through": validation_through,
            "probability_condition": "named_champion_action", "legal_candidates": len(legal), "choices": [],
        }
        if not legal:
            return result

        scores = _scores(model_stats, artifact["parameters"], kind, region, index, legal)[METHOD]
        probabilities = _distribution(scores, artifact["temperature"][kind][METHOD])
        selected = sorted(range(len(legal)), key=lambda position: (-scores[position], position))[:max(0, limit)]
        counts = model_stats.get(f"global:{kind}", {})
        for position in selected:
            champion = legal[position]
            chosen, exposed = counts.get(champion, (0, 0))
            evidence = (f"Open LCS training drafts: {chosen} {kind} selections in {exposed} legal opportunities."
                        if exposed else "Smoothed estimate with no prior legal opportunities in the open training sample.")
            result["choices"].append({
                "champion": champion, "probability": probabilities[position], "evidence": [evidence],
                "games": chosen, "roles": [],
            })
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, required=True, help="independently sourced public_games.jsonl")
    parser.add_argument("--catalog", type=Path, required=True, help="dated patch-to-champions JSON")
    parser.add_argument("--output", type=Path, required=True, help="public aggregate model JSON")
    args = parser.parse_args()
    with args.games.open(encoding="utf8") as source:
        artifact = fit_public_model((json.loads(line) for line in source if line.strip()),
                                    json.loads(args.catalog.read_text(encoding="utf8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"partitions": artifact["partitions"], "holdout": artifact["metrics"]["holdout"]}, indent=2))


if __name__ == "__main__":
    main()
