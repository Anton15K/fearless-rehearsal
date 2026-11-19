"""Fit and serve a chronological adjustment to frozen one-step pick probabilities."""
from argparse import ArgumentParser
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date as Date
from hashlib import sha256
from itertools import product
import json
from math import fsum, isclose, isfinite, log
from pathlib import Path
from tempfile import NamedTemporaryFile

from fearless.calibration import bundle_fingerprint
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model import serving_catalog
from fearless.model_features import ChampionCatalog, _patch_key
from fearless.rules import DRAFT_LENGTH, legal_champions, schedule


METHOD = "patch_pick_adaptation"
VERSION = 1


@dataclass(frozen=True)
class _Parameters:
    max_mix: float
    prior_strength: float
    other_phase_weight: float
    other_game_weight: float
    half_life_days: float

    def __post_init__(self):
        if not (
            0 <= self.max_mix < 1
            and 0 <= self.other_phase_weight <= 1
            and 0 <= self.other_game_weight <= 1
            and isfinite(self.prior_strength) and self.prior_strength > 0
            and isfinite(self.half_life_days) and self.half_life_days > 0
        ):
            raise ValueError("Invalid patch pick parameters.")

    def mix(self, mass):
        return self.max_mix * mass / (mass + self.prior_strength)


@dataclass(frozen=True)
class _Game:
    game_id: str
    patch: tuple[int, int] | None
    region: str
    number: int
    phases: tuple[tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True)
class _Series:
    series_id: str
    event: str | None
    day: Date
    games: tuple[_Game, ...]


def _phase(position):
    return int(position >= 16)


@dataclass(frozen=True)
class PatchPickModel:
    """An immutable snapshot of fitted settings and explicitly released draft evidence."""

    catalog: ChampionCatalog
    parameters: _Parameters
    _series: tuple[_Series, ...] = ()

    @classmethod
    def load(cls, path, bundle):
        artifact = json.loads(Path(path).read_text(encoding="utf8"))
        if (not isinstance(artifact, dict) or artifact.get("version") != VERSION
                or artifact.get("model_id") != bundle_fingerprint(bundle)):
            raise ValueError("Patch pick artifact is incompatible with the frozen bundle.")
        manifest = json.loads((Path(bundle) / "manifest.json").read_text(encoding="utf8"))
        return cls(serving_catalog(manifest["catalog"]), _Parameters(**artifact["parameters"]))

    def with_completed_series(self, rows):
        """Release additional whole series without mutating this model or retaining its input rows."""
        def normalize(game):
            return {
                **self.catalog.normalize_game(game),
                "disabled": tuple(self.catalog.canonical(name) for name in game.get("disabled", ())),
            }

        combined = {series.series_id: series for series in self._series}
        game_series = {game.game_id: series.series_id for series in self._series for game in series.games}
        for games in completed_series(normalize(row) for row in rows):
            series_id = str(games[0]["series_id"])
            members = []
            for game in games:
                game_id = str(game["game_id"])
                if game_series.setdefault(game_id, series_id) != series_id:
                    raise ValueError("A game cannot appear in multiple completed series.")
                picks = [[], []]
                for position, action in enumerate(game["actions"]):
                    if action["kind"] == "pick":
                        picks[_phase(position)].append(action["champion"])
                members.append(_Game(game_id, _patch_key(game.get("patch") or ""),
                                     game.get("region"), game["game_number"], tuple(map(tuple, picks))))
            evidence = _Series(series_id, games[0].get("event"),
                               Date.fromisoformat(completion_day(games)), tuple(members))
            if combined.setdefault(series_id, evidence) != evidence:
                raise ValueError("A completed series has conflicting pick evidence.")
        return replace(self, _series=tuple(sorted(combined.values(), key=lambda row: (row.day, row.series_id))))

    def _counts(self, *, date, patch, region, game_number, current, event, series_id, **_):
        day = Date.fromisoformat(date)
        patch_key = _patch_key(patch or "")
        phase = _phase(len(current))
        p = self.parameters
        counts, matched = Counter(), []
        if patch_key is None:
            return counts, matched
        for series in self._series:
            if series.day >= day:
                break
            if series.series_id == str(series_id) or (event is not None and event != series.event):
                continue
            decay = 2 ** (-(day - series.day).days / p.half_life_days)
            local = Counter()
            for game in series.games:
                if game.patch != patch_key or (event is None and game.region != region):
                    continue
                scale = decay * (1 if game.number == game_number else p.other_game_weight)
                for index, picks in enumerate(game.phases):
                    weight = scale * (1 if index == phase else p.other_phase_weight)
                    if weight > 0:
                        for champion in picks:
                            local[champion] += weight
            if local:
                counts.update(local)
                matched.append((series.day, local))
        return counts, matched

    def adjust(self, *, choices, date, patch, region, first, second, game_number, current,
               locked=(), pool=None, rosters=None, event=None, series_id=None, fearless=True, **_):
        """Blend matching completed picks into a full dated legal distribution."""
        if (not self._series or not self.parameters.max_mix or not fearless or not choices
                or len(current) >= DRAFT_LENGTH or schedule(first, second)[len(current)][1] != "pick"):
            return None
        counts, matched = self._counts(date=date, patch=patch, region=region, game_number=game_number,
                                       current=current, event=event, series_id=series_id)
        if not counts:
            return None

        canonical = self.catalog.canonical
        enabled = set(self.catalog.available(date, patch))
        if pool is not None:
            enabled.intersection_update(canonical(name) for name in pool)
        legal = set(legal_champions(enabled, (canonical(name) for name in locked),
                                   [self.catalog.normalize_action(action) for action in current]))
        names = [canonical(row["champion"]) for row in choices]
        probabilities = [row["probability"] for row in choices]
        if (len(legal) != len(names) or set(names) != legal
                or any(not isfinite(value) or value <= 0 for value in probabilities)):
            return None
        total = fsum(probabilities)
        if not isclose(total, 1.0, rel_tol=1e-7):
            return None

        mass = fsum(counts[name] for name in names)
        if not mass:
            return None
        weight = self.parameters.mix(mass)
        adjusted = [
            {**row, "probability": (1 - weight) * (probability / total) + weight * counts[name] / mass}
            for row, name, probability in zip(choices, names, probabilities, strict=True)
        ]
        eligible = [day for day, picks in matched if legal.intersection(picks)]
        return {
            "method": METHOD,
            "choices": sorted(adjusted, key=lambda row: -row["probability"]),
            "patch_adaptation": {
                "event": event, "patch": patch, "series": len(eligible),
                "evidence_through": eligible[-1].isoformat(), "weighted_legal_picks": mass,
                "mix_weight": weight,
            },
        }


def _replay(games, frozen):
    """Capture frozen production forecasts once so every fit candidate sees the same turns."""
    for game in sorted(games, key=lambda row: row["drafted_at"]):
        game = frozen.catalog.normalize_game(game)
        context = {key: game[key] for key in ("date", "patch", "region", "first", "second", "game_number", "locked")}
        disabled = {frozen.catalog.canonical(name) for name in game.get("disabled", ())}
        context["pool"] = [name for name in frozen.catalog.available(game["date"], game["patch"])
                           if name not in disabled]
        for position, action in enumerate(game["actions"]):
            if action["kind"] != "pick":
                continue
            context["current"] = game["actions"][:position]
            result = frozen.forecast(**context, limit=len(context["pool"]))
            target = action["champion"]
            if not any(row["champion"] == target for row in result["choices"]):
                raise ValueError(f"Frozen legal distribution excludes the observed pick in {game['game_id']}.")
            yield {**context, "event": game.get("event"), "series_id": game["series_id"]}, result["choices"], target


def _parameters(model, replay):
    """Choose weights by chronological pick log loss on development fit turns."""
    records = [
        (context, tuple(row["champion"] for row in choices), target,
         next(row["probability"] for row in choices if row["champion"] == target))
        for context, choices, target in replay
    ]
    best = (fsum(-log(base) for _, _, _, base in records), model.parameters)
    for phase, game, half_life in product((0, 0.5, 1), (0.35, 1), (7, 21)):
        pooled = replace(model, parameters=_Parameters(0, 80, phase, game, half_life))
        cache = {}
        evidence = []
        for context, names, target, base in records:
            key = (context["series_id"], context["game_number"], _phase(len(context["current"])))
            if key not in cache:
                cache[key], _ = pooled._counts(**context)
            counts = cache[key]
            mass = fsum(counts[name] for name in names)
            evidence.append((base, mass, counts[target] / mass if mass else base))
        for maximum, strength in product((0.10, 0.25, 0.50, 0.80), (200, 80, 20)):
            candidate = _Parameters(maximum, strength, phase, game, half_life)
            loss = fsum(-log(base + candidate.mix(mass) * (frequency - base))
                        for base, mass, frequency in evidence)
            if loss < best[0] - 1e-10:
                best = (loss, candidate)
    return best[1]


def _metrics(replay, model):
    losses = {"frozen": [], "adapted": []}
    ranks = {"frozen": [], "adapted": []}
    adjusted = 0
    for context, choices, target in replay:
        result = model.adjust(**context, choices=choices)
        adjusted += result is not None
        for name, rows in (("frozen", choices), ("adapted", result["choices"] if result else choices)):
            rank, probability = next((rank, row["probability"]) for rank, row in enumerate(rows, 1)
                                     if row["champion"] == target)
            losses[name].append(-log(probability))
            ranks[name].append(rank)
    turns = len(replay)
    return {
        "games": turns // 10, "series": len({context["series_id"] for context, _, _ in replay}),
        "turns": turns, "adjusted_turns": adjusted,
        **{
            name: {
                "top1": sum(rank == 1 for rank in values) / turns,
                "top3": sum(rank <= 3 for rank in values) / turns,
                "mean_reciprocal_rank": fsum(1 / rank for rank in values) / turns,
                "log_loss": fsum(losses[name]) / turns,
            }
            for name, values in ranks.items()
        },
    }


def fit(bundle, games, *, fit_through):
    """Tune on one chronological segment and diagnose on a later, disjoint segment."""
    from fearless.model import DraftForecaster

    fit_through = Date.fromisoformat(fit_through).isoformat()
    earlier, later = [], []
    for series in completed_series(games):
        if completion_day(series) <= fit_through:
            earlier.extend(series)
        elif min(game["date"] for game in series) > fit_through:
            later.extend(series)
        else:
            raise ValueError("A series spans the fit cutoff.")
    if not earlier or not later:
        raise ValueError("The fit cutoff requires both earlier fit and later diagnostic series.")

    frozen = DraftForecaster.load(bundle)
    cold = PatchPickModel(frozen.catalog, _Parameters(0, 80, 0.5, 1, 21))
    fit_history = cold.with_completed_series(earlier)
    train = list(_replay(earlier, frozen))
    selected = _parameters(fit_history, train)
    fitted = replace(fit_history, parameters=selected)
    artifact = {
        "version": VERSION, "model_id": bundle_fingerprint(bundle), "fit_through": fit_through,
        "parameters": asdict(selected),
    }
    diagnostic = list(_replay(later, frozen))
    report = {
        "fit": _metrics(train, fitted),
        "later_diagnostic": _metrics(diagnostic, fitted.with_completed_series(later)),
    }
    return artifact, report


def _write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, sort_keys=True, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True).add_parser(
        "fit", help="Select adaptation parameters and write a cold artifact.",
    )
    command.add_argument("--bundle", required=True, type=Path, help="Frozen model directory.")
    command.add_argument("--games", required=True, type=Path, help="Development-only complete series JSONL.")
    command.add_argument("--fit-through", required=True, help="Last UTC day for parameter selection.")
    command.add_argument("--artifact", required=True, type=Path, help="Output JSON file for the cold model.")
    command.add_argument("--report", required=True, type=Path, help="Output JSON file for development metrics.")
    args = parser.parse_args()
    artifact, metrics = fit(args.bundle, read_event_games(args.games), fit_through=args.fit_through)
    _write_json(args.artifact, artifact)
    _write_json(args.report, {**artifact, **metrics, "development_sha256": sha256(args.games.read_bytes()).hexdigest()})


if __name__ == "__main__":
    main()
