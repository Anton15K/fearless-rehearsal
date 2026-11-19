"""Three frozen pair predictors evaluated on identical public draft information."""
from collections import Counter, defaultdict
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from random import Random

from fearless.adaptive_pairs import AdaptivePairModel, PAIR_WINDOWS
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_forecaster import digest, verify_new_games
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model_features import _patch_key
from fearless.pair_model import pair_turns
from fearless.rules import legal_champions, schedule, validate_action
from scripts.coach_frequency import archive_timeline
from scripts.compare_challengers import pick_pairs

METHODS = ("frequency", "adaptive", "role_first")
SIMULATIONS = 32
SEED = 10429
ROOT = Path(__file__).resolve().parents[1]


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def identities(bundle, parent, adaptive, roles, events, pools):
    # Bind source as well as weights. A persistently running trial detects code drift on restart.
    source = sorted((ROOT / "fearless").glob("*.py")) + [
        ROOT / "scripts" / name for name in ("coach_frequency.py", "compare_challengers.py", "pair_trial.py")]
    code = fingerprint({str(path.relative_to(ROOT)): digest(path) for path in source})
    common = {"implementation": code, "bundle": incumbent_signature(bundle), "parent": digest(parent),
              "events": digest(events), "pools": digest(pools)}
    return {name: fingerprint({**common, "method": name, "artifact": artifact}) for name, artifact in zip(
        METHODS, ("patch-frequency-v1", digest(adaptive), digest(roles)), strict=True)}


def validate_prefix(ctx, prefix):
    if ctx["first"] == ctx["second"] or not ctx["pool"]:
        raise ValueError("Draft teams or enabled pool are invalid.")
    for index, action in enumerate(prefix):
        validate_action(ctx["pool"], ctx["locked"], prefix[:index], ctx["first"], ctx["second"], **action)


def checked(pairs, legal):
    result = [tuple(sorted(pair)) for pair in pairs]
    if (len(result) > 3 or len(set(result)) != len(result)
            or any(len(pair) != 2 or len(set(pair)) != 2 or not set(pair) <= legal for pair in result)):
        raise ValueError("Pair forecast contains an illegal, repeated or excess alternative.")
    return result


class PairTrial:
    def __init__(self, parent, adaptive, role_first, games):
        self.parent, self.frozen = parent, parent.frozen
        self.adaptive, self.role_first = adaptive, role_first
        self.series = completed_series(games)
        verify_new_games(self.frozen, self.series)
        self.ends = {str(group[0]["series_id"]): completion_day(group) for group in self.series}
        self.archive = archive_timeline(self.frozen.games)
        self.snapshot = lru_cache(maxsize=2)(self._snapshot)

    @classmethod
    def load(cls, *, bundle, parent, adaptive, roles, events, extra=()):
        from fearless.role_first_pairs import RoleFirstPairModel

        return cls(DraftChallengers.load(bundle, parent), AdaptivePairModel.load(adaptive, bundle),
                   RoleFirstPairModel.load(roles, bundle), [*read_event_games(events), *extra])

    def _snapshot(self, day, omit):
        released = [g for s in self.series if self.ends[str(s[0]["series_id"])] < day
                    and str(s[0]["series_id"]) != omit for g in s]
        overall, patches = Counter(), defaultdict(Counter)
        sources = [g for end, g in self.archive if end < day and str(g["series_id"]) != omit]
        for game in (*sources, *released):
            patch = _patch_key(game["patch"])
            bucket = patches[patch]
            pairs = [tuple(sorted(map(self.frozen.catalog.canonical, pair)))
                     for _, _, pair in pair_turns(game["actions"])]
            overall.update(pairs)
            bucket.update(pairs)
        return (self.adaptive.with_completed_series(released), self.role_first.with_completed_series(released),
                overall, patches)

    def predict(self, ctx, prefix, *, event, series_id, include_roles=False):
        if len(prefix) not in PAIR_WINDOWS:
            raise ValueError("A pair forecast requires exactly 7, 9 or 17 preceding actions.")
        validate_prefix(ctx, prefix)
        day = ctx["date"]
        # An explicitly excluded series only changes the snapshot if it was already released.
        omit = str(series_id) if self.ends.get(str(series_id), day) < day else None
        adaptive, role_first, overall, patches = self.snapshot(day, omit)
        legal = set(legal_champions(ctx["pool"], ctx["locked"], prefix))
        canon = self.frozen.catalog.canonical
        names = {canon(name): name for name in legal}
        if len(names) != len(legal):
            raise ValueError("The pool contains duplicate champion aliases.")
        table = patches.get(_patch_key(ctx["patch"]), overall)
        observed = sorted((pair for pair in table if set(pair) <= names.keys()), key=lambda p: (-table[p], p))[:3]
        kw = dict(**ctx, current=prefix, event=event, series_id=series_id, forecaster=self.frozen, limit=3)
        active = adaptive.rank_pairs(**kw)
        incumbent = [r["champions"] for r in active] if active else pick_pairs(self.parent, ctx, prefix)
        ranked = role_first.rank_pairs(**kw)
        forecasts = {"frequency": checked(((names[a], names[b]) for a, b in observed), legal),
                     "adaptive": checked(incumbent, legal), "role_first": checked((r["champions"] for r in ranked), legal)}
        return forecasts, role_first.rank_roles(**kw) if include_roles else None

    def before_draft(self, ctx, *, event, series_id):
        rng = Random(SEED)
        counts = {slot: {name: Counter() for name in METHODS} for slot in PAIR_WINDOWS}
        order = schedule(ctx["first"], ctx["second"])
        for _ in range(SIMULATIONS):
            prefix = []
            for index, (team, kind) in enumerate(order[:PAIR_WINDOWS[-1] + 1]):
                if index in counts:
                    ranks, _ = self.predict(ctx, prefix, event=event, series_id=series_id)
                    for name, pairs in ranks.items():
                        for position, pair in enumerate(pairs):
                            counts[index][name][pair] += (6, 3, 2)[position]
                if index == PAIR_WINDOWS[-1]:
                    break
                response = self.frozen.forecast(**ctx, current=prefix, limit=len(ctx["pool"]))
                choices = response["choices"]
                if not choices:
                    raise ValueError("The one-step forecaster could not generate this simulated draft.")
                selected = rng.choices(choices, weights=[row["probability"] for row in choices], k=1)[0]
                prefix.append({"team": team, "kind": kind, "champion": selected["champion"]})
        return {str(slot): {name: sorted(table, key=lambda p: (-table[p], p))[:3] for name, table in methods.items()}
                for slot, methods in counts.items()}
