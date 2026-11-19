"""Infer the roles of a double-pick window, then rank legal unordered champion pairs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date as calendar_date, timedelta
from functools import lru_cache
from itertools import combinations
import json
from math import isfinite, log
from pathlib import Path
from threading import Lock

import numpy as np

from fearless.ban_challenger import ROLE_INDEX, _assignments
from fearless.challengers import incumbent_signature
from fearless.event_forecaster import digest, verify_new_games
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model import DraftForecaster
from fearless.model_features import ROLES, _patch_key, evidence_date
from fearless.pair_model import _ally_picks, _position, _write, pair_turns
from fearless.rules import DRAFT_LENGTH, legal_champions


WINDOWS = (7, 9, 17)
ROLE_PAIRS = tuple(combinations(range(len(ROLES)), 2))
ROLE_PAIR_INDEX = {pair: index for index, pair in enumerate(ROLE_PAIRS)}
MASK_COUNT = 1 << len(ROLES)
RECENT_DAYS = 56
FIT_START = "2026-01-01"
FIT_THROUGH = "2026-04-30"
FIT_GRID = ((12, 20, 1.0), (5, 20, 1.0), (25, 20, 1.0), (12, 6, 1.0), (12, 55, 1.0), (12, 20, 1.6))


def _patch(value):
    key = _patch_key(value)
    return f"{key[0]}.{key[1]}" if key else value


def _archive(games):
    by_series = defaultdict(list)
    for game in games:
        by_series[str(game["series_id"])].append(game)
    return tuple((max(map(evidence_date, games)), tuple(sorted(games, key=lambda game: game["game_number"])))
                 for games in by_series.values())


def _valid_roster(roster, played):
    entries = [(entry["champion"], ROLE_INDEX[entry["role"]], str(entry.get("player") or ""))
               for entry in roster if entry.get("champion") in played and entry.get("role") in ROLE_INDEX]
    roles = Counter(role for _, role, _ in entries)
    champions = Counter(champion for champion, _, _ in entries)
    players = Counter(player for _, _, player in entries if player)
    return {champion: (role, player) for champion, role, player in entries
            if roles[role] == champions[champion] == 1 and (not player or players[player] == 1)}


def _role_mask(roles):
    return sum(1 << role for role in set(roles))


class _Ledger:
    def __init__(self, groups, catalog):
        self.catalog = catalog
        self.lineups = defaultdict(list)
        games, picks, pairs = [], [], []
        pick_teams, players, pair_teams = [], [], []
        for day, series in sorted(groups, key=lambda entry: entry[0]):
            for raw in series:
                if not raw.get("eligible") or len(raw["actions"]) != DRAFT_LENGTH:
                    continue
                game = catalog.normalize_game(raw)
                actions = game["actions"]
                sid = str(game["series_id"])
                index = len(games)
                games.append((day, sid, _patch(game["patch"]), game.get("event") or ""))
                lineups = {}
                for team in (game["first"], game["second"]):
                    played = set(_ally_picks(actions, team)) & catalog.indices.keys()
                    roster = _valid_roster(game["rosters"].get(team, ()), played)
                    lineups[team] = roster
                    players_seen = frozenset(player for role, player in roster.values() if player)
                    if players_seen:
                        self.lineups[team].append((day, sid, players_seen))
                    for champion, (role, player) in roster.items():
                        picks.append((index, role * len(catalog.names) + catalog.indices[champion]))
                        pick_teams.append(team)
                        players.append(player)
                for window, team, (left, right) in pair_turns(actions):
                    if window not in WINDOWS or team != _position(game["first"], game["second"], window):
                        continue
                    roster = lineups[team]
                    if left not in roster or right not in roster:
                        continue
                    roles = tuple(sorted((roster[left][0], roster[right][0])))
                    ally = _ally_picks(actions[:window], team)
                    used = _role_mask(roster[champion][0] for champion in ally) if all(c in roster for c in ally) else -1
                    pairs.append((index, window, used, ROLE_PAIR_INDEX[roles], catalog.indices[left], catalog.indices[right]))
                    pair_teams.append(team)
        self.days, self.series, self.patches, self.events = np.asarray(games, dtype=str).reshape(-1, 4).T
        self.pick_game, self.pick_bin = np.asarray(picks, dtype=np.intp).reshape(-1, 2).T
        self.pick_teams, self.players = np.asarray(pick_teams, dtype=str), np.asarray(players, dtype=str)
        self.pair_game, self.window, self.used, self.role_pair, self.a, self.b = np.asarray(pairs, dtype=np.intp).reshape(-1, 6).T
        self.pair_teams = np.asarray(pair_teams, dtype=str)
        self._cached_counts = lru_cache(maxsize=64)(self._dated_counts)

    def lineup(self, team, day, series, rosters):
        if rosters is not None and team in rosters:
            return tuple(sorted(set(map(str, rosters[team])) - {""}))
        for seen, sid, players in reversed(self.lineups.get(team, ())):
            if seen < day and sid != series:
                return tuple(sorted(players))
        return ()

    def _dated_counts(self, date, patch, team, event, own_series, players):
        cutoff = (calendar_date.fromisoformat(date) - timedelta(days=RECENT_DAYS)).isoformat()
        earlier = (self.days < date) & (self.series != own_series)
        same_patch, event_match = self.patches == patch, (self.events == event) if event else np.zeros(len(earlier), bool)
        recent = self.days >= cutoff
        pick = earlier[self.pick_game]
        size = len(self.catalog.names)

        def hist(selected):
            return np.bincount(self.pick_bin[pick & selected], minlength=len(ROLES) * size).reshape(len(ROLES), size)

        all_counts = hist(True)
        recent_counts = hist(recent[self.pick_game])
        patch_counts = hist(same_patch[self.pick_game])
        event_counts = hist(event_match[self.pick_game])
        team_counts = hist(self.pick_teams == team)
        player_counts = hist(np.isin(self.players, players))
        last = self.days[earlier][-1] if np.any(earlier) else None
        return earlier, same_patch, event_match, recent, last, all_counts, recent_counts, patch_counts, event_counts, team_counts, player_counts

    def evidence(self, *, catalog, date, patch, team, current, locks, pool, rosters, event, series_id, include_joint=True):
        own_series = str(series_id or "")
        if own_series and not np.any((self.series == own_series) & (self.days < date)):
            own_series = ""
        players = self.lineup(team, date, own_series, rosters)
        dated = self._cached_counts(date, _patch(patch), team, event, own_series, players)
        earlier, same_patch, event_match, recent, last, all_counts, recent_counts, patch_counts, event_counts, team_counts, player_counts = dated
        size = len(catalog.names)

        prefix = tuple(catalog.normalize_action(action) for action in current)
        ally = tuple(catalog.indices.get(champion, size) for champion in _ally_picks(prefix, team))
        display = {}
        for champion in sorted(pool):
            name = catalog.canonical(champion)
            if name in catalog.indices and (name not in display or name == champion):
                display[name] = champion
        legal = sorted(legal_champions(display, map(catalog.canonical, locks), prefix))
        legal_ids = np.array([catalog.indices[name] for name in legal], dtype=np.intp)

        appears = earlier[self.pair_game]
        patch_pairs, event_pairs = same_patch[self.pair_game], event_match[self.pair_game]
        same_window = self.window == len(current)
        team_pairs = self.pair_teams == team
        role_weight = 0.16 + 0.7 * patch_pairs + 0.8 * event_pairs + 0.2 * team_pairs
        role_counts = np.bincount(self.role_pair[appears & same_window], role_weight[appears & same_window], minlength=len(ROLE_PAIRS))
        scoped = appears & same_window & (self.used >= 0)
        conditioned = np.bincount(self.used[scoped] * len(ROLE_PAIRS) + self.role_pair[scoped], role_weight[scoped],
                                  minlength=MASK_COUNT * len(ROLE_PAIRS)).reshape(MASK_COUNT, len(ROLE_PAIRS))

        direct, exact_patch = None, Counter()
        if include_joint and len(legal_ids) >= 2:
            legal_column = np.full(size, -1, dtype=np.intp)
            legal_column[legal_ids] = np.arange(len(legal_ids))
            left, right = legal_column[self.a], legal_column[self.b]
            select = appears & (left >= 0) & (right >= 0)
            # Other windows inform composition at lower weight than the exact pick window.
            joint_weight = ((0.018 + 0.10 * recent[self.pair_game] + 0.55 * patch_pairs
                             + 0.42 * event_pairs + 0.25 * team_pairs) * (0.5 + 0.5 * same_window))
            width = len(legal)
            direct = np.bincount((self.role_pair[select] * width + left[select]) * width + right[select], joint_weight[select],
                                 minlength=len(ROLE_PAIRS) * width * width).reshape(len(ROLE_PAIRS), width, width)
            exact_patch = Counter(zip(left[select & patch_pairs], right[select & patch_pairs]))
        return _Evidence([display[name] for name in legal], legal_ids, ally, last, all_counts, recent_counts, patch_counts,
                         event_counts, team_counts, player_counts, role_counts, conditioned, direct, exact_patch)


@dataclass(frozen=True)
class _Evidence:
    display: list
    legal: np.ndarray
    ally: tuple
    last: str | None
    all: np.ndarray
    recent: np.ndarray
    patch: np.ndarray
    event: np.ndarray
    team: np.ndarray
    player: np.ndarray
    roles: np.ndarray
    conditioned: np.ndarray
    direct: np.ndarray | None
    exact_patch: Counter


def _normalize(values, axis=-1):
    return values / values.sum(axis=axis, keepdims=True)


@lru_cache(maxsize=len(ROLES))
def _role_options(ally_count):
    assignments, open_roles = _assignments(ally_count)
    masks = np.sum(1 << assignments, axis=1)
    pairs = np.asarray(ROLE_PAIRS)
    available = open_roles[:, pairs[:, 0]] * open_roles[:, pairs[:, 1]]
    return assignments, masks, available


def _role_probabilities(evidence, flex_power):
    # The prior remains soft for champions with multiple observed jobs or sparse evidence.
    observed = (evidence.all * 0.24 + evidence.recent * 0.45 + evidence.patch * 0.8
                + evidence.event * 0.5 + evidence.team * 0.45 + evidence.player * 0.4)
    affinity = np.column_stack((_normalize(observed + 0.08, axis=0), np.full(len(ROLES), 1 / len(ROLES))))
    if not evidence.ally:
        return _normalize(evidence.roles + 0.10)
    assignments, masks, available = _role_options(len(evidence.ally))
    likelihood = affinity[:, np.asarray(evidence.ally)].T
    weights = _normalize(likelihood[np.arange(len(evidence.ally)), assignments].prod(axis=1) ** flex_power)
    broad = _normalize(available * (evidence.roles + 0.10), axis=1)
    conditional = _normalize(available * evidence.conditioned[masks] + 3 * broad, axis=1)
    return weights @ conditional


def _champions(evidence, patch_strength):
    population = _normalize(evidence.all + 0.09)
    recent = _normalize(evidence.recent + 28 * population)
    patch = _normalize(evidence.patch + 0.65 * evidence.event + patch_strength * recent)
    teams = _normalize(evidence.team + 26 * patch)
    players = _normalize(evidence.player + 24 * teams)
    return _normalize(players[:, evidence.legal])


def _distribution(evidence, patch_strength, pair_strength, flex_power):
    roles = _role_probabilities(evidence, flex_power)
    champions = _champions(evidence, patch_strength)
    weights = np.zeros((len(ROLES), len(ROLES)))
    support = evidence.direct.sum(axis=(1, 2))
    for index, (left, right) in enumerate(ROLE_PAIRS):
        # Normalize each role pair on unequal legal champions before marginalizing.
        unequal = np.sum(champions[left] * (1 - champions[right]))
        base = roles[index] * pair_strength / (pair_strength + support[index]) / unequal
        weights[left, right] = weights[right, left] = base
    distribution = champions.T @ weights @ champions
    distribution += np.einsum("r,rij->ij", roles / (pair_strength + support), evidence.direct)
    np.fill_diagonal(distribution, 0)
    return distribution, champions, weights


@lru_cache(maxsize=128)
def _upper(size):
    return np.triu_indices(size, k=1)


class RoleFirstPairModel:
    def __init__(self, artifact, *, released=()):
        self._artifact = deepcopy(artifact)
        self.fit_through = calendar_date.fromisoformat(artifact["fit_through"]).isoformat()
        self.parameters = tuple(artifact[key] for key in ("patch_strength", "pair_strength", "flex_power"))
        if artifact.get("version") != 1 or not all(type(v) in (int, float) and isfinite(v) and v > 0 for v in self.parameters):
            raise ValueError("Invalid role-first pair artifact.")
        self._released = released
        self._lock = Lock()
        self._cached = (None, None)

    @classmethod
    def load(cls, path, bundle):
        artifact = json.loads(Path(path).read_text(encoding="utf8"))
        if artifact.get("incumbent_sha256") != incumbent_signature(bundle):
            raise ValueError("Role-first pairs were fitted against a different incumbent bundle.")
        return cls(artifact)

    def with_completed_series(self, rows):
        existing = {str(series[0]["series_id"]): series for series in self._released}
        for series in completed_series(rows):
            key = str(series[0]["series_id"])
            if key in existing and existing[key] != series:
                raise ValueError("Completed series cannot be replaced with different evidence.")
            existing[key] = deepcopy(series)
        return type(self)(self._artifact, released=tuple(existing.values()))

    def _ledger(self, forecaster):
        with self._lock:
            if self._cached[0] is not forecaster:
                verify_new_games(forecaster, self._released)
                events = tuple((completion_day(series), series) for series in self._released)
                self._cached = (forecaster, _Ledger((*_archive(forecaster.games), *events), forecaster.catalog))
            return self._cached[1]

    def _evidence(self, *, forecaster, date, patch, first, second, current, locked=(), pool=None,
                  rosters=None, event=None, series_id=None, fearless=True, include_joint=True, **_):
        if len(current) not in WINDOWS or date <= self.fit_through:
            return None
        catalog = forecaster.catalog
        pool = catalog.available(date, patch) if pool is None else pool
        team = _position(first, second, len(current))
        return self._ledger(forecaster).evidence(catalog=catalog, date=date, patch=patch, team=team, current=current,
                                                 locks=locked if fearless else (), pool=pool, rosters=rosters,
                                                 event=event, series_id=series_id, include_joint=include_joint)

    def rank_roles(self, *, limit=3, **context):
        evidence = self._evidence(**context, include_joint=False) if limit > 0 else None
        if evidence is None:
            return []
        probabilities = _role_probabilities(evidence, self.parameters[2])
        order = np.argsort(-probabilities, kind="stable")[:limit]
        return [{"roles": [ROLES[role] for role in ROLE_PAIRS[index]], "probability": float(probabilities[index])}
                for index in order]

    def rank_pairs(self, *, limit=3, **context):
        evidence = self._evidence(**context) if limit > 0 else None
        if evidence is None or len(evidence.legal) < 2:
            return []
        scores, champions, weights = _distribution(evidence, *self.parameters)
        left, right = _upper(len(evidence.legal))
        result = []
        for index in np.argsort(-scores[left, right], kind="stable")[:limit]:
            a, b = left[index], right[index]
            names = evidence.display[a], evidence.display[b]
            placements = champions[:, a, None] * weights * champions[None, :, b]
            plus = np.argsort(-placements.ravel(), kind="stable")[:2]
            options = [divmod(choice, len(ROLES)) for choice in plus]
            roles = [f"{names[0]} {ROLES[l]} with {names[1]} {ROLES[r]}" for l, r in options]
            support = f"Role estimates favor {roles[0]}."
            if placements.ravel()[plus[1]] >= 0.4 * placements.ravel()[plus[0]]:
                support = f"Plausible assignments include {roles[0]} or {roles[1]}."
            lines = [support]
            if count := evidence.exact_patch[a, b]:
                lines.append(f"Earlier double picks on this patch included these champions together {count} times.")
            if evidence.last:
                lines.append(f"Draft and role evidence available through {evidence.last} UTC.")
            result.append({"champions": sorted(names), "evidence": lines})
        return result


def _episodes(games, ledger, catalog, pools=None):
    for raw in games:
        game = catalog.normalize_game(raw)
        day, patch = game["date"], game["patch"]
        available = (pools[patch] if pools is not None else catalog.available(day, patch))
        disabled = {catalog.canonical(name) for name in game.get("disabled", ())}
        pool = tuple(name for name in available if catalog.canonical(name) not in disabled)
        actions = game["actions"]
        if len(actions) != DRAFT_LENGTH:
            continue
        for window, team, pair in pair_turns(actions):
            if window not in WINDOWS or team != _position(game["first"], game["second"], window):
                continue
            evidence = ledger.evidence(catalog=catalog, date=day, patch=patch, team=team, current=actions[:window],
                                       locks=game["locked"] if game["fearless"] else (), pool=pool, rosters=None,
                                       event=game.get("event"), series_id=game["series_id"])
            indices = {catalog.names[index]: i for i, index in enumerate(evidence.legal)}
            target = tuple(indices[name] for name in pair) if set(pair) <= indices.keys() else None
            roster = _valid_roster(game["rosters"].get(team, ()), set(_ally_picks(actions, team)))
            roles = tuple(sorted(roster[name][0] for name in pair)) if set(pair) <= roster.keys() else None
            yield game, window, evidence, target, roles


def _rank(scores, target):
    left, right = _upper(len(scores))
    target_index = np.searchsorted(left * len(scores) + right, target[0] * len(scores) + target[1])
    ordered = scores[left, right]
    value = ordered[target_index]
    return int(np.count_nonzero(ordered > value) + np.count_nonzero(ordered[:target_index] == value))


def _fit(bundle, forecaster, start, through):
    archive = _archive(forecaster.games)
    selected = [game for end, series in archive if start <= end <= through and all(g["date"] >= start for g in series)
                for game in series]
    ledger = _Ledger(archive, forecaster.catalog)
    selection = {parameters: [0.0, 0, 0] for parameters in FIT_GRID}
    all_turns = 0
    for _, _, evidence, target, _ in _episodes(selected, ledger, forecaster.catalog):
        all_turns += 1
        if target is None:
            continue
        for parameters, summary in selection.items():
            scores, _, _ = _distribution(evidence, *parameters)
            summary[0] -= log(scores[target])
            summary[1] += _rank(scores, target) < 3
            summary[2] += 1
    if not all_turns or not all(values[2] for values in selection.values()):
        raise ValueError("No legal pre-cutoff GRID windows were available for fitting.")
    selected_params = min(selection, key=lambda params: selection[params][0] / selection[params][2])
    return {"version": 1, "fit_from": start, "fit_through": through, "source": "frozen GRID archive only",
            "incumbent_sha256": incumbent_signature(bundle), "windows": all_turns,
            **dict(zip(("patch_strength", "pair_strength", "flex_power"), selected_params, strict=True)),
            "fitting_grid": [{"parameters": params, "nll": loss / count, "hits_at_3": hits, "legal_targets": count}
                             for params, (loss, hits, count) in selection.items()]}


def _diagnostic(model, forecaster, rows, pools):
    verified = completed_series(rows)
    enriched = model.with_completed_series(rows)
    ledger = enriched._ledger(forecaster)
    summary = defaultdict(Counter)
    for game, window, evidence, target, roles in _episodes((g for series in verified for g in series), ledger,
                                                           forecaster.catalog, pools):
        if game["date"] <= model.fit_through:
            raise ValueError("A diagnostic draft predates the frozen fit cutoff.")
        all_ = summary["all"], summary[game["event"]], summary[f"window_{window}"]
        if target is None:
            raise ValueError("A recorded event pair was outside the legal pool.")
        scores, _, _ = _distribution(evidence, *model.parameters)
        rank = _rank(scores, target)
        if roles is not None:
            label = ROLE_PAIR_INDEX[roles]
            probabilities = _role_probabilities(evidence, model.parameters[2])
            role_order = np.argsort(-probabilities, kind="stable")
        for scope in all_:
            scope["turns"] += 1
            scope["pair_hits_at_1"] += rank == 0
            scope["pair_hits_at_3"] += rank < 3
            scope["pair_nll_total"] -= log(scores[target])
            if roles is not None:
                scope["role_targets"] += 1
                scope["role_hits_at_1"] += int(role_order[0] == label)
                scope["role_hits_at_3"] += label in role_order[:3]
                scope["role_nll_total"] -= log(probabilities[label])
    return {name: {**dict(counts), "pair_hit_at_3": counts["pair_hits_at_3"] / counts["turns"],
                   "pair_nll": counts["pair_nll_total"] / counts["turns"],
                   "role_hit_at_3": counts["role_hits_at_3"] / counts["role_targets"] if counts["role_targets"] else None}
            for name, counts in summary.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit", help="freeze role-first parameters on pre-May 2026 GRID")
    replay = commands.add_parser("replay", help="diagnose a frozen artifact on known events")
    for command in (fit, replay):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    fit.add_argument("--from", dest="start", default=FIT_START)
    fit.add_argument("--through", default=FIT_THROUGH)
    for option in ("model", "games", "catalog"):
        replay.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    forecaster = DraftForecaster.load(args.bundle)
    if args.command == "fit":
        start = calendar_date.fromisoformat(args.start).isoformat()
        through = calendar_date.fromisoformat(args.through).isoformat()
        if not start <= through <= FIT_THROUGH:
            parser.error("Fit windows must end by April 30, 2026.")
        output = _fit(args.bundle, forecaster, start, through)
    else:
        model = RoleFirstPairModel.load(args.model, args.bundle)
        rows = read_event_games(args.games)
        pools = json.loads(args.catalog.read_text(encoding="utf8"))
        output = {"games": len(rows), "games_sha256": digest(args.games), "artifact_sha256": digest(args.model),
                  "fit_through": model.fit_through, "diagnostic": _diagnostic(model, forecaster, rows, pools)}
    _write(args.output, output)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
