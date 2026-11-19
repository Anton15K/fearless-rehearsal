"""Dated candidate pools and shared training and serving draft features."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
import unicodedata
from typing import Iterable, Mapping, Sequence

import numpy as np

from fearless.rules import DRAFT_LENGTH, DraftAction, legal_champions, schedule, series_locks


TRAIN_START = '2025-01-01'
VALIDATION_START = '2026-01-01'
TEST_START = '2026-03-28'
LATER_START = '2026-06-15'
RECENT_DAYS = 56
ROLES = ('top', 'jungle', 'mid', 'bot', 'support')
ACTION_KINDS = ('pick', 'ban')
FEATURES = (
    'baseline', 'global_rate', 'recent_rate', 'older_rate', 'region_rate', 'patch_rate',
    'region_patch_rate', 'slot_rate', 'game_rate', 'format_rate', 'global_support',
    'recent_support', 'regional_support', 'patch_support', 'global_pick_rate', 'global_ban_rate',
    'team_action_rate', 'team_pick_rate', 'opponent_pick_rate', 'targeted_ban_rate',
    'team_recent_pick_rate', 'opponent_recent_pick_rate', 'team_pick_count', 'opponent_pick_count',
    'team_player_rate', 'opponent_player_rate', 'team_player_count', 'opponent_player_count',
    'team_recent_player_count', 'opponent_recent_player_count',
    'known_role_fit', 'known_role_flex', 'known_role_count',
    'ally_pick_pair', 'enemy_pick_pair', 'ally_ban_pair', 'enemy_ban_pair',
    'ally_pick_pair_support', 'enemy_pick_pair_support', 'ally_ban_pair_support', 'enemy_ban_pair_support',
    'slot_number', 'draft_position', 'game_number', 'fearless', 'locked_count', 'legal_count',
)


def split_for_date(day: str) -> str:
    if day < TRAIN_START:
        return 'history'
    if day < VALIDATION_START:
        return 'train'
    if day < TEST_START:
        return 'validation'
    if day < LATER_START:
        return 'test'
    return 'later'


def _utc_time(timestamp: str | None, day: str) -> datetime:
    value = datetime.fromisoformat(timestamp or day)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evidence_date(game: dict) -> str:
    """Use the later of the draft date and observed completion in UTC."""
    return max(game['date'], _utc_time(game.get('finished_at'), game['date']).date().isoformat())


def observations(games: Iterable[dict]):
    eligible = (game for game in games if game.get('eligible'))
    return sorted(eligible, key=lambda game: (evidence_date(game), _utc_time(game.get('drafted_at'), game['date'])))


def _patch_key(value: str) -> tuple[int, int] | None:
    match = re.search(r'(\d+)\.(\d+)', value)
    if not match:
        return None
    major, minor = map(int, match.groups())
    # Published season patch labels 25.x and 26.x also appear as client 15.x and 16.x.
    return (major - 10 if 25 <= major <= 29 else major, minor)


class ChampionCatalog:
    """A champion's first known release date and optional minimum competitive patch."""

    def __init__(self, releases: Mapping[str, str | dict], source: str = 'provided', patches=None):
        self.releases = dict(releases)
        self.source = source
        self.patches = {str(patch): tuple(sorted(set(names))) for patch, names in (patches or {}).items()}
        self._patches = {_patch_key(patch): names for patch, names in self.patches.items() if _patch_key(patch)}
        self.names = tuple(sorted(set(releases).union(*(set(names) for names in self.patches.values()))))
        self.indices = {name: index for index, name in enumerate(self.names)}
        self.aliases = {self._alias_key(name): name for name in self.names}
        self._availability = {}

    @staticmethod
    def _alias_key(name):
        normal = unicodedata.normalize('NFKD', name).casefold()
        normal = ''.join(character for character in normal if character.isalnum())
        return {'monkeyking': 'wukong', 'nunu': 'nunuwillump', 'renata': 'renataglasc'}.get(normal, normal)

    @classmethod
    def from_json(cls, value: Mapping) -> ChampionCatalog:
        if 'releases' in value or 'patches' in value:
            return cls(value.get('releases', {}), value.get('source', 'provided'), value.get('patches'))
        if value and all(isinstance(names, list) for names in value.values()):
            return cls({}, 'patch champion catalog', value)
        return cls(value)

    def to_json(self):
        return {'releases': self.releases, 'patches': self.patches, 'source': self.source}

    def canonical(self, name: str | None) -> str | None:
        if name is None or name in self.indices:
            return name
        return self.aliases.get(self._alias_key(name), name)

    def normalize_action(self, action: DraftAction) -> DraftAction:
        return {**action, 'champion': self.canonical(action['champion'])}

    def normalize_game(self, game: dict) -> dict:
        return {**game,
                'actions': [self.normalize_action(action) for action in game['actions']],
                'locked': sorted({self.canonical(name) for name in game.get('locked', ())}),
                **({'disabled': [self.canonical(name) for name in game['disabled']]} if game.get('disabled') else {}),
                'rosters': {team: [{**entry, 'champion': self.canonical(entry.get('champion'))} for entry in roster]
                            for team, roster in game.get('rosters', {}).items()}}

    @classmethod
    def from_games(cls, games: Sequence[dict]) -> ChampionCatalog:
        earliest = {}
        for game in games:
            if game.get('eligible'):
                next_day = (date.fromisoformat(evidence_date(game)) + timedelta(days=1)).isoformat()
                for action in game['actions']:
                    if action['champion'] is not None:
                        observed = earliest.setdefault(action['champion'], next_day)
                        earliest[action['champion']] = min(observed, next_day)
        return cls(earliest, 'prior GRID appearances')

    def available(self, day: str, patch: str) -> tuple[str, ...]:
        key = (day, patch)
        if key not in self._availability:
            target_patch = _patch_key(patch)
            if self._patches:
                # On an uncovered patch, use the newest older catalog by patch number.
                latest = max((known for known in self._patches if target_patch and known <= target_patch), default=None)
                self._availability[key] = self._patches.get(latest, ())
                return self._availability[key]
            choices = []
            for name in self.names:
                release = self.releases[name]
                released = release.get('date', '') if isinstance(release, dict) else release
                first_patch = _patch_key(release.get('patch', '')) if isinstance(release, dict) else None
                if released <= day and (first_patch is None or (
                        target_patch is not None and first_patch <= target_patch)):
                    choices.append(name)
            self._availability[key] = tuple(choices)
        return self._availability[key]


@dataclass(frozen=True)
class DraftContext:
    date: str
    patch: str
    region: str
    first: str
    second: str
    game_number: int
    fearless: bool

    @classmethod
    def from_game(cls, game: dict) -> DraftContext:
        return cls(game['date'], game.get('patch') or 'Unknown', game.get('region') or 'Unknown',
                   game['first'], game['second'], game['game_number'], game.get('fearless', True))


@dataclass
class TurnFeatures:
    champions: list[str]
    team: str
    kind: str
    values: np.ndarray

    def column(self, name: str) -> np.ndarray:
        return self.values[:, FEATURES.index(name)]


class History:
    """Accumulate draft evidence in date order. Current-day games enter tomorrow."""

    def __init__(self, catalog: ChampionCatalog):
        self.catalog = catalog
        self.size = len(catalog.names)
        self.as_of = None
        self.pending = deque()
        self.counts = {}
        self.recent_counts = {}
        self.rosters = {}
        self.roster_dates = {}
        self.players = {}
        self.recent_players = {}
        self.role_counts = np.zeros((self.size, len(ROLES)), dtype=np.int32)
        self.pairs = np.zeros((2, 4, self.size, self.size), dtype=np.int32)
        self.pair_support = np.zeros((2, 4, self.size), dtype=np.int32)
        self.empty_counts = np.zeros((2, self.size), dtype=np.int32)
        self.empty_player = np.zeros(self.size, dtype=np.int32)
        self.end = None

    def before(self, day: str, games: Iterable[dict] = ()) -> None:
        if self.end is not None and day < self.end:
            raise ValueError('Historical evidence must advance in date order.')
        self.end = day
        cutoff = (date.fromisoformat(day) - timedelta(days=RECENT_DAYS)).isoformat()
        for game in games:
            completed = evidence_date(game)
            if completed >= day:
                raise ValueError('A historical game must precede the evidence date.')
            if game.get('eligible'):
                self._observe(game, recent=False, change=1)
                if completed >= cutoff:
                    self._observe(game, recent=True, change=1)
                    self.pending.append((completed, game))
                self.as_of = completed
        while self.pending and self.pending[0][0] < cutoff:
            _, expired = self.pending.popleft()
            self._observe(expired, recent=True, change=-1)

    def player_ids(self, team: str, supplied: Mapping[str, Sequence[str]] | None = None) -> tuple[str, ...]:
        if supplied is not None and team in supplied:
            return tuple(dict.fromkeys(supplied[team]))
        return self.rosters.get(team, ())

    def champion_evidence(self, champion: str, kind: str) -> tuple[int | None, list[str]]:
        index = self.catalog.indices[champion]
        # An eligible draft can use a champion only once, so these action counts are distinct games.
        played = int(self.counts.get(('all', kind), self.empty_counts)[0, index]) if self.as_of else None
        counts = self.role_counts[index]
        roles = [ROLES[position] for position in np.argsort(-counts, kind='stable') if counts[position] > 0]
        return played, roles

    def roster_evidence(self, team: str, supplied: Mapping[str, Sequence[str]] | None = None) -> dict:
        players = self.player_ids(team, supplied)
        if supplied is not None and team in supplied:
            source, day = 'supplied', None
        elif players:
            source, day = 'previous_observed_lineup', self.roster_dates[team]
        else:
            source, day = 'unavailable', None
        return {'source': source, 'as_of': day, 'players': len(players)}

    def _scope_keys(self, context: DraftContext, position: int, team: str, kind: str) -> tuple:
        opponent = context.second if team == context.first else context.first
        patch = _patch_key(context.patch) or context.patch
        return (
            ('all', kind), ('region', context.region, kind), ('patch', patch, kind),
            ('region_patch', context.region, patch, kind), ('slot', position, kind),
            ('game', min(context.game_number, 5), kind), ('format', context.fearless, kind),
            ('team', team, kind), ('target', opponent, kind),
        )

    def _count(self, table, key, legal, chosen, change):
        if key not in table:
            table[key] = np.zeros((2, self.size), dtype=np.int32)
        values = table[key]
        values[1, legal] += change
        if chosen is not None:
            values[0, chosen] += change

    def _observe(self, game: dict, recent: bool, change: int):
        context = DraftContext.from_game(game)
        table = self.recent_counts if recent else self.counts
        enabled = self.catalog.available(context.date, context.patch)
        active = np.zeros(self.size, dtype=np.bool_)
        active[[self.catalog.indices[name] for name in enabled]] = True
        for champion in game.get('disabled', ()):
            if champion in self.catalog.indices:
                active[self.catalog.indices[champion]] = False
        for champion in game.get('locked', ()) if context.fearless else ():
            if champion in self.catalog.indices:
                active[self.catalog.indices[champion]] = False
        prefix = []
        for position, action in enumerate(game['actions']):
            team, kind = action['team'], action['kind']
            choice = self.catalog.indices.get(action['champion'])
            if choice is not None and not active[choice]:
                choice = None
            legal = np.flatnonzero(active)
            # A label outside the dated pool remains uncovered, including for its own exposure counts.
            if choice is not None:
                keys = (('all', kind), ('team', team, kind)) if recent else self._scope_keys(context, position, team, kind)
                for key in keys:
                    self._count(table, key, legal, choice, change)
                if not recent:
                    action_index = ACTION_KINDS.index(kind)
                    for prefix_team, prefix_kind, idx in prefix:
                        relation = 2 * ACTION_KINDS.index(prefix_kind) + (prefix_team != team)
                        self.pairs[action_index, relation, idx, choice] += 1
                        self.pair_support[action_index, relation, idx] += 1
            index = self.catalog.indices.get(action['champion'])
            if index is not None:
                active[index] = False
                prefix.append((team, kind, index))

        for team, roster in game.get('rosters', {}).items():
            picks = {action['champion'] for action in game['actions'] if action['team'] == team and action['kind'] == 'pick'}
            player_ids = []
            for entry in roster:
                player = entry.get('player')
                champion = entry.get('champion')
                if not player or champion not in picks or champion not in self.catalog.indices:
                    continue
                player_ids.append(player)
                index = self.catalog.indices[champion]
                players = self.recent_players if recent else self.players
                if player not in players:
                    players[player] = np.zeros(self.size, dtype=np.int32)
                players[player][index] += change
                role = entry.get('role')
                if not recent and role in ROLES and champion in enabled:
                    self.role_counts[index, ROLES.index(role)] += 1
            if player_ids and not recent:
                self.rosters[team] = tuple(dict.fromkeys(player_ids))
                self.roster_dates[team] = game['date']

    def _rate(self, table, key, ids, prior, strength):
        count, support = self._counts(table, key, ids)
        return (count + strength * prior) / (support + strength)

    def _counts(self, table, key, ids):
        return table.get(key, self.empty_counts)[:, ids]

    def _player_features(self, players, ids, prior):
        if not players:
            zeros = np.zeros(len(ids))
            return prior, zeros, zeros
        all_counts = np.stack([self.players.get(player, self.empty_player) for player in players])
        recent_counts = np.stack([self.recent_players.get(player, self.empty_player) for player in players])
        rates = (all_counts[:, ids] + 12 * prior) / (all_counts.sum(axis=1, keepdims=True) + 12)
        return rates.max(axis=0), all_counts[:, ids].sum(axis=0), recent_counts[:, ids].sum(axis=0)

    def _role_features(self, team, current, ids):
        all_picks = [self.catalog.indices[action['champion']] for action in current
                     if action['team'] == team and action['kind'] == 'pick' and action['champion'] in self.catalog.indices]
        role_counts = self.role_counts[ids]
        count = role_counts.sum(axis=1)
        roles = role_counts / np.maximum(1, count[:, None])
        occupied = np.zeros(len(ROLES))
        for index in all_picks:
            evidence = self.role_counts[index]
            occupied = np.maximum(occupied, evidence / max(1, evidence.sum()))
        fit = roles @ (1 - occupied)
        flex = np.count_nonzero((role_counts >= 2) & (roles >= 0.12), axis=1)
        return fit, flex, count

    def _pair_features(self, kind, team, current, ids, base):
        rates, counts = [], []
        kind_index = ACTION_KINDS.index(kind)
        for relation in range(4):
            previous_kind, enemy = ACTION_KINDS[relation // 2], bool(relation % 2)
            indices = [self.catalog.indices[action['champion']] for action in current
                       if action['kind'] == previous_kind and (action['team'] != team) == enemy
                       and action['champion'] in self.catalog.indices]
            if indices:
                observed = self.pairs[kind_index, relation][np.ix_(indices, ids)]
                support = self.pair_support[kind_index, relation, indices, None]
                rates.append(((observed + 15 * base) / (support + 15)).max(axis=0))
                counts.append(observed.sum(axis=0))
            else:
                rates.append(base)
                counts.append(np.zeros(len(ids)))
        return (*rates, *counts)

    def features(self, context: DraftContext, current: Sequence[DraftAction], locked: Iterable[str],
                 pool: Iterable[str] | None = None, rosters=None) -> TurnFeatures:
        position = len(current)
        if position >= DRAFT_LENGTH:
            raise ValueError('This draft is complete.')
        team, kind = schedule(context.first, context.second)[position]
        opponent = context.second if team == context.first else context.first
        locked = set(locked) if context.fearless else set()
        enabled = self.catalog.available(context.date, context.patch)
        if pool is not None:
            permitted = set(pool)
            enabled = [champion for champion in enabled if champion in permitted]
        champions = legal_champions(enabled, locked, current)
        if not champions:
            return TurnFeatures(champions, team, kind, np.empty((0, len(FEATURES)), dtype=np.float32))
        ids = np.fromiter((self.catalog.indices[name] for name in champions), dtype=np.intp)
        scopes = self._scope_keys(context, position, team, kind)
        base = self._rate(self.counts, scopes[0], ids, 1 / max(100, len(champions)), 110)
        recent = self._rate(self.recent_counts, scopes[0], ids, base, 60)
        global_count, global_support = self._counts(self.counts, scopes[0], ids)
        recent_count, recent_support = self._counts(self.recent_counts, scopes[0], ids)
        older = (global_count - recent_count + 110 / max(100, len(champions))) / (global_support - recent_support + 110)
        region = self._rate(self.counts, scopes[1], ids, recent, 50)
        patch = self._rate(self.counts, scopes[2], ids, recent, 65)
        region_patch = self._rate(self.counts, scopes[3], ids, (region + patch) / 2, 40)
        slot_rate = self._rate(self.counts, scopes[4], ids, base, 55)
        game_rate = self._rate(self.counts, scopes[5], ids, base, 60)
        format_rate = self._rate(self.counts, scopes[6], ids, base, 75)
        global_pick = self._rate(self.counts, ('all', 'pick'), ids, 0.01, 110)
        global_ban = self._rate(self.counts, ('all', 'ban'), ids, 0.01, 110)
        team_rate = self._rate(self.counts, scopes[7], ids, region_patch, 36)
        team_pick = self._rate(self.counts, ('team', team, 'pick'), ids, global_pick, 36)
        opponent_pick = self._rate(self.counts, ('team', opponent, 'pick'), ids, global_pick, 36)
        targeted_ban = self._rate(self.counts, ('target', opponent, 'ban'), ids, global_ban, 44)
        team_recent_pick = self._rate(self.recent_counts, ('team', team, 'pick'), ids, team_pick, 28)
        opponent_recent_pick = self._rate(self.recent_counts, ('team', opponent, 'pick'), ids, opponent_pick, 28)
        team_pick_count, _ = self._counts(self.counts, ('team', team, 'pick'), ids)
        opponent_pick_count, _ = self._counts(self.counts, ('team', opponent, 'pick'), ids)
        team_players = self._player_features(self.player_ids(team, rosters), ids, global_pick)
        opponent_players = self._player_features(self.player_ids(opponent, rosters), ids, global_pick)
        role_fit, role_flex, role_count = self._role_features(team, current, ids)
        pairs = self._pair_features(kind, team, current, ids, base)
        baseline = np.log(0.56 * region_patch + 0.27 * slot_rate + 0.17 * game_rate)
        fixed = np.tile((position + 1, int(team == context.first), context.game_number,
                         int(context.fearless), len(locked), len(champions)), (len(ids), 1))
        values = np.column_stack((baseline, base, recent, older, region, patch, region_patch, slot_rate,
                                  game_rate, format_rate, global_support, recent_support,
                                  self._counts(self.counts, scopes[1], ids)[1],
                                  self._counts(self.counts, scopes[2], ids)[1], global_pick, global_ban,
                                  team_rate, team_pick, opponent_pick, targeted_ban, team_recent_pick,
                                  opponent_recent_pick, team_pick_count, opponent_pick_count,
                                  team_players[0], opponent_players[0], team_players[1], opponent_players[1],
                                  team_players[2], opponent_players[2], role_fit, role_flex, role_count,
                                  *pairs, fixed)).astype(np.float32)
        return TurnFeatures(champions, team, kind, values)


def prepare_games(games: Iterable[dict]) -> list[dict]:
    """Order completed records and derive every series lock with earlier drafts only."""
    by_series = defaultdict(list)
    for game in games:
        by_series[game['series_id']].append(game)
    prepared = []
    for series in by_series.values():
        prior = []
        for game in sorted(series, key=lambda row: row['game_number']):
            if 'locked' not in game:
                game = {**game, 'locked': sorted(series_locks(prior, fearless=game.get('fearless', True)))}
            if game.get('eligible'):
                prior.append(game['actions'])
            prepared.append(game)
    return sorted(prepared, key=lambda game: (game['date'], _utc_time(game.get('drafted_at'), game['date']),
                                             str(game['series_id']), game['game_number']))


def series_splits(games: Iterable[dict]) -> dict[str, str]:
    periods = defaultdict(set)
    for game in games:
        if game.get('eligible'):
            periods[game['series_id']].add(split_for_date(game['date']))
    return {series: next(iter(names)) if len(names) == 1 else 'cross_boundary' for series, names in periods.items()}
