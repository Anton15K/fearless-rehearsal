"""Choice-set calibration, validation-only promotion and paired series reports."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from fearless.model_features import FEATURES


@dataclass
class ObservedTurn:
    values: np.ndarray | None
    target: int | None
    labeled: bool
    series: str
    game: str
    phase: str
    region: str
    patch: str
    game_number: int
    format: str
    illegal: frozenset[int]


class Evaluation:
    def __init__(self, turns):
        self.turns = turns
        sizes = np.fromiter((len(turn.values) for turn in turns), dtype=np.intp)
        self.offsets = np.r_[0, sizes.cumsum()]
        self.nonempty = np.flatnonzero(sizes)
        self.labeled = np.array([turn.labeled for turn in turns], dtype=bool)
        self.covered = np.array([turn.target is not None for turn in turns], dtype=bool)
        self.covered_nonempty = self.covered[self.nonempty]
        self.features = np.concatenate([turn.values for turn in turns]) if turns else np.empty((0, len(FEATURES)))
        self.baseline = self.features[:, FEATURES.index('baseline')].copy()
        for turn in turns:
            turn.values = None
        self.targets = np.array([self.offsets[index] + turn.target for index, turn in enumerate(turns)
                                 if turn.target is not None], dtype=np.intp)

    def log_losses(self, logits):
        if len(logits) == 0:
            return np.empty(0)
        starts = self.offsets[self.nonempty]
        maximum = np.maximum.reduceat(logits, starts)
        normalized = logits - np.repeat(maximum, np.diff(np.r_[starts, len(logits)]))
        log_denom = np.log(np.add.reduceat(np.exp(normalized), starts))
        return log_denom[self.covered_nonempty] - normalized[self.targets]

    def outcomes(self, logits):
        hits1 = np.zeros(len(self.turns), dtype=bool)
        hits3 = np.zeros(len(self.turns), dtype=bool)
        confidence = np.full(len(self.turns), np.nan)
        nll = np.full(len(self.turns), np.nan)
        illegal = np.zeros(len(self.turns), dtype=np.uint8)
        nll[self.covered] = self.log_losses(logits)
        for index in self.nonempty:
            start, stop = self.offsets[index:index + 2]
            scores = logits[start:stop]
            choice = self.turns[index].target
            confidence[index] = 1 / np.exp(scores - scores.max()).sum()
            if self.turns[index].illegal:
                top = np.argsort(-scores, kind='stable')[:3]
                illegal[index] = sum(choice in self.turns[index].illegal for choice in top)
            if choice is not None:
                rank = np.count_nonzero(scores > scores[choice]) + np.count_nonzero(scores[:choice] == scores[choice])
                hits1[index] = rank == 0
                hits3[index] = rank < 3
        return hits1, hits3, confidence, nll, illegal


def _temperature(evaluation, scores):
    if not evaluation.covered.any():
        return 1.0
    from scipy.optimize import minimize_scalar

    def objective(log_temperature):
        return float(evaluation.log_losses(scores / np.exp(log_temperature)).mean())

    fitted = minimize_scalar(objective, bounds=(-3, 3), method='bounded', options={'xatol': 0.01})
    return float(np.exp(fitted.x))


def choose_methods(validation, learned, meta, joint=None):
    """Fit scores and promote a method using only the validation choice sets."""
    baseline = validation.baseline
    base_temperature = _temperature(validation, baseline)
    learned_temperature = _temperature(validation, learned)
    configurations = {
        'baseline': {'method': 'baseline', 'temperature': base_temperature},
        'learned': {'method': 'learned', 'temperature': learned_temperature},
        'learned_meta_only': {'method': 'learned_meta_only', 'temperature': _temperature(validation, meta)},
    }
    if joint is not None:
        configurations['learned_joint'] = {'method': 'learned_joint', 'temperature': _temperature(validation, joint)}

    def quality(scores):
        hits1, hits3, _, losses, _ = validation.outcomes(scores)
        return int(hits3.sum()), int(hits1.sum()), -float(losses[validation.covered].mean()) if validation.covered.any() else 0

    base_scores = baseline / base_temperature
    learned_scores = learned / learned_temperature
    best_blend = None
    best_blend_quality = None
    for weight in (0.2, 0.35, 0.5, 0.65, 0.8):
        blend = (1 - weight) * base_scores + weight * learned_scores
        temperature = _temperature(validation, blend)
        q = quality(blend / temperature)
        if best_blend_quality is None or q > best_blend_quality:
            best_blend_quality = q
            best_blend = {'method': 'blend', 'temperature': temperature, 'weight': weight,
                          'baseline_temperature': base_temperature, 'learned_temperature': learned_temperature}
    configurations['blend'] = best_blend
    validation_outcomes = {'baseline': quality(base_scores), 'learned': quality(learned_scores),
                           'blend': best_blend_quality}
    chosen = max(validation_outcomes, key=validation_outcomes.get)
    if validation_outcomes[chosen][0] <= validation_outcomes['baseline'][0]:
        chosen = 'baseline'
    return configurations, configurations[chosen]


def _confidence_interval(evaluation, indices, left, right, samples, rng):
    series_turns = defaultdict(list)
    for index in indices:
        series_turns[evaluation.turns[index].series].append(index)
    if len(series_turns) < 2:
        return None
    difference = left.astype(np.int32) - right.astype(np.int32)
    sums = np.array([difference[group].sum() for group in series_turns.values()])
    sizes = np.array([len(group) for group in series_turns.values()])
    drawn = rng.integers(0, len(sizes), size=(samples, len(sizes)))
    estimates = sums[drawn].sum(axis=1) / sizes[drawn].sum(axis=1)
    return [float(bound) for bound in np.quantile(estimates, (0.025, 0.975))]


def _calibration(hits, confidence):
    if not len(confidence):
        return None
    ece = 0
    points = []
    bins = np.minimum((confidence * 10).astype(np.int8), 9)
    for index in range(10):
        in_bin = bins == index
        count = int(in_bin.sum())
        if count:
            mean_confidence = float(confidence[in_bin].mean())
            accuracy = float(hits[in_bin].mean())
            ece += count / len(bins) * abs(mean_confidence - accuracy)
            points.append({'range': [index / 10, (index + 1) / 10], 'turns': count,
                           'confidence': mean_confidence, 'accuracy': accuracy})
    return {'top1_ece': ece, 'top1_brier': float(np.mean((confidence - hits) ** 2)), 'bins': points}


def _report(evaluation, indices, outcomes, samples, seed, detail):
    labeled = indices[evaluation.labeled[indices]]
    covered = labeled[evaluation.covered[labeled]]
    sizes = np.diff(evaluation.offsets)[indices]
    valid = int(np.count_nonzero(sizes))
    result = {'counts': {'turns': len(indices), 'labeled_turns': len(labeled), 'legal_targets': len(covered),
                         'null_targets': len(indices) - len(labeled), 'uncovered_targets': len(labeled) - len(covered),
                         'scored_turns': valid, 'series': len({evaluation.turns[index].series for index in indices}),
                         'games': len({(evaluation.turns[index].series, evaluation.turns[index].game) for index in indices}),
                         'mean_legal_candidates': float(sizes.mean()) if len(sizes) else None},
              'coverage': len(covered) / len(labeled) if len(labeled) else None, 'methods': {}}
    baseline1, baseline3, _, _, _ = outcomes['baseline']
    for name, (hits1, hits3, confidence, losses, illegal) in outcomes.items():
        lift1 = float(hits1[labeled].mean() - baseline1[labeled].mean()) if len(labeled) else None
        lift3 = float(hits3[labeled].mean() - baseline3[labeled].mean()) if len(labeled) else None
        row = {'hit_at_1': float(hits1[labeled].mean()) if len(labeled) else None,
               'hit_at_3': float(hits3[labeled].mean()) if len(labeled) else None,
               'top1_hits': int(hits1[labeled].sum()), 'top3_hits': int(hits3[labeled].sum()),
               'hit_at_1_lift': lift1, 'hit_at_3_lift': lift3,
               'log_loss_legal_targets': float(losses[covered].mean()) if len(covered) else None}
        if detail:
            scored = labeled[np.isfinite(confidence[labeled])]
            row.update({'illegal_recommendations': int(illegal[indices].sum()),
                        'recommendations': int(np.minimum(sizes, 3).sum()),
                        'calibration': _calibration(hits1[scored], confidence[scored]),
                        'hit_at_3_lift_paired_series_ci95': _confidence_interval(
                            evaluation, labeled, hits3, baseline3, samples, np.random.default_rng(seed)),
                        'hit_at_1_lift_paired_series_ci95': _confidence_interval(
                            evaluation, labeled, hits1, baseline1, samples, np.random.default_rng(seed))})
        result['methods'][name] = row
    return result


def summarize(evaluation, logits, promoted, bootstrap_samples, seed):
    outcomes = {name: evaluation.outcomes(scores) for name, scores in logits.items()}
    overall = _report(evaluation, np.arange(len(evaluation.turns), dtype=np.intp), outcomes,
                      bootstrap_samples, seed, detail=True)
    overall['promoted_method'] = promoted
    slices = defaultdict(lambda: defaultdict(list))
    for index, turn in enumerate(evaluation.turns):
        for dimension in ('phase', 'region', 'patch', 'game_number', 'format'):
            slices[dimension][str(getattr(turn, dimension))].append(index)
    overall['slices'] = {dimension: {value: _report(evaluation, np.array(indices), outcomes, bootstrap_samples,
                                                   seed, detail=False)
                                    for value, indices in values.items()}
                         for dimension, values in slices.items()}
    return overall
