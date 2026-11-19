"""Fit rank-preserving confidence for the refreshed event pick recipe."""
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass
from itertools import groupby
import json
from math import fsum, log
from pathlib import Path

import numpy as np

from fearless.calibration import FROZEN, TEMPERATURES, _metrics, adjust_probabilities
from fearless.challengers import incumbent_signature


VERSION = 1
RECIPE = "refresh"
METHOD = "refreshed_pick"


@dataclass(frozen=True)
class EventConfidence:
    """Rescale a full refreshed pick distribution while retaining its rank and evidence."""

    temperature: float

    def __post_init__(self):
        if not TEMPERATURES[0] <= self.temperature <= TEMPERATURES[-1]:
            raise ValueError("Event confidence temperature is outside the fitted bounds.")

    @classmethod
    def load(cls, path, bundle):
        artifact = json.loads(Path(path).read_text(encoding="utf8"))
        if (not isinstance(artifact, dict) or artifact.get("version") != VERSION or artifact.get("recipe") != RECIPE
                or artifact.get("incumbent") != incumbent_signature(bundle)):
            raise ValueError("Event confidence requires the exact incumbent bundle and refreshed pick recipe.")
        temperature = artifact.get("temperature")
        if type(temperature) not in (int, float):
            raise ValueError("Event confidence requires a numeric temperature.")
        return cls(temperature)

    def apply(self, choices):
        """Adjust every legal choice. Callers preserve unrefreshed forecasts as served."""
        if not choices:
            return []
        probabilities = [row["probability"] for row in choices]
        if any(probability <= 0 for probability in probabilities):
            raise ValueError("Supply a positive full legal pick distribution.")
        adjusted = adjust_probabilities(probabilities, self.temperature)
        return [{**row, "probability": probability} for row, probability in zip(choices, adjusted, strict=True)]


@dataclass(frozen=True)
class PickConfidenceTurn:
    tournament: str
    probabilities: tuple[float, ...]
    target: int
    refreshed: bool


def score_refresh(parent, rows, catalog):
    """Replay the deployed refresh path and keep its cold fallback forecasts unchanged."""
    from fearless.event_forecaster import EventForecaster
    from scripts.compare_challengers import context, distribution
    from scripts.evaluate_adaptation import cache_history, windows

    model = EventForecaster(parent, rows, recipe=RECIPE)
    advance_parent, advance_refresh = cache_history(parent.frozen), cache_history(model.updated)
    for day, games in groupby(windows(model.series), key=lambda game: game["date"]):
        advance_parent(day)
        advance_refresh(day)
        for game in games:
            tournament = game.get("event")
            if not tournament:
                raise ValueError("Every pick needs a resolved tournament identity.")
            options = dict(**context(game, catalog), event=tournament, series_id=game["series_id"])
            for index, action in enumerate(game["actions"]):
                if action["kind"] != "pick":
                    continue
                probabilities, result = distribution(model, options, game["actions"][:index])
                yield PickConfidenceTurn(tournament, tuple(probabilities.values()), list(probabilities).index(
                    action["champion"]), result["method"] == METHOD)


def _grid_losses(turns):
    eligible = [row for row in turns if row.refreshed]
    if not eligible:
        return np.zeros(len(TEMPERATURES))
    width = max(len(row.probabilities) for row in eligible)
    shifted = np.full((len(eligible), width), -np.inf)
    for index, row in enumerate(eligible):
        values = np.log(row.probabilities)
        shifted[index, :len(values)] = values - values.max()
    targets = shifted[np.arange(len(eligible)), [row.target for row in eligible]]
    return np.array([
        (np.log(np.exp(shifted / temperature).sum(axis=1)) - targets / temperature).sum()
        for temperature in TEMPERATURES
    ])


def _temperature(losses):
    selected = int(losses.argmin())
    return TEMPERATURES[selected] if losses[selected] < losses[FROZEN] - 1e-10 else 1.0


def _outcomes(turns, temperature):
    raw, calibrated = [], []
    for row in turns:
        probabilities = row.probabilities
        adjusted = adjust_probabilities(probabilities, temperature) if row.refreshed else probabilities
        for collection, scores in ((raw, probabilities), (calibrated, adjusted)):
            collection.append((scores[0], row.target == 0, row.target < 3, -log(scores[row.target])))
    return raw, calibrated


def _report(raw, calibrated, eligible):
    return {
        "turns": len(raw),
        "eligible_turns": eligible,
        **{
            name: {**_metrics(outcomes), "hit_at_1": fsum(row[1] for row in outcomes) / len(outcomes)}
            for name, outcomes in (("raw", raw), ("calibrated", calibrated))
        },
    }


def fit_temperature(turns):
    """Choose one temperature by NLL and diagnose transfer between development tournaments."""
    tournaments = defaultdict(list)
    for turn in turns:
        tournaments[turn.tournament].append(turn)
    if len(tournaments) < 2 or not any(turn.refreshed for rows in tournaments.values() for turn in rows):
        raise ValueError("Fit needs refreshed picks and at least two development tournaments.")
    losses = {event: _grid_losses(rows) for event, rows in sorted(tournaments.items())}
    temperature = _temperature(sum(losses.values()))
    all_raw, all_calibrated, folds = [], [], {}
    eligible = 0
    for event in losses:
        fold_temperature = _temperature(sum(other for name, other in losses.items() if name != event))
        rows = tournaments[event]
        raw, calibrated = _outcomes(rows, fold_temperature)
        count = sum(row.refreshed for row in rows)
        folds[event] = {"temperature": fold_temperature, **_report(raw, calibrated, count)}
        all_raw.extend(raw)
        all_calibrated.extend(calibrated)
        eligible += count
    refit = _outcomes([row for rows in tournaments.values() for row in rows], temperature)
    return EventConfidence(temperature), {
        "by_tournament": folds,
        "leave_one_tournament_out": _report(all_raw, all_calibrated, eligible),
        "refit": _report(*refit, eligible),
    }


def main():
    from fearless.challengers import DraftChallengers
    from fearless.event_forecaster import digest, read_event_games
    from fearless.patch_picks import _write_json

    parser = ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True).add_parser("fit")
    for name in ("bundle", "parent", "catalog", "artifact", "report"):
        command.add_argument("--" + name, required=True, type=Path)
    command.add_argument("--games", required=True, type=Path, nargs="+",
                         help="Explicit approved development archives, replayed independently.")
    args = parser.parse_args()

    parent = DraftChallengers.load(args.bundle, args.parent)
    catalog = json.loads(args.catalog.read_text(encoding="utf8"))
    turns = (turn for path in args.games for turn in score_refresh(parent, read_event_games(path), catalog))
    fitted, diagnostics = fit_temperature(turns)
    artifact = {
        "version": VERSION, "recipe": RECIPE, "incumbent": incumbent_signature(args.bundle),
        "temperature": fitted.temperature,
    }
    report = {**artifact, **diagnostics, "development_sha256": [digest(path) for path in args.games]}
    _write_json(args.artifact, artifact)
    _write_json(args.report, report)


if __name__ == "__main__":
    main()
