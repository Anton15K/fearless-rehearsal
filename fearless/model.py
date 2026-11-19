"""Train, evaluate and serve time-ordered professional draft rankers."""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict, defaultdict
import gzip
from itertools import groupby, takewhile
import json
import os
from pathlib import Path
from threading import Lock
from typing import Iterable, Mapping, Sequence

import numpy as np

from fearless.model_features import (
    ACTION_KINDS, FEATURES, LATER_START, RECENT_DAYS, TEST_START, TRAIN_START, VALIDATION_START,
    ChampionCatalog, DraftContext, History, evidence_date, observations, prepare_games, series_splits,
)
from fearless.model_metrics import Evaluation, ObservedTurn, choose_methods, summarize
from fearless.rules import DRAFT_LENGTH, DraftAction


BUNDLE_VERSION = 1
BASE_FEATURES = [*range(FEATURES.index('team_action_rate')), *range(FEATURES.index('slot_number'), len(FEATURES))]


def _softmax(scores):
    scores = scores.astype(np.float64, copy=False)
    scaled = np.exp(scores - scores.max())
    return scaled / scaled.sum()


def _method_scores(config, baseline, learned=None):
    if config['method'] == 'baseline':
        return baseline / config['temperature']
    if config['method'] in ('learned', 'learned_meta_only', 'learned_joint'):
        return learned / config['temperature']
    weight = config['weight']
    values = weight * learned / config['learned_temperature']
    values += (1 - weight) * baseline / config['baseline_temperature']
    return values / config['temperature']


class DraftForecaster:
    """Reload an evaluated bundle and forecast the next action over legal choices."""

    def __init__(self, catalog: ChampionCatalog, games: Iterable[dict], *, models=None, serving=None):
        self.catalog = catalog
        self.games = observations(catalog.normalize_game(game) for game in prepare_games(games))
        self.models = models or {}
        self.serving = serving or {}
        self._histories = OrderedDict()
        self._history_lock = Lock()

    @classmethod
    def load(cls, directory: str | Path) -> DraftForecaster:
        directory = Path(directory)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf8'))
        if manifest['version'] != BUNDLE_VERSION or manifest['features'] != list(FEATURES):
            raise ValueError('The saved model uses an incompatible draft feature schema.')
        models = {}
        if manifest['models']:
            from catboost import CatBoostRanker

            for kind, name in manifest['models'].items():
                models[kind] = CatBoostRanker().load_model(str(directory / name))
        with gzip.open(directory / 'history.jsonl.gz', 'rt', encoding='utf8') as source:
            games = [json.loads(line) for line in source if line.strip()]
        catalog = ChampionCatalog.from_json(manifest['catalog'])
        return cls(catalog, games, models=models, serving=manifest['serving'])

    def _history(self, day):
        with self._history_lock:
            if day not in self._histories:
                history = History(self.catalog)
                history.before(day, takewhile(lambda game: evidence_date(game) < day, self.games))
                self._histories[day] = history
                if len(self._histories) > 3:
                    self._histories.popitem(last=False)
            self._histories.move_to_end(day)
            return self._histories[day]

    def forecast(self, *, date: str, patch: str, region: str, first: str, second: str,
                 game_number: int, current: Sequence[DraftAction], locked: Iterable[str] = (),
                 pool: Iterable[str] | None = None, rosters: Mapping[str, Sequence[str]] | None = None,
                 limit: int = 3, fearless: bool = True) -> dict:
        """Use only records dated before the request, plus explicitly supplied draft state."""
        if len(current) >= DRAFT_LENGTH:
            return {'status': 'complete', 'choices': [], 'legal_candidates': 0}
        context = DraftContext(date, patch, region, first, second, game_number, fearless)
        history = self._history(date)
        current = [self.catalog.normalize_action(action) for action in current]
        locked = [self.catalog.canonical(champion) for champion in locked]
        chosen_pool = None
        if pool is not None:
            chosen_pool = {self.catalog.canonical(champion): champion for champion in pool}
        turn = history.features(context, current, locked, chosen_pool, rosters)
        roster_evidence = {team: history.roster_evidence(team, rosters) for team in (first, second)}
        config = self.serving.get(turn.kind) if date >= TEST_START else None
        config = config or {'method': 'baseline', 'temperature': 1.0}
        calibrated = bool(turn.champions) and config.get('calibrated', False)
        result = {
            'status': 'scored' if turn.champions else 'no_candidates', 'kind': turn.kind,
            'team': turn.team, 'as_of': history.as_of, 'legal_candidates': len(turn.champions),
            'roster_players': {team: evidence['players'] for team, evidence in roster_evidence.items()},
            'roster_evidence': roster_evidence, 'calibrated': calibrated,
            'calibration_through': '2026-03-27' if calibrated else None,
            'probability_condition': 'named_champion_action', 'choices': [],
        }
        if not turn.champions:
            return {**result, 'method': 'unavailable'}
        learned = None
        if config['method'] != 'baseline':
            learned = self.models[turn.kind].predict(turn.values, thread_count=1)
        scores = _method_scores(config, turn.column('baseline'), learned)
        probabilities = _softmax(scores)
        ordered = np.argsort(-scores, kind='stable')[:max(0, limit)]
        result['method'] = config['method']
        for index in ordered:
            champion = turn.champions[index]
            display = chosen_pool.get(champion, champion) if chosen_pool is not None else champion
            values = {name: float(value) for name, value in zip(FEATURES, turn.values[index], strict=True)}
            evidence = []
            player_count = int(values['team_player_count' if turn.kind == 'pick' else 'opponent_player_count'])
            role_count = int(values['known_role_count'])
            side = 'Team' if turn.kind == 'pick' else 'Opponent'
            if player_count:
                evidence.append(f'{side} historical player picks: {player_count}')
            if role_count:
                evidence.append(f'Verified role observations: {role_count}')
            pair_count = int(values['ally_pick_pair_support'] + values['enemy_pick_pair_support'])
            if pair_count:
                evidence.append(f'Historical continuations after current picks: {pair_count}')
            if values['patch_support']:
                evidence.append(f'Prior legal {turn.kind} turns on this patch: {int(values["patch_support"])}')
            if not evidence and history.as_of is not None:
                evidence.append('Historical population prior')
            games, roles = history.champion_evidence(champion, turn.kind)
            result['choices'].append({'champion': display, 'probability': float(probabilities[index]),
                                      'evidence': evidence, 'games': games, 'roles': roles})
        return result


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf8')
    temporary.replace(path)


def _sample_features(turn, label, negatives, rng):
    if len(turn.champions) <= negatives + 1:
        return turn.values, label
    hard = np.argsort(-turn.column('baseline'), kind='stable')
    hard = hard[hard != label][:negatives // 2]
    remaining = np.ones(len(turn.champions), dtype=bool)
    remaining[hard] = False
    remaining[label] = False
    random = rng.choice(np.flatnonzero(remaining), size=negatives - len(hard), replace=False)
    chosen = np.concatenate(([label], hard, random))
    return turn.values[chosen], 0


def _collect(games, catalog, negatives, seed):
    history = History(catalog)
    splits = series_splits(games)
    rng = np.random.default_rng(seed)
    source = iter(observations(games))
    pending = next(source, None)
    training = {kind: [] for kind in ACTION_KINDS}
    evaluations = defaultdict(list)
    counts = defaultdict(Counter)
    for day, same_day in groupby(games, lambda game: game['date']):
        available = []
        while pending is not None and evidence_date(pending) < day:
            available.append(pending)
            pending = next(source, None)
        history.before(day, available)
        for game in same_day:
            if not game.get('eligible'):
                counts['eligibility']['excluded_games'] += 1
                continue
            split = splits[game['series_id']]
            counts[split]['games'] += 1
            if split in ('history', 'cross_boundary'):
                continue
            context = DraftContext.from_game(game)
            enabled = set(catalog.available(context.date, context.patch))
            locked = set(game['locked']) if context.fearless else set()
            for position, action in enumerate(game['actions']):
                turn = history.features(context, game['actions'][:position], game['locked'])
                if (turn.team, turn.kind) != (action['team'], action['kind']):
                    raise ValueError('An eligible game has actions outside the tournament draft order.')
                champion = action['champion']
                position_in_pool = turn.champions.index(champion) if champion in turn.champions else None
                counts[split][f'{turn.kind}_turns'] += 1
                if position_in_pool is not None:
                    counts[split][f'{turn.kind}_legal_targets'] += 1
                if split == 'train':
                    if position_in_pool is not None and len(turn.champions) > 1:
                        training[turn.kind].append(_sample_features(turn, position_in_pool, negatives, rng))
                else:
                    phase = 'opening' if position < 12 else 'second'
                    used = {action['champion'] for action in game['actions'][:position]}
                    illegal = frozenset(index for index, name in enumerate(turn.champions)
                                        if name not in enabled or name in locked or name in used)
                    evaluations[split, turn.kind].append(ObservedTurn(
                        turn.values, position_in_pool, champion is not None,
                        str(game['series_id']), str(game.get('game_id') or game['game_number']),
                        phase, context.region, context.patch, context.game_number,
                        'fearless' if context.fearless else 'standard', illegal,
                    ))
    return training, evaluations, dict(counts)


def _fit(groups, *, iterations, threads, device, seed, kind, ablate=False):
    from catboost import CatBoostRanker, Pool

    if not groups:
        raise ValueError(f'No training {kind} actions have a legal target and at least two choices.')
    sizes = np.fromiter((len(values) for values, _ in groups), dtype=np.int32)
    starts = np.r_[0, np.cumsum(sizes[:-1])]
    labels = np.zeros(sizes.sum(), dtype=np.float32)
    labels[starts + [label for _, label in groups]] = 1
    ids = np.repeat(np.arange(len(groups)), sizes)
    matrix = np.concatenate([values for values, _ in groups])
    options = dict(loss_function='PairLogit', learning_rate=0.055, depth=6, l2_leaf_reg=8,
                   random_seed=seed, thread_count=threads, task_type=device.upper(), verbose=False,
                   allow_writing_files=False, boosting_type='Plain', border_count=64)
    if device == 'gpu':
        options['devices'] = '0'
    ranker = CatBoostRanker(iterations=iterations, **options)
    ranker.fit(Pool(matrix, label=labels, group_id=ids, feature_names=list(FEATURES), thread_count=threads))
    ablation = None
    if ablate:
        ablation = CatBoostRanker(iterations=iterations, **options)
        ablation.fit(Pool(matrix[:, BASE_FEATURES], label=labels, group_id=ids, thread_count=threads))
    return ranker, ablation


def _history_payload(game):
    fields = ('date', 'series_id', 'game_id', 'game_number', 'patch', 'region', 'first', 'second',
              'actions', 'locked', 'fearless', 'eligible', 'drafted_at', 'finished_at')
    payload = {key: game[key] for key in fields if key in game}
    payload['rosters'] = {team: [{key: entry.get(key) for key in ('player', 'champion', 'role')} for entry in roster]
                          for team, roster in game.get('rosters', {}).items()}
    return payload


def train(games: Iterable[dict], output: str | Path, *, catalog: ChampionCatalog | None = None,
          device: str = 'cpu', iterations: int = 300, negatives: int = 48, threads: int = 4,
          bootstrap_samples: int = 1000, seed: int = 10429, joint_experiment: bool = False) -> dict:
    """Fit on 2025, freeze choices on validation, and report held-out results."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    games = prepare_games(games)
    catalog = catalog or ChampionCatalog.from_games(games)
    games = [catalog.normalize_game(game) for game in games]
    print(f'Replaying {len(games)} normalized games using {catalog.source}', flush=True)
    training, evaluation_turns, counts = _collect(games, catalog, negatives, seed)
    print(f'Training eligible groups: {len(training["pick"])} picks and {len(training["ban"])} bans', flush=True)
    metrics = {
        'protocol': {
            'train': [TRAIN_START, '2025-12-31'], 'validation': [VALIDATION_START, '2026-03-27'],
            'test': [TEST_START, '2026-06-14'], 'later': [LATER_START, None],
            'series_crossing_split_boundaries': 'excluded from fitted and scored turns',
            'historical_statistics': 'eligible games from strictly earlier UTC dates; updated chronologically across all periods',
            'prior_series_information': 'earlier game picks used for Fearless legality',
            'training_choice_sets': f'positive plus up to {negatives} deterministic hard and uniform legal negatives',
            'evaluation_choice_sets': 'full dated enabled champion pool minus earlier picks and bans',
            'candidate_catalog': catalog.source, 'recent_days': RECENT_DAYS,
            'selection': 'best validation Hit@3, then Hit@1 and legal-target log loss; strictly higher Hit@3 required for promotion',
            'calibration': 'per-action validation temperature scaling over full legal choice sets',
            'probability_condition': 'a named champion is chosen; skipped bans are counted separately',
            'objective': 'CatBoost PairLogit separate pick and ban models',
            'meta_only': 'same ranker and iteration count with team, player, role and pair feature blocks removed',
            'joint_experiment': ('one shared pick and ban ranker, calibrated separately by action kind, diagnostic only'
                                 if joint_experiment else 'disabled'),
            'uncertainty': f'{bootstrap_samples} paired bootstrap samples clustered by series',
        },
        'training': {'iterations': iterations, 'threads': threads, 'device': device, 'seed': seed,
                     'groups': {kind: len(groups) for kind, groups in training.items()}},
        'game_and_turn_counts': counts,
        'promotion': {}, 'splits': {split: {} for split in ('validation', 'test', 'later')},
    }
    serving, names = {}, {}
    joint_ranker = None
    if joint_experiment:
        print(f'Fitting shared pick and ban ranker on {device.upper()}', flush=True)
        joint_ranker, _ = _fit(training['pick'] + training['ban'], iterations=iterations, threads=threads,
                               device=device, seed=seed, kind='joint pick and ban')
    for kind in ACTION_KINDS:
        print(f'Fitting {kind} rankers on {device.upper()}', flush=True)
        ranker, ablation = _fit(training.pop(kind), iterations=iterations, threads=threads,
                               device=device, seed=seed, kind=kind, ablate=True)
        filename = f'{kind}.cbm'
        ranker.save_model(str(output / filename))
        names[kind] = filename
        results = {}
        for split in metrics['splits']:
            evaluations = Evaluation(evaluation_turns.pop((split, kind), []))
            matrix = evaluations.features
            if len(matrix):
                learned = ranker.predict(matrix, thread_count=threads)
                meta = ablation.predict(matrix[:, BASE_FEATURES], thread_count=threads)
            else:
                learned = meta = np.empty(0)
            joint = joint_ranker.predict(matrix, thread_count=threads) if joint_ranker is not None and len(matrix) else np.empty(0)
            evaluations.features = None
            results[split] = (evaluations, learned, meta, joint)
        validation, learned, meta, joint = results['validation']
        configurations, serving[kind] = choose_methods(validation, learned, meta,
                                                        joint if joint_experiment else None)
        serving[kind] = {**serving[kind], 'calibrated': bool(validation.covered.any())}
        metrics['promotion'][kind] = serving[kind]
        for split, (evaluations, learned, meta, joint) in results.items():
            alternatives = {'learned_meta_only': meta, 'learned_joint': joint}
            logits = {name: _method_scores(config, evaluations.baseline, alternatives.get(name, learned))
                      for name, config in configurations.items()}
            metrics['splits'][split][kind] = summarize(evaluations, logits, serving[kind]['method'],
                                                        bootstrap_samples, seed)
        print(f'{kind} serving method: {serving[kind]["method"]}', flush=True)
    temporary = output / 'history.jsonl.gz.tmp'
    with gzip.open(temporary, 'wt', encoding='utf8', compresslevel=5) as history:
        for game in games:
            if game.get('eligible'):
                history.write(json.dumps(_history_payload(game), separators=(',', ':')) + '\n')
    temporary.replace(output / 'history.jsonl.gz')
    _write_json(output / 'metrics.json', metrics)
    _write_json(output / 'manifest.json', {
        'version': BUNDLE_VERSION, 'features': list(FEATURES), 'models': names,
        'catalog': catalog.to_json(), 'serving': serving, 'trained_for_dates_from': TEST_START,
    })
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    training = commands.add_parser('train', help='train and evaluate on normalized GRID game JSONL')
    training.add_argument('--games', type=Path, required=True, help='normalized JSONL; gzip accepted')
    training.add_argument('--catalog', type=Path, help='dated release mapping or patch to champion-list JSON')
    training.add_argument('--output', type=Path, required=True, help='private bundle directory')
    training.add_argument('--device', choices=('cpu', 'gpu'), default='cpu')
    training.add_argument('--iterations', type=int, default=300)
    training.add_argument('--negatives', type=int, default=48)
    training.add_argument('--threads', type=int, default=int(os.environ.get('SLURM_CPUS_PER_TASK', '4')))
    training.add_argument('--bootstrap-samples', type=int, default=1000)
    training.add_argument('--seed', type=int, default=10429)
    training.add_argument('--joint-experiment', action='store_true', help='evaluate an auxiliary shared pick and ban ranker')
    args = parser.parse_args()
    if min(args.iterations, args.negatives, args.threads, args.bootstrap_samples) < 1:
        parser.error('iterations, negatives, threads and bootstrap samples must be positive')
    opener = gzip.open if args.games.suffix == '.gz' else Path.open
    with opener(args.games, 'rt', encoding='utf8') as source:
        rows = [json.loads(line) for line in source if line.strip()]
    catalog = ChampionCatalog.from_json(json.loads(args.catalog.read_text(encoding='utf8'))) if args.catalog else None
    train(rows, args.output, catalog=catalog, device=args.device, iterations=args.iterations,
          negatives=args.negatives, threads=args.threads, bootstrap_samples=args.bootstrap_samples, seed=args.seed,
          joint_experiment=args.joint_experiment)


if __name__ == '__main__':
    main()
