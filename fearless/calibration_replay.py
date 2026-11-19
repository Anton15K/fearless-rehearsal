"""Offline replay and diagnostics for the frozen private pick calibration sidecar.

A fit requires an explicitly supplied complete-series archive and final evidence
date. With no fit date, the CLI loads the existing artifact and writes only an
evaluation report. To refresh a deployed artifact, pass scored picks from newly
approved completed series to CalibrationSidecar.refreshed, evaluate the new copy
on disjoint later series, and deploy it explicitly against the same frozen model.
Neither serving nor evaluation refreshes the sidecar.
"""

import argparse
from collections import defaultdict
from datetime import date
import gzip
from itertools import groupby
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from fearless.calibration import CalibrationSidecar, ScoredPick, bundle_fingerprint, evaluate
from fearless.model import DraftForecaster, _method_scores, _softmax
from fearless.model_features import DraftContext, History, TEST_START, evidence_date, prepare_games, series_splits
from fearless.rules import DRAFT_LENGTH


def closed_series_days(games: list[dict]) -> dict[str, str]:
    """Read closure from a complete-series export, rejecting partial, gapped, or unfinished groups."""
    groups = defaultdict(list)
    for game in games:
        groups[str(game["series_id"])].append(game)
    completed = {}
    for series, rows in groups.items():
        numbers = sorted(row["game_number"] for row in rows)
        if numbers != list(range(1, len(rows) + 1)):
            continue
        if any(
            not row.get("eligible") or row.get("finished") is not True
            or row.get("series_complete") is False or len(row["actions"]) != DRAFT_LENGTH
            for row in rows
        ):
            continue
        completed[series] = max(evidence_date(row) for row in rows)
    return completed


def score_frozen_picks(games, forecaster: DraftForecaster, *, label: str | None = None, threads: int = 1):
    """Score legal pick turns with the actual frozen configuration and only its frozen history."""
    rows = [forecaster.catalog.normalize_game(game) for game in prepare_games(games)]
    completion = closed_series_days(rows)
    splits = series_splits(rows)
    config = forecaster.serving["pick"]
    if not config.get("calibrated"):
        raise ValueError("Fit the sidecar only over a frozen, validation-calibrated production pick model.")
    past = iter(forecaster.games)
    pending = next(past, None)
    history = History(forecaster.catalog)
    groups = (row for row in rows if row["date"] >= TEST_START and row.get("eligible"))
    for day, drafts in groupby(groups, key=lambda row: row["date"]):
        available = []
        while pending is not None and evidence_date(pending) < day:
            available.append(pending)
            pending = next(past, None)
        history.before(day, available)

        queries = []
        for game in drafts:
            split = splits[game["series_id"]]
            if split == "cross_boundary" and label is None:
                continue
            context = DraftContext.from_game(game)
            for position, action in enumerate(game["actions"]):
                if action["kind"] != "pick" or action["champion"] is None:
                    continue
                current = game["actions"][:position]
                turn = history.features(context, current, game["locked"])
                if (turn.kind, turn.team) != ("pick", action["team"]):
                    raise ValueError("A supplied pick differs from the draft schedule.")
                if not turn.champions:
                    continue
                target = turn.champions.index(action["champion"]) if action["champion"] in turn.champions else None
                queries.append((game, label or split, turn, target, position))

        if not queries:
            continue
        predictions = None
        if config["method"] != "baseline":
            features = np.concatenate([turn.values for _, _, turn, _, _ in queries])
            predictions = forecaster.models["pick"].predict(features, thread_count=threads)
        offset = 0
        for game, split, turn, target, position in queries:
            end = offset + len(turn.champions)
            learned = predictions[offset:end] if predictions is not None else None
            scores = _method_scores(config, turn.column("baseline"), learned)
            offset = end
            series = str(game["series_id"])
            yield ScoredPick(
                day, completion.get(series), game["patch"], series,
                str(game.get("game_id") or game["game_number"]), split,
                tuple(_softmax(scores).tolist()), target, position,
            )


def _read_games(path: Path):
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _private_report(path: Path, content: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(content, file, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, required=True, help="Private complete-series normalized JSONL archive.")
    parser.add_argument("--bundle", type=Path, required=True, help="Frozen evaluated private bundle.")
    parser.add_argument("--artifact", type=Path, required=True, help="Private path for the calibration sidecar.")
    parser.add_argument("--report", type=Path, required=True, help="Private path for the paired metrics.")
    parser.add_argument("--fit-through", type=date.fromisoformat, help="Explicit final evidence date for a new fit.")
    parser.add_argument("--label", help="Evaluation-only split label, for example unseen_gate.")
    args = parser.parse_args()
    if args.fit_through is not None and args.label:
        parser.error("Provide a fit date for development data or evaluate with a label, not both.")
    model_id = bundle_fingerprint(args.bundle)
    forecaster = DraftForecaster.load(args.bundle)
    records = list(score_frozen_picks(_read_games(args.games), forecaster, label=args.label))
    if not records:
        parser.error("The supplied data include no scored production pick turns.")
    if args.fit_through is None:
        sidecar = CalibrationSidecar.load(args.artifact, model_id=model_id)
    else:
        sidecar = CalibrationSidecar.fit(records, through=args.fit_through.isoformat(), model_id=model_id)
        sidecar.save(args.artifact)
    _private_report(args.report, {
        "scope": "old_splits_development_only" if args.fit_through else "evaluation_only",
        "frozen_model_id": model_id, "fit_through": sidecar.fit_through,
        "splits": evaluate(records, sidecar),
    })


if __name__ == "__main__":
    main()
