"""Retrieve and rank legal unordered double picks from earlier completed drafts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date as calendar_date, timedelta
from itertools import groupby
import json
from math import isfinite, log, log1p
from pathlib import Path
from threading import Lock

import numpy as np

from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model import DraftForecaster, _method_scores, _softmax
from fearless.model_features import DraftContext, History, ROLES, TEST_START, _patch_key, evidence_date
from fearless.pair_model import (
    BEAM_WIDTH, _ally_picks, _beam, _incumbent_identity, _pair_features, _pair_role_options,
    _position, _rank_stats, _ranking, _role_assignments, _role_evidence, _role_mask, _write,
    PairChallenger, PairHistory, pair_turns, whole_series,
)
from fearless.rules import DRAFT_LENGTH, legal_champions


FEATURES = (
    "individual_log", "incumbent_path", "archive_pair", "archive_window", "recent_window",
    "patch_pair", "region_window", "event_pair", "event_window", "team_pair", "roster_pair",
    "roster_champions", "role_conflict",
)
PAIR_WINDOWS = (7, 9, 17)
DIRECT_ARCHIVE_LIMIT = 160
RECENT_DAYS = 56
TRAIN_THROUGH = "2026-07-31"
DEVELOPMENT_SPLIT = "2026-10-28"


@dataclass(frozen=True)
class _Draft:
    day: str
    series: str
    event: str | None
    patch: tuple | str
    region: str
    picks: tuple
    pairs: tuple


def _drafts(series, completed, catalog):
    for raw in series:
        if not raw.get("eligible") or len(raw["actions"]) != DRAFT_LENGTH:
            continue
        game = catalog.normalize_game(raw)
        picks = {(action["team"], action["champion"]) for action in game["actions"] if action["kind"] == "pick"}
        roster = {(team, entry["champion"]): (str(entry["player"]), entry.get("role"))
                  for team, entries in game["rosters"].items() for entry in entries
                  if (team, entry["champion"]) in picks and entry.get("player")}
        pairs = tuple((window, team, pair) for window, team, pair in pair_turns(game["actions"])
                      if window in PAIR_WINDOWS and team == _position(game["first"], game["second"], window))
        yield _Draft(completed, str(game["series_id"]), game.get("event"),
                     _patch_key(game["patch"]) or game["patch"], game["region"], tuple(roster.items()), pairs)


def _archive(games, catalog):
    grouped = defaultdict(list)
    for game in games:
        grouped[str(game["series_id"])].append(game)
    records = []
    for games in grouped.values():
        end = max(map(evidence_date, games))
        records.extend(_drafts(sorted(games, key=lambda game: game["game_number"]), end, catalog))
    return tuple(sorted(records, key=lambda draft: draft.day))


class _ArchiveCache:
    def __init__(self):
        self.lock = Lock()
        self.forecaster = None
        self.drafts = ()

    def get(self, forecaster):
        with self.lock:
            if self.forecaster is not forecaster:
                self.drafts = _archive(forecaster.games, forecaster.catalog)
                self.forecaster = forecaster
            return self.drafts


class _Evidence:
    def __init__(self, observed, catalog, *, day, patch, region, window, team, event, series_id, rosters,
                 archive_replay):
        self.catalog = catalog
        self.role_counts = np.zeros((len(catalog.names), len(ROLES)), dtype=np.int32)
        self.counts = defaultdict(Counter)
        self.latest = defaultdict(dict)
        self.player_champions = Counter()
        self.lineup_supplied = rosters is not None and team in rosters
        self.lineup = set(map(str, rosters[team])) if self.lineup_supplied else set()
        self.lineup_day = ""
        cutoff = (calendar_date.fromisoformat(day) - timedelta(days=RECENT_DAYS)).isoformat()
        patch = _patch_key(patch) or patch
        current_series = None if series_id is None else str(series_id)
        relevant = [draft for draft in observed if draft.day < day and draft.series != current_series]

        if not self.lineup_supplied:
            for draft in reversed(relevant):
                players = {player for (side, _), (player, _) in draft.picks if side == team}
                if players:
                    self.lineup, self.lineup_day = players, draft.day
                    break

        for draft in relevant:
            recent = draft.day >= cutoff
            same_patch = draft.patch == patch
            event_match = (bool(event and draft.event == event)
                           or archive_replay and same_patch and draft.region == region)
            paired_players = {}
            for (side, champion), (player, role) in draft.picks:
                if player in self.lineup:
                    paired_players[side, champion] = player
                    self.player_champions[champion] += 3 if recent else 1
                index = catalog.indices.get(champion)
                if index is not None and role in ROLES:
                    self.role_counts[index, ROLES.index(role)] += 3 if event_match else 1

            for position, side, pair in draft.pairs:
                if not draft.event:
                    self.counts["archive_pair"][pair] += 1
                    if position == window:
                        self.counts["archive_window"][pair] += 1
                if position == window:
                    if recent:
                        self.counts["recent_window"][pair] += 1
                    if draft.region == region and recent:
                        self.counts["region_window"][pair] += 1
                if same_patch:
                    self.counts["patch_pair"][pair] += 1
                    self.latest["patch_pair"][pair] = draft.day
                if event_match:
                    self.counts["event_pair"][pair] += 1
                    self.latest["event_pair"][pair] = draft.day
                    if position == window:
                        self.counts["event_window"][pair] += 1
                if side == team:
                    self.counts["team_pair"][pair] += 1
                    self.latest["team_pair"][pair] = draft.day
                player_matches = sum((side, champion) in paired_players for champion in pair)
                if player_matches:
                    self.counts["roster_pair"][pair] += player_matches
                    self.latest["roster_pair"][pair] = draft.day

    def direct_pairs(self, legal):
        counts = self.counts
        candidates = set(counts["archive_pair"]) | set(counts["recent_window"]) | set(counts["patch_pair"])
        candidates = {pair for pair in candidates if pair[0] in legal and pair[1] in legal}

        def priority(pair):
            return -(counts["archive_window"][pair] + 2 * counts["recent_window"][pair]
                     + 3 * counts["patch_pair"][pair] + 3 * counts["team_pair"][pair]
                     + 3 * counts["roster_pair"][pair] + 0.2 * counts["archive_pair"][pair]), pair

        archive = sorted(candidates, key=priority)[:DIRECT_ARCHIVE_LIMIT]
        released = (pair for pair in counts["event_pair"] if pair[0] in legal and pair[1] in legal)
        return set(archive).union(released)

    def lines(self, pair):
        lines = []
        for name, scope in (("event_pair", "earlier event drafts"), ("patch_pair", "earlier drafts on this patch"),
                            ("team_pair", "earlier drafts by this team")):
            if count := self.counts[name][pair]:
                lines.append(f"Pair picks in {scope}: {count}, last observed {self.latest[name][pair]}.")
        if count := self.counts["roster_pair"][pair]:
            lineup = "The supplied lineup" if self.lineup_supplied else "The last observed lineup"
            noun = "observation" if count == 1 else "observations"
            lines.append(f"{lineup} has {count} player–champion {noun} with this pair, "
                         f"last observed {self.latest['roster_pair'][pair]}.")
        if not lines and (count := self.counts["archive_pair"][pair]):
            noun = "draft" if count == 1 else "drafts"
            lines.append(f"Pair observed in {count} earlier archive {noun}.")
        return lines


@dataclass
class _Candidates:
    wide: dict
    shipped: dict
    features: dict
    display: dict
    evidence: _Evidence
    occupied: set


def _candidates(forecast, catalog, observed, *, day, patch, region, team, current, locked,
                pool, rosters, event, series_id, width, archive_replay=False):
    display = {catalog.canonical(name): name for name in pool}
    prefix = tuple(catalog.normalize_action(action) for action in current)
    legal = set(legal_champions(display, map(catalog.canonical, locked), prefix))
    initial = forecast(prefix, len(legal)) if len(legal) >= 2 else {"choices": []}
    unary = {catalog.canonical(row["champion"]): row["probability"] for row in initial["choices"]}

    def beam_forecast(actions, limit):
        return initial if len(actions) == len(prefix) else forecast(actions, limit)

    wide, shipped = _beam(beam_forecast, prefix, team, width, legal)
    evidence = _Evidence(observed, catalog, day=day, patch=patch, region=region, window=len(prefix),
                         team=team, event=event, series_id=series_id, rosters=rosters, archive_replay=archive_replay)
    ally = _ally_picks(prefix, team)
    roles = {champion: _role_mask(evidence, champion) for champion in legal.union(ally)}
    occupied = _role_assignments([roles[champion] for champion in ally])
    fits = {}
    features = {}
    for pair in sorted(wide.keys() | evidence.direct_pairs(legal)):
        a, b = pair
        independent = max(unary.get(a, 0.0005) * unary.get(b, 0.0005), 1e-12)
        masks = roles[a], roles[b]
        if masks not in fits:
            fits[masks] = not occupied or any(_pair_role_options(occupied, *masks))
        counts = evidence.counts
        features[pair] = (
            log(independent) / 3,
            min(log1p(wide.get(pair, 0.0) / independent), 6),
            log1p(counts["archive_pair"][pair] / 4),
            log1p(counts["archive_window"][pair] / 2),
            log1p(counts["recent_window"][pair] / 2),
            log1p(counts["patch_pair"][pair] / 2),
            log1p(counts["region_window"][pair] / 2),
            log1p(counts["event_pair"][pair] / 2),
            log1p(counts["event_window"][pair]),
            log1p(counts["team_pair"][pair] / 2),
            log1p(counts["roster_pair"][pair] / 2),
            (log1p(evidence.player_champions[a] / 8) + log1p(evidence.player_champions[b] / 8)) / 2,
            -int(not fits[masks]),
        )
    return _Candidates(wide, shipped, features, display, evidence, occupied)


def _rank(candidates, weights):
    return sorted(candidates.features, key=lambda pair: (
        -sum(weight * value for weight, value in zip(weights, candidates.features[pair], strict=True)), pair))


class AdaptivePairModel:
    """Load frozen weights and return a new model when completed series are released."""

    def __init__(self, artifact, *, released=(), archive=None):
        self.train_through = calendar_date.fromisoformat(artifact["train_through"]).isoformat()
        self.weights = tuple(artifact["weights"])
        self.width = artifact["beam_width"]
        if (artifact.get("version") != 1 or artifact.get("features") != list(FEATURES)
                or not 3 <= self.width <= 12 or len(self.weights) != len(FEATURES)
                or not all(map(isfinite, self.weights))):
            raise ValueError("Invalid adaptive pair artifact.")
        self.artifact = deepcopy(artifact)
        self._released = released
        self._archive = archive if archive is not None else _ArchiveCache()

    @classmethod
    def load(cls, path, bundle):
        artifact = json.loads(Path(path).read_text(encoding="utf8"))
        if artifact.get("incumbent_sha256") != _incumbent_identity(bundle):
            raise ValueError("Adaptive pairs were fitted against a different incumbent bundle.")
        return cls(artifact)

    def with_completed_series(self, rows):
        released = {str(series[0]["series_id"]): series for series in self._released}
        for series in completed_series(rows):
            key = str(series[0]["series_id"])
            if key in released and released[key] != series:
                raise ValueError("Completed series cannot be replaced with different evidence.")
            released[key] = deepcopy(series)
        return type(self)(self.artifact, released=tuple(released.values()), archive=self._archive)

    def _observed(self, forecaster):
        archive = self._archive.get(forecaster)
        if not self._released:
            return archive
        released = {str(series[0]["series_id"]) for series in self._released}
        drafts = (draft for series in self._released for draft in _drafts(series, completion_day(series), forecaster.catalog))
        return sorted((*[draft for draft in archive if draft.series not in released], *drafts), key=lambda draft: draft.day)

    def rank_pairs(self, *, forecaster, date, patch, region, first, second, game_number, current,
                   locked=(), pool=None, rosters=None, event=None, series_id=None, limit=3, fearless=True, **_):
        if len(current) not in PAIR_WINDOWS or date <= self.train_through or limit <= 0:
            return []
        team = _position(first, second, len(current))
        catalog = forecaster.catalog
        enabled = tuple(catalog.available(date, patch) if pool is None else pool)
        canonical_pool = tuple(dict.fromkeys(map(catalog.canonical, enabled)))
        locks = tuple(map(catalog.canonical, locked)) if fearless else ()

        def forecast(prefix, top):
            return forecaster.forecast(date=date, patch=patch, region=region, first=first, second=second,
                                       game_number=game_number, current=prefix, locked=locks, pool=canonical_pool,
                                       rosters=rosters, limit=top, fearless=fearless)

        candidates = _candidates(forecast, catalog, self._observed(forecaster), day=date, patch=patch,
                                 region=region, team=team, current=current, locked=locks, pool=enabled,
                                 rosters=rosters, event=event, series_id=series_id, width=self.width)
        output = []
        for pair in _rank(candidates, self.weights)[:limit]:
            champions = tuple(candidates.display[name] for name in pair)
            lines = candidates.evidence.lines(pair)
            if positions := _role_evidence(champions, pair, candidates.evidence, candidates.occupied):
                lines.insert(0, positions)
            if not lines:
                lines.append("Both champions appear in the incumbent's two-pick shortlist.")
            output.append({"champions": sorted(champions), "evidence": lines})
        return output


@dataclass
class _Episode:
    game: dict
    target: tuple
    cold: _Candidates
    warm: _Candidates | None = None
    legacy: list | None = None


def _episodes(rows, forecaster, *, width, archived, released=(), legacy=None, archive_replay=False):
    catalog = forecaster.catalog
    history = History(catalog)
    distribution = iter(forecaster.games)
    pending = next(distribution, None)
    pair_history = PairHistory(forecaster.games) if legacy is not None else None
    full_evidence = tuple(sorted((*archived, *released), key=lambda draft: draft.day))
    for day, games in groupby(rows, key=lambda game: game["date"]):
        past = []
        while pending is not None and evidence_date(pending) < day:
            past.append(pending)
            pending = next(distribution, None)
        history.before(day, past)
        config = forecaster.serving.get("pick") if day >= TEST_START else None
        config = config or {"method": "baseline", "temperature": 1.0}
        for game in games:
            context = DraftContext.from_game(game)
            locked = game["locked"] if context.fearless else ()
            pool = tuple(name for name in catalog.available(day, context.patch)
                         if name not in game.get("disabled", ()))

            def forecast(prefix, top):
                turn = history.features(context, prefix, locked, pool)
                if not turn.champions:
                    return {"choices": []}
                learned = (forecaster.models["pick"].predict(turn.values, thread_count=1)
                           if config["method"] != "baseline" else None)
                scores = _method_scores(config, turn.column("baseline"), learned)
                probabilities = _softmax(scores)
                indices = np.argsort(-scores, kind="stable")[:top]
                return {"choices": [{"champion": turn.champions[index], "probability": float(probabilities[index])}
                                    for index in indices]}

            for window, team, target in pair_turns(game["actions"]):
                if window not in PAIR_WINDOWS or len(game["actions"]) != DRAFT_LENGTH:
                    continue
                current = game["actions"][:window]
                if not set(target) <= set(legal_champions(pool, locked, current)):
                    raise ValueError(f"Recorded pair is unavailable on its dated patch in {game['game_id']}.")
                settings = dict(day=day, patch=context.patch, region=context.region, team=team, current=current,
                                locked=locked, pool=pool, rosters=None, event=game.get("event"),
                                series_id=game["series_id"], width=width)
                cached = {}

                def cached_forecast(prefix, top):
                    # Only the proposed first pick and limit vary within one window.
                    key = (len(prefix), prefix[-1]["champion"], top)
                    if key not in cached:
                        cached[key] = forecast(prefix, top)
                    return cached[key]

                cold = _candidates(cached_forecast, catalog, archived, **settings, archive_replay=archive_replay)
                warm = _candidates(cached_forecast, catalog, full_evidence, **settings) if released else None
                legacy_rank = None
                if legacy is not None:
                    features = _pair_features(cold.wide, pair_history=pair_history, history=history, current=current,
                                              day=day, team=team, rosters=None)
                    legacy_rank = _ranking(cold.wide, features, legacy.artifact["weights"])
                yield _Episode(game, target, cold, warm, legacy_rank)


def _fit(episodes):
    from scipy.optimize import minimize

    sizes, rows, targets = [], [], []
    for episode in episodes:
        features = episode.cold.features
        if episode.target not in features or len(features) < 2:
            continue
        targets.append(len(rows) + tuple(features).index(episode.target))
        rows.extend(features.values())
        sizes.append(len(features))
    if not targets:
        raise ValueError("No represented pair targets for training.")
    features = np.array(rows)
    starts = np.cumsum([0, *sizes[:-1]])
    target_features = features[targets].mean(axis=0)
    regularization = 0.015

    def objective(weights):
        logits = np.sum(features * weights, axis=1)
        centered = logits - np.repeat(np.maximum.reduceat(logits, starts), sizes)
        exponent = np.exp(centered)
        denominator = np.add.reduceat(exponent, starts)
        probability = exponent / np.repeat(denominator, sizes)
        loss = np.mean(np.log(denominator) - centered[targets]) + regularization * (weights @ weights)
        gradient = (probability @ features) / len(targets) - target_features + 2 * regularization * weights
        return loss, gradient

    result = minimize(objective, np.zeros(len(FEATURES)), jac=True, method="L-BFGS-B", bounds=[(0, 8)] * len(FEATURES))
    if not result.success:
        raise ValueError(f"Adaptive pair fit failed: {result.message}")
    return result.x.tolist(), len(targets)


def _report(episodes, weights):
    ranks = defaultdict(list)
    novel = Counter()
    clustered = defaultdict(Counter)
    for episode in episodes:
        cold = episode.cold
        target = episode.target
        rankings = {
            "incumbent": sorted(cold.shipped, key=lambda pair: (-cold.shipped[pair], pair)),
            "wide_incumbent": sorted(cold.wide, key=lambda pair: (-cold.wide[pair], pair)),
            "adaptive_cold": _rank(cold, weights),
        }
        if episode.legacy is not None:
            rankings["wide_challenger"] = episode.legacy
        if episode.warm is not None:
            rankings["adaptive_warm"] = _rank(episode.warm, weights)
        series = clustered[str(episode.game["series_id"])]
        series["turns"] += 1
        for method, ranking in rankings.items():
            rank = ranking.index(target) if target in ranking else None
            ranks[method].append(rank)
            series[method] += rank is not None and rank < 3
            if method.startswith("adaptive_") and target not in cold.wide and rank is not None:
                novel[method] += 1
    intervals = {}
    if len(clustered) > 1:
        methods = tuple(ranks)
        grouped = np.array([[row["turns"], *(row[method] for method in methods)] for row in clustered.values()])
        indices = np.random.default_rng(10429).integers(0, len(grouped), size=(2000, len(grouped)))
        samples = grouped[indices].sum(axis=1)
        for method in (name for name in methods if name.startswith("adaptive_")):
            index = 1 + methods.index(method)
            intervals[method] = {baseline: np.percentile(
                (samples[:, index] - samples[:, 1 + methods.index(baseline)]) / samples[:, 0], [2.5, 97.5]
            ).tolist() for baseline in methods if baseline != method}
    return {"turns": len(episodes), "series": len(clustered),
            "ranking": {name: _rank_stats(results) for name, results in ranks.items()},
            "targets_retrieved_outside_wide_beam": dict(novel), "hit_at_3_delta_ci95": intervals}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fitting = commands.add_parser("fit", help="fit exclusively on dated series in the frozen GRID archive")
    replay = commands.add_parser("replay", help="evaluate cold and chronologically refreshed models on prior events")
    for command in (fitting, replay):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--wide-challenger", type=Path, help="directory of the previous private pair reranker")
    fitting.add_argument("--train-from", default=TEST_START)
    fitting.add_argument("--train-through", default=TRAIN_THROUGH)
    replay.add_argument("--model", type=Path, required=True)
    replay.add_argument("--games", type=Path, required=True, help="previously studied event JSONL")
    replay.add_argument("--split", default=DEVELOPMENT_SPLIT, help="first date of the later diagnostic window")
    args = parser.parse_args()

    forecaster = DraftForecaster.load(args.bundle)
    archived = _archive(forecaster.games, forecaster.catalog)
    legacy = PairChallenger.load(args.wide_challenger, incumbent=args.bundle) if args.wide_challenger else None
    if args.command == "fit":
        start = calendar_date.fromisoformat(args.train_from).isoformat()
        through = calendar_date.fromisoformat(args.train_through).isoformat()
        if not TEST_START <= start <= through:
            parser.error("Training dates must follow frozen incumbent model selection.")
        rows = whole_series(forecaster.games, start, through)
        training = list(_episodes(rows, forecaster, width=BEAM_WIDTH, archived=archived,
                                  legacy=legacy, archive_replay=True))
        print(f"Assembled {len(training)} dated GRID training windows.", flush=True)
        weights, fitting_turns = _fit(training)
        artifact = {"version": 1, "features": list(FEATURES), "weights": weights, "beam_width": BEAM_WIDTH,
                    "incumbent_sha256": _incumbent_identity(args.bundle), "train_from": start, "train_through": through,
                    "fitting_turns": fitting_turns, "training": _report(training, weights)}
        _write(args.output, artifact)
        print(json.dumps({key: artifact[key] for key in ("weights", "fitting_turns", "training")}, indent=2), flush=True)
    else:
        model = AdaptivePairModel.load(args.model, args.bundle)
        split = calendar_date.fromisoformat(args.split).isoformat()
        series = completed_series(read_event_games(args.games))
        released = tuple(draft for members in series
                         for draft in _drafts(members, completion_day(members), forecaster.catalog))
        rows = sorted((forecaster.catalog.normalize_game(game) for members in series for game in members
                       if game["date"] > model.train_through), key=lambda game: (game["date"], game["drafted_at"]))
        results = list(_episodes(rows, forecaster, width=model.width, archived=archived, released=released, legacy=legacy))
        earlier_series = {str(members[0]["series_id"]) for members in series if completion_day(members) < split}
        later_series = {str(members[0]["series_id"]) for members in series if all(game["date"] >= split for game in members)}
        report = {"diagnostic_split": split, "all": _report(results, model.weights),
                  "earlier": _report([row for row in results if str(row.game["series_id"]) in earlier_series], model.weights),
                  "later": _report([row for row in results if str(row.game["series_id"]) in later_series], model.weights)}
        _write(args.output, report)
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
