"""Private, opt-in ranker for consecutive unordered draft picks."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
from itertools import groupby
import json
from math import isfinite, log, log1p
from pathlib import Path
from threading import Lock

import numpy as np

from fearless.model import DraftForecaster, _method_scores, _softmax
from fearless.model_features import DraftContext, History, ROLES, TEST_START, TRAIN_START, evidence_date, prepare_games
from fearless.rules import DRAFT_LENGTH, legal_champions, schedule


TRAIN_THROUGH = "2025-12-31"
VALIDATION_FROM = "2026-01-01"
VALIDATION_THROUGH = "2026-03-27"
FEATURES = ("global_pair", "team_pair", "player_pair", "role_conflict")
ALL_ROLES = (1 << 5) - 1
ROLE_BITS = tuple(1 << index for index in range(len(ROLES)))
BEAM_WIDTH = 8
SIDECAR = "challenger.json"


def pair_turns(actions):
    for index, (a, b) in enumerate(zip(actions, actions[1:])):
        if a["kind"] == b["kind"] == "pick" and a["team"] == b["team"]:
            if a["champion"] and b["champion"] and a["champion"] != b["champion"]:
                yield index, a["team"], tuple(sorted((a["champion"], b["champion"])))


def whole_series(games, start, through):
    grouped = defaultdict(list)
    for game in games:
        grouped[str(game["series_id"])].append(game)
    return sorted((game for series in grouped.values()
                   if all(start <= game["date"] <= evidence_date(game) <= through for game in series)
                   for game in series if game.get("eligible")),
                  key=lambda game: (game["date"], str(game["series_id"]), game.get("game_number", 1)))


class PairHistory:
    """Index completed pair evidence by day so a query can only count earlier outcomes."""

    def __init__(self, games):
        self.global_pairs, self.team_pairs, self.player_pairs = (defaultdict(list) for _ in range(3))
        for game in sorted((game for game in games if game.get("eligible")), key=evidence_date):
            day = evidence_date(game)
            rosters = {team: {entry.get("champion"): str(entry["player"]) for entry in roster if entry.get("player")}
                       for team, roster in game.get("rosters", {}).items()}
            for _, team, pair in pair_turns(game["actions"]):
                self.global_pairs[pair].append(day)
                self.team_pairs[team, pair].append(day)
                for champion in pair:
                    player = rosters.get(team, {}).get(champion)
                    if player:
                        self.player_pairs[player, champion, pair].append(day)

    def counts(self, pair, team, players, day):
        return (
            bisect_left(self.global_pairs.get(pair, ()), day),
            bisect_left(self.team_pairs.get((team, pair), ()), day),
            sum(bisect_left(dates, day) for dates in self._player_dates(pair, players)),
        )

    def _player_dates(self, pair, players):
        return (self.player_pairs.get((str(player), champion, pair), ())
                for player in set(players) for champion in pair)

    def dated_evidence(self, pair, team, players, day):
        team_dates = self.team_pairs.get((team, pair), ())
        team_count = bisect_left(team_dates, day)
        latest_team = team_dates[team_count - 1] if team_count else None
        player_count, latest_player = 0, None
        for dates in self._player_dates(pair, players):
            count = bisect_left(dates, day)
            if count:
                player_count += count
                latest_player = max(latest_player or "", dates[count - 1])
        return team_count, latest_team, player_count, latest_player


def _role_assignments(masks):
    assignments = {0}
    for mask in masks:
        assignments = {used | role for used in assignments for role in ROLE_BITS
                       if mask & role and not used & role}
    return assignments


def _role_mask(history, champion, *, unsupported=ALL_ROLES):
    index = history.catalog.indices.get(champion)
    if index is None:
        return unsupported
    observations = history.role_counts[index]
    total = int(observations.sum())
    if total < 5:
        return unsupported
    return sum(bit for bit, count in zip(ROLE_BITS, observations, strict=True)
               if count >= 2 and count / total >= 0.12) or unsupported


def _pair_role_options(occupied, left_mask, right_mask):
    for used in occupied:
        for left in ROLE_BITS:
            if left & left_mask and not used & left:
                for right in ROLE_BITS:
                    if right & right_mask and not (used | left) & right:
                        yield left, right


def _ally_picks(current, team):
    return [action["champion"] for action in current if action["team"] == team and action["kind"] == "pick"]


def _pair_features(pairs, *, pair_history, history, current, day, team, rosters):
    ally = _ally_picks(current, team)
    roles = {champion: _role_mask(history, champion) for champion in set(ally).union(*pairs)}
    occupied = _role_assignments([roles[champion] for champion in ally])
    players = history.player_ids(team, rosters)
    features = {}
    for pair in pairs:
        global_count, team_count, player_count = pair_history.counts(pair, team, players, day)
        feasible = not occupied or any(_pair_role_options(occupied, roles[pair[0]], roles[pair[1]]))
        features[pair] = (log1p(global_count / 3), log1p(team_count / 2), log1p(player_count / 2), -int(not feasible))
    return features


def _role_evidence(display, canonical, history, occupied):
    observed = [_role_mask(history, champion, unsupported=0) for champion in canonical]
    if not occupied or not any(observed):
        return None
    options = sorted(set(_pair_role_options(occupied, *(mask or ALL_ROLES for mask in observed))))
    if not options:
        return "Earlier role observations suggest a position overlap in this draft."

    names = dict(zip(ROLE_BITS, ROLES, strict=True))
    if not all(observed):
        known = 0 if observed[0] else 1
        roles = " or ".join(names[role] for role in sorted({option[known] for option in options}))
        return f"Plausible positions for {display[known]} from earlier role observations include {roles}."

    def describe(option):
        return f"{display[0]} {names[option[0]]} with {display[1]} {names[option[1]]}"

    first = options[0]
    if len(options) == 1:
        return f"Earlier role observations support {describe(first)}."
    other = max(options[1:], key=lambda option: sum(a != b for a, b in zip(first, option, strict=True)))
    return f"Earlier role observations support assignments such as {describe(first)} or {describe(other)}."


def _dated_pair_evidence(pair_history, pair, team, players, day, supplied):
    team_count, latest_team, player_count, latest_player = pair_history.dated_evidence(pair, team, players, day)
    evidence = []
    if team_count:
        noun = "draft" if team_count == 1 else "drafts"
        evidence.append(f"This team picked the pair in {team_count} earlier {noun}, last observed {latest_team}.")
    if player_count:
        lineup = "The supplied lineup" if supplied else "The last observed lineup"
        noun = "observation" if player_count == 1 else "observations"
        evidence.append(f"{lineup} has {player_count} player–champion {noun} with this pair, last observed {latest_player}.")
    return evidence


def _beam(forecast, current, team, width, legal):
    wide, shipped = Counter(), Counter()
    for first_index, first in enumerate(forecast(current, width).get("choices", ())[:width]):
        a, a_mass = first["champion"], first["probability"]
        if a not in legal or not a_mass:
            continue
        prefix = (*current, {"champion": a, "team": team, "kind": "pick"})
        for second_index, second in enumerate(forecast(prefix, width).get("choices", ())[:width]):
            b, b_mass = second["champion"], second["probability"]
            if b not in legal or b == a or not b_mass:
                continue
            pair = tuple(sorted((a, b)))
            wide[pair] += a_mass * b_mass
            if first_index < 3 and second_index < 3:
                shipped[pair] += a_mass * b_mass
    return dict(wide), dict(shipped)


def _position(first, second, index):
    steps = schedule(first, second)[index:index + 2]
    return steps[0][0] if len(steps) == 2 and steps[0] == steps[1] and steps[0][1] == "pick" else None


def _ranking(mass, features, weights):
    return sorted(mass, key=lambda pair: (-log(mass[pair]) - sum(w * f for w, f in zip(weights, features[pair], strict=True)), pair))


def _incumbent_identity(bundle):
    manifest_path = Path(bundle) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    filenames = ["manifest.json", "history.jsonl.gz"]
    if manifest["models"].get("pick"):
        filenames.append(manifest["models"]["pick"])
    digest = sha256()
    for name in filenames:
        digest.update((Path(bundle) / name).read_bytes())
    return digest.hexdigest()


class PairChallenger:
    def __init__(self, artifact):
        if (artifact.get("version") != 1 or not 3 <= artifact["beam_width"] <= 12
                or len(artifact["weights"]) != len(FEATURES) or not all(map(isfinite, artifact["weights"]))):
            raise ValueError("Invalid pair challenger artifact.")
        self.artifact = artifact
        self._lock = Lock()
        self._incumbent = None
        self._pair_history = None

    @classmethod
    def load(cls, directory, incumbent=None):
        artifact = json.loads((Path(directory) / SIDECAR).read_text(encoding="utf8"))
        if incumbent is not None and artifact.get("incumbent_sha256") != _incumbent_identity(incumbent):
            raise ValueError("Pair challenger was fitted against a different incumbent bundle.")
        return cls(artifact)

    def rank_pairs(self, *, forecaster, date, patch, region, first, second, game_number, current,
                   locked=(), pool=None, rosters=None, limit=3, fearless=True):
        """Return ranked alternatives only. The incumbent's outlook retains its own path mass."""
        team = _position(first, second, len(current))
        if team is None or date < TEST_START or date <= self.artifact["train_through"] or limit <= 0:
            return []
        with self._lock:
            if self._incumbent is not forecaster:
                self._pair_history = PairHistory(forecaster.games)
                self._incumbent = forecaster
            pair_history = self._pair_history

        enabled = tuple(dict.fromkeys(forecaster.catalog.available(date, patch) if pool is None else pool))
        locked = tuple(locked) if fearless else ()

        def forecast(prefix, top):
            return forecaster.forecast(date=date, patch=patch, region=region, first=first, second=second,
                                       game_number=game_number, current=prefix, locked=locked, pool=enabled,
                                       rosters=rosters, limit=top, fearless=fearless)

        mass, _ = _beam(forecast, current, team, self.artifact["beam_width"], set(legal_champions(enabled, locked, current)))
        if not mass:
            return []
        # Use baseline output labels for display and canonical names for historical evidence.
        canonical = forecaster.catalog.canonical
        canonical_pairs = {pair: tuple(map(canonical, pair)) for pair in mass}
        pair_keys = {pair: tuple(sorted(champions)) for pair, champions in canonical_pairs.items()}
        normalized = [forecaster.catalog.normalize_action(action) for action in current]
        history = forecaster._history(date)
        historical = _pair_features(pair_keys.values(), pair_history=pair_history, history=history,
                                    current=normalized, day=date, team=team, rosters=rosters)
        features = {pair: historical[key] for pair, key in pair_keys.items()}
        selected = _ranking(mass, features, self.artifact["weights"])[:limit]
        occupied = _role_assignments([_role_mask(history, champion) for champion in _ally_picks(normalized, team)])
        players = history.player_ids(team, rosters)
        alternatives = []
        for pair in selected:
            evidence = _dated_pair_evidence(pair_history, pair_keys[pair], team, players, date, bool(rosters and team in rosters))
            positions = _role_evidence(pair, canonical_pairs[pair], history, occupied)
            if positions:
                evidence.insert(0, positions)
            row = {"champions": list(pair)}
            if evidence:
                row["evidence"] = evidence
            alternatives.append(row)
        return alternatives


def _episodes(rows, forecaster, width):
    history = History(forecaster.catalog)
    distribution = iter(forecaster.games)
    pending = next(distribution, None)
    pair_history = PairHistory(forecaster.games)
    for day, games in groupby(rows, key=lambda game: game["date"]):
        past = []
        while pending is not None and evidence_date(pending) < day:
            past.append(pending)
            pending = next(distribution, None)
        history.before(day, past)
        # Before this boundary the original bundle itself serves an unlearned historical prior.
        config = forecaster.serving.get("pick") if day >= TEST_START else None
        config = config or {"method": "baseline", "temperature": 1.0}
        for game in games:
            context = DraftContext.from_game(game)
            locked = game["locked"]

            def forecast(prefix, limit):
                turn = history.features(context, prefix, locked)
                if not turn.champions:
                    return {"choices": []}
                learned = (forecaster.models["pick"].predict(turn.values, thread_count=1)
                           if config["method"] != "baseline" else None)
                scores = _method_scores(config, turn.column("baseline"), learned)
                probabilities = _softmax(scores)
                top = np.argsort(-scores, kind="stable")[:limit]
                return {"choices": [{"champion": turn.champions[index], "probability": float(probabilities[index])}
                                     for index in top]}

            for index, team, target in pair_turns(game["actions"]):
                if len(game["actions"]) != DRAFT_LENGTH or team != _position(context.first, context.second, index):
                    continue
                current = game["actions"][:index]
                pool = forecaster.catalog.available(day, context.patch)
                legal = set(legal_champions(pool, locked if context.fearless else (), current))
                mass, shipped = _beam(forecast, current, team, width, legal)
                features = _pair_features(mass, pair_history=pair_history, history=history, current=current,
                                          day=day, team=team, rosters=None)
                yield (str(game["series_id"]), target, mass, shipped, features, set(target) <= legal)


def _fit(episodes):
    from scipy.optimize import minimize

    offsets, sizes, targets, rows = [], [], [], []
    for _, target, mass, _, features, _ in episodes:
        if target not in mass or len(mass) < 2:
            continue
        choices = list(mass)
        targets.append(len(rows) + choices.index(target))
        sizes.append(len(choices))
        for pair in choices:
            offsets.append(log(mass[pair]))
            rows.append(features[pair])
    if not targets:
        raise ValueError("No training pair targets are present in the wide incumbent beam.")
    offsets, rows = np.asarray(offsets), np.asarray(rows)
    starts = np.cumsum([0, *sizes[:-1]])
    target_features = rows[targets].mean(axis=0)

    def objective(weights):
        logits = offsets + np.sum(rows * weights, axis=1)
        centered = logits - np.repeat(np.maximum.reduceat(logits, starts), sizes)
        exponent = np.exp(centered)
        denominator = np.add.reduceat(exponent, starts)
        probability = exponent / np.repeat(denominator, sizes)
        loss = np.mean(np.log(denominator) - centered[targets]) + 0.025 * (weights @ weights)
        gradient = (probability @ rows) / len(targets) - target_features + 0.05 * weights
        return loss, gradient

    result = minimize(objective, np.zeros(len(FEATURES)), jac=True, method="L-BFGS-B", bounds=[(0, 4)] * len(FEATURES))
    if not result.success:
        raise ValueError(f"Pair challenger fitting failed: {result.message}")
    return result.x.tolist(), len(targets)


def _rank_stats(ranks):
    total = len(ranks)
    return {
        "coverage": sum(rank is not None for rank in ranks) / total,
        "hit_at_1": sum(rank == 0 for rank in ranks) / total,
        "hit_at_3": sum(rank is not None and rank < 3 for rank in ranks) / total,
        "mrr": sum(1 / (rank + 1) for rank in ranks if rank is not None) / total,
    }


def _diagnostic(episodes, weights):
    if not episodes:
        raise ValueError("No eligible consecutive picks in this whole-series window.")
    ranks = defaultdict(list)
    clustered = defaultdict(lambda: [0, 0, 0])
    for series, target, wide, shipped, features, legal in episodes:
        choices = {
            "incumbent": sorted(shipped, key=lambda pair: (-shipped[pair], pair)),
            "wide_incumbent": sorted(wide, key=lambda pair: (-wide[pair], pair)),
            "challenger": _ranking(wide, features, weights),
        }
        for method, ranked in choices.items():
            ranks[method].append(ranked.index(target) if target in ranked else None)
        found = [int(target in ranked[:3]) for ranked in choices.values()]
        counts = clustered[series]
        counts[0] += found[2] - found[0]
        counts[1] += found[2] - found[1]
        counts[2] += 1
    grouped = np.asarray(list(clustered.values()))
    interval = {}
    if len(grouped) > 1:
        indices = np.random.default_rng(10429).integers(0, len(grouped), size=(1000, len(grouped)))
        samples = grouped[indices].sum(axis=1)
        for index, method in enumerate(("incumbent", "wide_incumbent")):
            interval[method] = np.percentile(samples[:, index] / samples[:, 2], [2.5, 97.5]).tolist()
    return {"series": len(clustered), "turns": len(episodes), "legal_targets": sum(row[-1] for row in episodes),
            "ranking": {method: _rank_stats(values) for method, values in ranks.items()},
            "pair_hit_at_3_delta_ci95": interval}


def _write(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.chmod(0o600)
    temporary.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fitting = commands.add_parser("fit", help="fit on 2025 whole series and report early 2026 validation")
    evaluation = commands.add_parser("evaluate", help="compare frozen methods on an unopened later whole-series window")
    for command in (fitting, evaluation):
        command.add_argument("--incumbent", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--games", type=Path, help="normalized JSONL; defaults to the incumbent history")
    fitting.add_argument("--train-from", default=TRAIN_START)
    fitting.add_argument("--train-through", default=TRAIN_THROUGH)
    evaluation.add_argument("--challenger", type=Path, required=True)
    evaluation.add_argument("--from", dest="start", required=True)
    evaluation.add_argument("--through", required=True)
    args = parser.parse_args()

    forecaster = DraftForecaster.load(args.incumbent)
    source = forecaster.games
    if args.games is not None:
        with args.games.open(encoding="utf8") as infile:
            source = [forecaster.catalog.normalize_game(game) for game in prepare_games(json.loads(line) for line in infile if line.strip())]
    if args.command == "fit":
        start, end = map(lambda day: date.fromisoformat(day).isoformat(), (args.train_from, args.train_through))
        if not TRAIN_START <= start <= end <= TRAIN_THROUGH:
            parser.error("Pair fitting must use whole series within the 2025 calendar year.")
        train = list(_episodes(whole_series(source, start, end), forecaster, BEAM_WIDTH))
        print(f"Constructed {len(train)} private 2025 pair turns.", flush=True)
        weights, fitted = _fit(train)
        validation = list(_episodes(whole_series(source, VALIDATION_FROM, VALIDATION_THROUGH), forecaster, BEAM_WIDTH))
        artifact = {"version": 1, "beam_width": BEAM_WIDTH, "features": list(FEATURES), "weights": weights,
                    "incumbent_sha256": _incumbent_identity(args.incumbent), "train_from": start, "train_through": end,
                    "training_turns": len(train), "fitting_turns": fitted, "validation_from": VALIDATION_FROM,
                    "validation_through": VALIDATION_THROUGH, "validation": _diagnostic(validation, weights)}
        _write(args.output / SIDECAR, artifact)
        print(json.dumps({key: artifact[key] for key in ("training_turns", "fitting_turns", "weights", "validation")}, indent=2))
    else:
        start, end = map(lambda day: date.fromisoformat(day).isoformat(), (args.start, args.through))
        if not TEST_START <= start <= end:
            parser.error("Evaluation must start after the frozen validation period.")
        candidate = PairChallenger.load(args.challenger, incumbent=args.incumbent).artifact
        episodes = list(_episodes(whole_series(source, start, end), forecaster, candidate["beam_width"]))
        report = {"from": start, "through": end, "scope": "consecutive same-team unordered picks",
                  **_diagnostic(episodes, candidate["weights"])}
        _write(args.output, report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
