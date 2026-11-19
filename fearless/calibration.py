"""Opt-in chronological temperature calibration for full private pick distributions."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from math import exp, fsum, isclose, isfinite, log
from pathlib import Path
from tempfile import NamedTemporaryFile
import re
from typing import Iterable, Sequence


TEMPERATURES = tuple(exp(index / 40) for index in range(-16, 17))
FROZEN = TEMPERATURES.index(1.0)
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScoredPick:
    day: str
    completed_day: str | None
    patch: str
    series_id: str
    game_id: str
    split: str
    probabilities: Sequence[float]
    target: int | None
    position: int | None = None


@dataclass(frozen=True)
class CalibrationPolicy:
    min_series: int = 8
    min_turns: int = 120
    max_age_days: int = 75
    patch_distance: int = 2
    half_life_days: int = 35
    nearby_weight: float = 0.6
    log_temperature_penalty: float = 0.35
    min_objective_gain: float = 0.002


@dataclass(frozen=True)
class CalibrationDecision:
    temperature: float
    source: str
    series: int
    turns: int
    evidence_start: str | None
    evidence_through: str | None
    patches: tuple[str, ...]
    temperature_p10: float | None = None
    temperature_p90: float | None = None
    fallback: str | None = None


@dataclass(frozen=True)
class _Series:
    series_id: str
    completed: str
    patch: str
    turns: int
    losses: tuple[float, ...]


def _patch(patch: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", patch)
    if not match:
        return None
    season, update = map(int, match.groups())
    return (season - 10 if 25 <= season <= 29 else season, update)


def _log_probabilities(probabilities: Sequence[float]) -> tuple[float, ...]:
    if len(probabilities) == 0 or any(not isfinite(p) or p < 0 for p in probabilities):
        raise ValueError("Supply a nonempty full legal probability distribution.")
    if not isclose(fsum(probabilities), 1.0, rel_tol=1e-6):
        raise ValueError("Full legal probabilities must sum to one, including choices outside the shortlist.")
    return tuple(log(p) if p > 0 else float("-inf") for p in probabilities)


def adjust_probabilities(probabilities: Sequence[float], temperature: float) -> tuple[float, ...]:
    """Rescale a complete legal pick distribution without changing its ranking."""
    if not isfinite(temperature) or temperature <= 0:
        raise ValueError("Temperature must be finite and positive.")
    logits = _log_probabilities(probabilities)
    if temperature == 1.0:
        return tuple(probabilities)
    maximum = max(logits)
    weights = tuple(exp((score - maximum) / temperature) for score in logits)
    total = fsum(weights)
    return tuple(weight / total for weight in weights)


def bundle_fingerprint(directory: str | Path) -> str:
    """Bind a fitted calibration artifact to the exact frozen model and history."""
    directory = Path(directory)
    manifest = directory / "manifest.json"
    metadata = json.loads(manifest.read_text(encoding="utf8"))
    names = ["manifest.json", "history.jsonl.gz"]
    pick = metadata["models"].get("pick")
    if pick:
        names.append(pick)
    digest = sha256()
    for name in names:
        digest.update(bytes.fromhex(sha256((directory / name).read_bytes()).hexdigest()))
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    index = (len(values) - 1) * fraction
    low = int(index)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low)


class CalibrationSidecar:
    """Immutable evidence summary queried using strictly earlier completed series."""

    def __init__(self, series, *, fit_through: str, policy: CalibrationPolicy, model_id: str | None = None):
        self._series = tuple(series)
        self.fit_through = date.fromisoformat(fit_through).isoformat()
        self.policy = policy
        self.model_id = model_id

    @classmethod
    def fit(cls, rows: Iterable[ScoredPick], *, through: str, policy=CalibrationPolicy(), model_id=None):
        through = date.fromisoformat(through).isoformat()
        grouped = defaultdict(list)
        for row in rows:
            grouped[row.series_id].append(row)
        series = []
        for series_id, records in grouped.items():
            completions = {row.completed_day for row in records}
            if len(completions) != 1:
                raise ValueError("Every record in a series must share its series completion date.")
            completed = completions.pop()
            patches = {_patch(row.patch) for row in records}
            if completed is None or completed > through or len(patches) != 1 or None in patches:
                continue
            if any(completed < row.day for row in records):
                raise ValueError("A series completion date cannot precede any of its drafts.")
            losses = []
            seen = {}
            for row in records:
                if row.position is not None:
                    choice = (row.game_id, row.position)
                    label = (row.target, tuple(row.probabilities))
                    if choice in seen:
                        if seen[choice] != label:
                            raise ValueError("A pick slot has conflicting calibration evidence.")
                        continue
                    seen[choice] = label
                if row.target is None:
                    continue
                probabilities = _log_probabilities(row.probabilities)
                if probabilities[row.target] == float("-inf"):
                    continue
                maximum = max(probabilities)
                shifted = tuple(value - maximum for value in probabilities)
                losses.append(tuple(
                    log(fsum(exp(value / temp) for value in shifted)) - shifted[row.target] / temp
                    for temp in TEMPERATURES
                ))
            if losses:
                key = next(iter(patches))
                series.append(_Series(
                    series_id, completed, f"{key[0]}.{key[1]}",
                    len(losses), tuple(fsum(values) for values in zip(*losses)),
                ))
        return cls(sorted(series, key=lambda group: (group.completed, group.series_id)),
                   fit_through=through, policy=policy, model_id=model_id)

    def refreshed(self, rows: Iterable[ScoredPick], *, through: str) -> "CalibrationSidecar":
        """Add explicitly released whole-series evidence while preserving this fitted snapshot."""
        through = date.fromisoformat(through).isoformat()
        if through < self.fit_through:
            raise ValueError("The refresh cutoff cannot precede the prior fit.")
        incoming = self.fit(rows, through=through, policy=self.policy, model_id=self.model_id)
        combined = {group.series_id: group for group in self._series}
        for group in incoming._series:
            previous = combined.get(group.series_id)
            if previous is None:
                combined[group.series_id] = group
                continue
            if (
                previous.completed != group.completed or previous.patch != group.patch
                or previous.turns != group.turns
                or not all(isclose(old, new, rel_tol=1e-9, abs_tol=1e-10)
                           for old, new in zip(previous.losses, group.losses, strict=True))
            ):
                raise ValueError("A previously fitted series has conflicting calibration evidence.")
        series = sorted(combined.values(), key=lambda group: (group.completed, group.series_id))
        return type(self)(series, fit_through=through, policy=self.policy, model_id=self.model_id)

    def save(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = {
            "version": SCHEMA_VERSION, "fit_through": self.fit_through, "model_id": self.model_id,
            "policy": asdict(self.policy), "series": [asdict(group) for group in self._series],
        }
        with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(content, file, allow_nan=False, separators=(",", ":"))
                file.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: str | Path, *, model_id: str | None = None):
        data = json.loads(Path(path).read_text(encoding="utf8"))
        if data["version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported pick calibration artifact.")
        if model_id is not None and data["model_id"] != model_id:
            raise ValueError("Pick calibration was fitted for a different frozen bundle.")
        series = [_Series(**{**group, "losses": tuple(group["losses"])}) for group in data["series"]]
        return cls(series, fit_through=data["fit_through"], policy=CalibrationPolicy(**data["policy"]),
                   model_id=data["model_id"])

    def temperature_for(self, day: str, patch: str, *, series_id: str | None = None) -> float:
        return self.decision_for(day, patch, series_id=series_id).temperature

    def decision_for(self, day: str, patch: str, *, series_id: str | None = None) -> CalibrationDecision:
        target_day = date.fromisoformat(day)
        target_patch = _patch(patch)
        policy = self.policy
        if target_patch is None:
            return CalibrationDecision(1.0, "frozen", 0, 0, None, None, (), fallback="unknown_patch")
        nearby = []
        for group in self._series:
            completed = date.fromisoformat(group.completed)
            age = (target_day - completed).days
            if group.series_id == series_id or not 0 < age <= policy.max_age_days:
                continue
            season, update = _patch(group.patch)
            distance = target_patch[1] - update
            if season != target_patch[0] or not 0 <= distance <= policy.patch_distance:
                continue
            weight = 2 ** (-age / policy.half_life_days)
            nearby.append((group, weight if distance == 0 else weight * policy.nearby_weight, distance))

        same = [entry for entry in nearby if entry[2] == 0]
        cohort, source = (same, "same_patch") if self._supported(same) else (nearby, "nearby_patches")
        first = min((group.completed for group, _, _ in cohort), default=None)
        last = max((group.completed for group, _, _ in cohort), default=None)
        patch_names = tuple(sorted({group.patch for group, _, _ in cohort}, key=_patch))
        info = (len(cohort), sum(group.turns for group, _, _ in cohort), first, last, patch_names)
        if not self._supported(cohort):
            return CalibrationDecision(1.0, "frozen", *info, fallback="insufficient_recent_evidence")

        masses = [group.turns * weight for group, weight, _ in cohort]
        weighted_losses = [[loss * weight for loss in group.losses] for group, weight, _ in cohort]
        totals = tuple(fsum(column) for column in zip(*weighted_losses))
        mass = fsum(masses)
        best = self._select(totals, mass)
        if best == FROZEN:
            return CalibrationDecision(1.0, "frozen", *info, fallback="insufficient_gain")
        jackknife = [
            TEMPERATURES[self._select(tuple(total - contribution for total, contribution in zip(totals, loss)),
                                      mass - omitted)]
            for loss, omitted in zip(weighted_losses, masses) if omitted < mass
        ]
        return CalibrationDecision(
            TEMPERATURES[best], source, *info,
            temperature_p10=_percentile(jackknife, 0.1) if jackknife else None,
            temperature_p90=_percentile(jackknife, 0.9) if jackknife else None,
        )

    def _supported(self, cohort):
        return (
            len(cohort) >= self.policy.min_series
            and sum(group.turns for group, _, _ in cohort) >= self.policy.min_turns
        )

    def _select(self, totals, mass):
        costs = [
            loss / mass + self.policy.log_temperature_penalty * log(temp) ** 2
            for loss, temp in zip(totals, TEMPERATURES)
        ]
        best = min(range(len(TEMPERATURES)), key=costs.__getitem__)
        return best if costs[FROZEN] - costs[best] >= self.policy.min_objective_gain else FROZEN


def evaluate(rows: Iterable[ScoredPick], sidecar: CalibrationSidecar) -> dict:
    """Compare the frozen and walk-forward probabilities on a supplied evaluation set."""
    groups = defaultdict(list)
    for row in rows:
        if len(row.probabilities):
            groups[row.split].append(row)
    return {split: _evaluation(group, sidecar) for split, group in groups.items()}


def _evaluation(rows, sidecar):
    decisions = {}
    collections = {"frozen": [], "walk_forward": []}
    for row in rows:
        key = (row.day, row.patch, row.series_id)
        if key not in decisions:
            decisions[key] = sidecar.decision_for(row.day, row.patch, series_id=row.series_id)
        decision = decisions[key]
        ranking = sorted(range(len(row.probabilities)), key=row.probabilities.__getitem__, reverse=True)
        adapted = adjust_probabilities(row.probabilities, decision.temperature)
        for name, probabilities in (("frozen", row.probabilities), ("walk_forward", adapted)):
            loss = -log(max(probabilities[row.target], 1e-300)) if row.target is not None else None
            collections[name].append((probabilities[ranking[0]], ranking[0] == row.target,
                                      row.target in ranking[:3], loss))
    temperatures = [decision.temperature for decision in decisions.values()]
    return {
        "turns": len(rows), "covered": sum(row.target is not None for row in rows),
        "series": len({row.series_id for row in rows}),
        "draft_dates": [min(row.day for row in rows), max(row.day for row in rows)],
        "decisions_by_source": dict(Counter(decision.source for decision in decisions.values())),
        "turns_retuned": sum(decisions[(row.day, row.patch, row.series_id)].temperature != 1.0 for row in rows),
        "evidence_through": max((decision.evidence_through for decision in decisions.values()
                                 if decision.evidence_through is not None), default=None),
        "temperature_percentiles": {f"p{quantile}": _percentile(temperatures, quantile / 100)
                                    for quantile in (10, 50, 90)},
        **{name: _metrics(outcomes) for name, outcomes in collections.items()},
    }


def _metrics(outcomes):
    bins = [[] for _ in range(10)]
    losses = []
    for confidence, hit, _, loss in outcomes:
        bins[min(int(confidence * 10), 9)].append((confidence, hit))
        if loss is not None:
            losses.append(loss)
    reliability = []
    for index, pairs in enumerate(bins):
        count = len(pairs)
        reliability.append({
            "range": [index / 10, (index + 1) / 10], "turns": count,
            "confidence": fsum(confidence for confidence, _ in pairs) / count if count else None,
            "accuracy": sum(hit for _, hit in pairs) / count if count else None,
        })
    return {
        "nll": fsum(losses) / len(losses) if losses else None,
        "top_choice_ece": fsum(row["turns"] * abs(row["confidence"] - row["accuracy"])
                               for row in reliability if row["turns"]) / len(outcomes),
        "hit_at_3": sum(hit for _, _, hit, _ in outcomes) / len(outcomes),
        "reliability": reliability,
    }
