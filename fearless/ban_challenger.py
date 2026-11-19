"""Opt-in second-phase ban ranker trained on historical opponent role scarcity."""
from __future__ import annotations

import argparse
from collections import OrderedDict, defaultdict, deque
from datetime import date as calendar_date, timedelta
from functools import lru_cache
import gzip
from hashlib import sha256
from itertools import groupby, permutations, takewhile
import json
import os
from pathlib import Path
from threading import Lock
from typing import Iterable, Mapping, Sequence

import numpy as np

from fearless.model import DraftForecaster, _method_scores, _sample_features, _softmax, _write_json
from fearless.model_features import (
    FEATURES, ROLES, TEST_START, DraftContext, History, TurnFeatures, evidence_date, observations,
    prepare_games, series_splits,
)
from fearless.model_metrics import Evaluation, ObservedTurn, _temperature, summarize
from fearless.rules import DraftAction, schedule

METHOD = 'second_phase_ban_challenger'
SCHEMA_VERSION = 1
ROLE_WINDOW = 84
ROLE_INDEX = {role: index for index, role in enumerate(ROLES)}
ROLE_FEATURES = (
    *(f'ban_pressure_{role}' for role in ROLES), 'weighted_open_role_fit', 'largest_role_pressure',
    'joint_open_role_pool_loss', 'weighted_best_replacement_gap', 'weighted_remaining_role_mass',
    'weighted_effective_pool_size', 'weighted_team_role_picks', 'weighted_starter_role_picks',
    'weighted_recent_starter_picks', 'largest_open_role_probability', 'revealed_role_entropy',
)
FROZEN_FILES = ('manifest.json', 'ban.cbm', 'history.jsonl.gz')


def second_phase_ban(day: str, first: str, second: str, current: Sequence[DraftAction]) -> bool:
    return day >= TEST_START and 12 <= len(current) < 16 and schedule(first, second)[len(current)][1] == 'ban'


@lru_cache(maxsize=6)
def _assignments(revealed: int):
    assignments = np.array(list(permutations(range(len(ROLES)), revealed)), dtype=np.intp)
    still_open = np.ones((len(assignments), len(ROLES)), dtype=bool)
    still_open[np.arange(len(assignments))[:, None], assignments] = False
    return assignments, still_open.astype(np.float64)


def role_openings(likelihood: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Marginalize open roles over all unique roles for the already picked champions."""
    assignments, open_roles = _assignments(len(likelihood))
    if not len(assignments):
        raise ValueError('A team cannot reveal more picks than it has roles.')
    weights = likelihood[np.arange(len(likelihood)), assignments].prod(axis=1)
    if not weights.any():
        weights = np.ones(len(assignments))
    probabilities = weights / weights.sum()
    return probabilities @ open_roles, (open_roles.T * probabilities) @ open_roles


def marginal_losses(comfort: np.ndarray, openings: np.ndarray, pairs: np.ndarray):
    """Compute lost one-role and distinct two-role option mass if each candidate is banned."""
    available = comfort.sum(axis=0)
    loss = comfort / np.maximum(available, 1e-12)
    pressure = loss * openings
    best = comfort.max(axis=0)
    runner_up = np.sort(comfort, axis=0)[-2] if len(comfort) > 1 else np.zeros(len(ROLES))
    replacement_gap = np.where(comfort == best, (best - runner_up) / np.maximum(available, 1e-12), 0)
    joint = np.zeros(len(comfort))
    for left in range(len(ROLES)):
        for right in range(left + 1, len(ROLES)):
            if pairs[left, right] <= 0:
                continue
            a, b = comfort[:, left], comfort[:, right]
            eligible = available[left] * available[right] - a @ b
            if eligible > 1e-12:
                removed = a * available[right] + b * available[left] - 2 * a * b
                joint += pairs[left, right] * np.clip(removed / eligible, 0, 1)
    diversity = available ** 2 / np.maximum(np.sum(comfort ** 2, axis=0), 1e-12)
    return pressure, joint, replacement_gap @ openings, diversity


def _distribution(array, axis):
    return array / np.maximum(np.sum(array, axis=axis, keepdims=True), 1e-12)


class RoleHistory:
    """Collect champion-role observations from completed drafts before the request date."""

    def __init__(self, champion_indices: Mapping[str, int]):
        self.indices = champion_indices
        self.empty = np.zeros((2, len(ROLES), len(champion_indices)), dtype=np.int32)
        self.population = self.empty.copy()
        self.teams = {}
        self.players = {}
        self.recent = deque()

    def before(self, day: str, games: Iterable[dict]):
        cutoff = (calendar_date.fromisoformat(day) - timedelta(days=ROLE_WINDOW)).isoformat()
        while self.recent and self.recent[0][0] < cutoff:
            _, observations_ = self.recent.popleft()
            self._update(observations_, 1, -1)
        for game in games:
            completed = evidence_date(game)
            if completed >= day:
                raise ValueError('Role evidence must come from a game completed before the draft date.')
            if not game.get('eligible'):
                continue
            observed = []
            picks = {(action['team'], action['champion']) for action in game['actions'] if action['kind'] == 'pick'}
            for team, roster in game.get('rosters', {}).items():
                for entry in roster:
                    champion, role = entry.get('champion'), entry.get('role')
                    if (team, champion) in picks and champion in self.indices and role in ROLE_INDEX:
                        observed.append((team, entry.get('player'), ROLE_INDEX[role], self.indices[champion]))
            self._update(observed, 0, 1)
            if observed and completed >= cutoff:
                self._update(observed, 1, 1)
                self.recent.append((completed, observed))

    def _update(self, observed, window, change):
        for team, player, role, champion in observed:
            self.population[window, role, champion] += change
            if team not in self.teams:
                self.teams[team] = self.empty.copy()
            self.teams[team][window, role, champion] += change
            if player:
                if player not in self.players:
                    self.players[player] = self.empty.copy()
                self.players[player][window, role, champion] += change

    @staticmethod
    def _recent_weighted(counts):
        return 0.35 * counts[0] + 0.65 * counts[1]

    def features(self, *, team: str, players: Sequence[str], revealed: Sequence[str], candidates: Sequence[str]):
        ids = [self.indices[name] for name in candidates]
        revealed_ids = [self.indices[name] for name in revealed if name in self.indices]
        population = self._recent_weighted(self.population)
        team_counts = self._recent_weighted(self.teams.get(team, self.empty))
        popularity = _distribution(population + 0.22, axis=1)
        team_profile = _distribution(team_counts + 18 * popularity, axis=1)
        known_players = [self.players[player] for player in players if player in self.players]
        pooled, weight = 2 * team_profile, np.full((len(ROLES), 1), 2.0)
        player_support = np.zeros_like(team_counts)
        recent_support = np.zeros_like(team_counts)
        for player_counts in known_players:
            histogram = self._recent_weighted(player_counts)
            totals = histogram.sum(axis=1, keepdims=True)
            starter_weight = _distribution(totals + 0.2, axis=0) * totals / (totals + 5)
            pooled += 5 * starter_weight * _distribution(histogram + 9 * team_profile, axis=1)
            weight += 5 * starter_weight
            player_support += player_counts[0]
            recent_support += player_counts[1]
        role_comfort = 0.28 * popularity + 0.25 * team_profile + 0.47 * pooled / weight
        # Champion role affinity stays useful when the shown pick is rare in this lineup.
        champ_affinity = _distribution(population + 0.20, axis=0)
        revealed_likelihood = (0.65 * champ_affinity + 0.35 * _distribution(role_comfort, axis=0)).T
        openings, open_pairs = role_openings(revealed_likelihood[revealed_ids])
        comfort = role_comfort[:, ids].T
        pressure, joint, gap, diversity = marginal_losses(comfort, openings, open_pairs)
        available = comfort.sum(axis=0)
        size = max(1, len(ids))
        entropy = -np.sum(openings * np.log(np.maximum(openings, 1e-12)))
        values = np.column_stack((
            pressure, champ_affinity[:, ids].T @ openings, pressure.max(axis=1), joint, gap,
            (available - comfort) @ openings, np.full(size, diversity @ openings),
            team_counts[:, ids].T @ openings, player_support[:, ids].T @ openings,
            recent_support[:, ids].T @ openings, np.full(size, openings.max()), np.full(size, entropy),
        ))
        return values.astype(np.float32)


def _extra(history, roles, context, turn, current, rosters):
    opponent = context.second if turn.team == context.first else context.first
    revealed = [action['champion'] for action in current
                if action['team'] == opponent and action['kind'] == 'pick' and action['champion'] is not None]
    return roles.features(team=opponent, players=history.player_ids(opponent, rosters),
                          revealed=revealed, candidates=turn.champions)


def _frozen_scores(frozen, turn):
    config = frozen.serving.get('ban', {'method': 'baseline', 'temperature': 1.0})
    raw = None
    if config['method'] != 'baseline':
        raw = frozen.models['ban'].predict(turn.values, thread_count=1)
    return _method_scores(config, turn.column('baseline'), raw)


def _blend(config, frozen_logits, new_logits):
    weight = config['weight']
    return ((1 - weight) * frozen_logits + weight * new_logits / config['challenger_temperature']) / config['temperature']


def _checksums(bundle):
    return {name: sha256((Path(bundle) / name).read_bytes()).hexdigest() for name in FROZEN_FILES}


class BanChallenger:
    """Wrap a frozen forecaster without changing its bundle or other action forecasts."""

    def __init__(self, frozen: DraftForecaster, ranker, config: dict):
        self.frozen, self.ranker, self.config = frozen, ranker, config
        self._role_history = OrderedDict()
        self._lock = Lock()

    @classmethod
    def load(cls, sidecar_dir: str | Path, frozen_bundle_dir: str | Path) -> BanChallenger:
        from catboost import CatBoostRanker

        directory = Path(sidecar_dir)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf8'))
        if manifest['version'] != SCHEMA_VERSION or manifest['features'] != list(FEATURES + ROLE_FEATURES):
            raise ValueError('The ban challenger uses a different feature schema.')
        if manifest['frozen_bundle_sha256'] != _checksums(frozen_bundle_dir):
            raise ValueError('The ban challenger was fitted against a different frozen bundle.')
        model = CatBoostRanker().load_model(str(directory / 'second_phase_ban.cbm'))
        return cls(DraftForecaster.load(frozen_bundle_dir), model, manifest['blend'])

    def _roles(self, day):
        with self._lock:
            if day not in self._role_history:
                history = RoleHistory(self.frozen.catalog.indices)
                history.before(day, takewhile(lambda game: evidence_date(game) < day, self.frozen.games))
                self._role_history[day] = history
                if len(self._role_history) > 4:
                    self._role_history.popitem(last=False)
            self._role_history.move_to_end(day)
            return self._role_history[day]

    def forecast(self, *, date: str, patch: str, region: str, first: str, second: str,
                 game_number: int, current: Sequence[DraftAction], locked: Iterable[str] = (),
                 pool: Iterable[str] | None = None, rosters: Mapping[str, Sequence[str]] | None = None,
                 limit: int = 3, fearless: bool = True) -> dict:
        options = dict(date=date, patch=patch, region=region, first=first, second=second, game_number=game_number,
                       current=current, locked=locked, pool=pool, rosters=rosters, fearless=fearless)
        if not second_phase_ban(date, first, second, current):
            return self.frozen.forecast(**options, limit=limit)
        options['locked'] = tuple(locked)
        options['pool'] = tuple(pool) if pool is not None else None
        frozen_result = self.frozen.forecast(**options, limit=len(self.frozen.catalog.names))
        if not frozen_result['choices']:
            return frozen_result
        catalog = self.frozen.catalog
        history = self.frozen._history(date)
        context = DraftContext(date, patch, region, first, second, game_number, fearless)
        prefix = [catalog.normalize_action(action) for action in current]
        locks = [catalog.canonical(champion) for champion in options['locked']]
        enabled = {catalog.canonical(champion) for champion in options['pool']} if options['pool'] is not None else None
        turn = history.features(context, prefix, locks, enabled, rosters)
        extra = _extra(history, self._roles(date), context, turn, prefix, rosters)
        raw = self.ranker.predict(np.column_stack((turn.values, extra)), thread_count=1)
        scores = _blend(self.config, _frozen_scores(self.frozen, turn), raw)
        # Preserve positive mass on every legal choice even with a near-deterministic fitted temperature.
        probabilities = _softmax(np.maximum(scores, scores.max() - 700))
        info = {catalog.canonical(choice['champion']): choice for choice in frozen_result['choices']}
        choices = []
        for index in np.argsort(-scores, kind='stable')[:max(0, limit)]:
            choice = info[turn.champions[index]]
            role_pressure = extra[index, :len(ROLES)]
            role = ROLES[role_pressure.argmax()]
            choices.append({**choice, 'probability': float(probabilities[index]),
                            'evidence': [*choice['evidence'], f'Highest estimated pressure on an open role: {role}']})
        return {**frozen_result, 'method': METHOD, 'choices': choices, 'calibrated': True,
                'calibration_through': '2026-03-27', 'challenger_weight': self.config['weight']}


def _replay(games, frozen):
    games = [frozen.catalog.normalize_game(game) for game in prepare_games(games)]
    splits = series_splits(games)
    history, roles = History(frozen.catalog), RoleHistory(frozen.catalog.indices)
    historical = iter(frozen.games)
    pending = next(historical, None)
    for day, day_games in groupby(games, lambda row: row['date']):
        available = []
        while pending is not None and evidence_date(pending) < day:
            available.append(pending)
            pending = next(historical, None)
        history.before(day, available)
        roles.before(day, available)
        for game in day_games:
            split = splits.get(game['series_id'])
            if split not in ('train', 'validation', 'test', 'later') or len(game['actions']) < 16:
                continue
            if not game.get('eligible'):
                continue
            context = DraftContext.from_game(game)
            for slot in range(12, 16):
                prefix, action = game['actions'][:slot], game['actions'][slot]
                turn = history.features(context, prefix, game['locked'])
                if (turn.kind, turn.team) != (action['kind'], action['team']):
                    raise ValueError('A recorded second-phase ban was outside the standard draft order.')
                outcome = action['champion']
                target = turn.champions.index(outcome) if outcome in turn.champions else None
                enabled = set(frozen.catalog.available(context.date, context.patch))
                locked = set(game['locked']) if context.fearless else set()
                used = {item['champion'] for item in prefix}
                illegal = frozenset(index for index, name in enumerate(turn.champions)
                                    if name not in enabled or name in locked or name in used)
                frozen_logits = _frozen_scores(frozen, turn) if split != 'train' and turn.champions else np.empty(0)
                if turn.champions:
                    extra = _extra(history, roles, context, turn, prefix, rosters=None)
                    turn.values = np.column_stack((turn.values, extra))
                else:
                    turn.values = np.empty((0, len(FEATURES + ROLE_FEATURES)), dtype=np.float32)
                yield split, turn, frozen_logits, ObservedTurn(
                    turn.values, target, outcome is not None, str(game['series_id']),
                    str(game.get('game_id') or context.game_number), 'second', context.region, context.patch,
                    context.game_number, 'fearless' if context.fearless else 'standard', illegal,
                )


def _fit(groups, *, iterations, device, threads, seed):
    from catboost import CatBoostRanker, Pool

    if not groups:
        raise ValueError('No covered, nontrivial second-phase ban turns in the 2025 training split.')
    sizes = np.fromiter((len(values) for values, _ in groups), dtype=np.int32)
    starts = np.r_[0, np.cumsum(sizes[:-1])]
    labels = np.zeros(sizes.sum(), dtype=np.float32)
    labels[starts + [label for _, label in groups]] = 1
    ids = np.repeat(np.arange(len(groups)), sizes)
    matrix = np.concatenate([values for values, _ in groups])
    options = dict(iterations=iterations, loss_function='PairLogit', learning_rate=0.045, depth=5,
                   l2_leaf_reg=12, random_seed=seed, thread_count=threads, task_type=device.upper(),
                   verbose=False, allow_writing_files=False, boosting_type='Plain', border_count=64)
    if device == 'gpu':
        options['devices'] = '0'
    ranker = CatBoostRanker(**options)
    ranker.fit(Pool(matrix, label=labels, group_id=ids, feature_names=list(FEATURES + ROLE_FEATURES), thread_count=threads))
    return ranker


def _choice_scores(ranker, rows, threads):
    if not rows:
        return Evaluation([]), np.empty(0), np.empty(0)
    evaluation = Evaluation([example for _, example in rows])
    frozen_logits = np.concatenate([scores for scores, _ in rows])
    scores = ranker.predict(evaluation.features, thread_count=threads) if len(evaluation.features) else np.empty(0)
    evaluation.features = None
    return evaluation, frozen_logits, scores


def _calibrate(validation, frozen_logits, raw_logits):
    if not validation.covered.any():
        raise ValueError('No covered second-phase bans in validation. Held-out selection requires validation games.')
    challenger_temperature = _temperature(validation, raw_logits)
    options = []
    for weight in (0.10, 0.20, 0.35, 0.50, 0.70, 1.00):
        config = dict(weight=weight, challenger_temperature=challenger_temperature, temperature=1.0)
        blended = _blend(config, frozen_logits, raw_logits)
        config['temperature'] = _temperature(validation, blended)
        hits1, hits3, _, nll, _ = validation.outcomes(blended / config['temperature'])
        quality = (int(hits3.sum()), int(hits1.sum()), -float(nll[validation.covered].mean()))
        options.append((quality, config))
    return max(options, key=lambda option: option[0])[1]


def _report(evaluation, frozen_logits, raw_logits, config, bootstrap, seed):
    comparison = summarize(evaluation, {
        'baseline': frozen_logits,
        'challenger_only': raw_logits / config['challenger_temperature'],
        'challenger': _blend(config, frozen_logits, raw_logits),
    }, 'baseline', bootstrap, seed)
    del comparison['promoted_method']
    comparison['baseline_model'] = 'frozen production ban forecaster'
    comparison['candidate'] = METHOD
    return comparison


def train(games, frozen_bundle, output, *, iterations=220, device='cpu', threads=4, seed=9827,
          negatives=64, bootstrap=1000):
    """Fit 2025 ban groups, calibrate on January–March 27, and seal an opt-in sidecar."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / 'manifest.json').exists():
        raise FileExistsError('Choose a new output directory for each frozen challenger fit.')
    frozen = DraftForecaster.load(frozen_bundle)
    rng = np.random.default_rng(seed)
    groups = []
    evaluations = defaultdict(list)
    for split, turn, frozen_logits, example in _replay(games, frozen):
        if split == 'train':
            if example.target is not None and len(turn.champions) > 1:
                groups.append(_sample_features(turn, example.target, negatives, rng))
        else:
            evaluations[split].append((frozen_logits, example))
    print(f'Fitting {len(groups)} named second-phase ban groups with CatBoost {device.upper()}', flush=True)
    ranker = _fit(groups, iterations=iterations, threads=threads, device=device, seed=seed)
    validation, frozen_logits, new_logits = _choice_scores(ranker, evaluations.pop('validation', []), threads)
    config = _calibrate(validation, frozen_logits, new_logits)
    print(f'Frozen validation blend: challenger weight {config["weight"]:.2f}', flush=True)
    manifest = {'version': SCHEMA_VERSION, 'method': METHOD, 'features': list(FEATURES + ROLE_FEATURES),
                'blend': config, 'frozen_bundle_sha256': _checksums(frozen_bundle),
                'training_period': ['2025-01-01', '2025-12-31'],
                'validation_period': ['2026-01-01', '2026-03-27'], 'valid_from': TEST_START,
                'phase': 'second-phase bans, zero-based positions 12–15', 'device': device,
                'iterations': iterations, 'negatives': negatives, 'seed': seed}
    ranker.save_model(str(output / 'second_phase_ban.cbm'))
    _write_json(output / 'manifest.json', manifest)
    reports = {'validation': _report(validation, frozen_logits, new_logits, config, bootstrap, seed)}
    for split in ('test', 'later'):
        evaluation, frozen_scores, new_scores = _choice_scores(ranker, evaluations.pop(split, []), threads)
        reports[split] = _report(evaluation, frozen_scores, new_scores, config, bootstrap, seed)
    metrics = {
        'protocol': {**manifest, 'recent_role_window_days': ROLE_WINDOW,
                     'selection': 'validation Hit@3, then Hit@1, then legal-target log loss',
                     'series_crossing_split_boundaries': 'excluded',
                     'historical_evidence': 'eligible games completed strictly before the draft UTC date in the frozen history',
                     'probabilities': 'conditional on naming a champion, over the full legal set',
                     'test_and_later': 'known diagnostics, excluded from all fitting and selection',
                     'bootstrap': f'{bootstrap} paired samples of full series'},
        'training_groups': len(groups), 'splits': reports,
    }
    _write_json(output / 'metrics.json', metrics)
    return metrics


def evaluate(games, challenger: BanChallenger, *, bootstrap=1000, seed=9827, threads=4):
    """Evaluate the frozen sidecar against the production model on whole supplied series."""
    rows = [(baseline, example) for split, _, baseline, example in _replay(games, challenger.frozen)
            if split in ('test', 'later')]
    evaluation, baseline, raw = _choice_scores(challenger.ranker, rows, threads)
    return _report(evaluation, baseline, raw, challenger.config, bootstrap, seed)


def _read_games(path):
    opener = gzip.open if path.suffix == '.gz' else Path.open
    with opener(path, 'rt', encoding='utf8') as source:
        return [json.loads(line) for line in source if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    training = commands.add_parser('train')
    evaluation = commands.add_parser('evaluate')
    for command in (training, evaluation):
        command.add_argument('--games', type=Path, required=True, help='normalized JSONL (gzip accepted)')
        command.add_argument('--frozen', type=Path, required=True, help='production frozen bundle directory')
        command.add_argument('--output', type=Path, required=True, help='training directory or evaluation JSON file')
        command.add_argument('--threads', type=int, default=int(os.environ.get('SLURM_CPUS_PER_TASK', '4')))
        command.add_argument('--bootstrap-samples', type=int, default=1000)
        command.add_argument('--seed', type=int, default=9827)
    training.add_argument('--device', choices=('cpu', 'gpu'), default='cpu')
    training.add_argument('--iterations', type=int, default=220)
    training.add_argument('--negatives', type=int, default=64)
    evaluation.add_argument('--sidecar', type=Path, required=True)
    args = parser.parse_args()
    if min(args.threads, args.bootstrap_samples, getattr(args, 'iterations', 1), getattr(args, 'negatives', 1)) < 1:
        parser.error('thread, sampling, iteration and negatives settings must be positive')
    rows = _read_games(args.games)
    options = dict(threads=args.threads, bootstrap=args.bootstrap_samples, seed=args.seed)
    if args.command == 'train':
        train(rows, args.frozen, args.output, iterations=args.iterations, device=args.device,
              negatives=args.negatives, **options)
    else:
        challenger = BanChallenger.load(args.sidecar, args.frozen)
        _write_json(args.output, evaluate(rows, challenger, **options))


if __name__ == '__main__':
    main()
