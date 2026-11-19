"""Apply a verified private pick or pair recipe using released tournament evidence."""
from bisect import bisect_left
from collections import OrderedDict
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock

from fearless.challengers import incumbent_signature
from fearless.event_series import completed_series, completion_day, read_event_games
from fearless.model import DraftForecaster
from fearless.rules import DRAFT_LENGTH, schedule

RECIPES = ("fixed", "refresh", "fusion", "refresh_fusion")


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def verify_new_games(frozen, series):
    ids = {str(g["series_id"]) for g in frozen.games}
    games = {str(g.get("game_id")) for g in frozen.games if g.get("game_id")}
    def signature(row):
        return row["date"], tuple((a["kind"], frozen.catalog.canonical(a["champion"])) for a in row["actions"])
    drafts = {signature(g) for g in frozen.games if len(g["actions"]) == DRAFT_LENGTH}
    for group in series:
        if str(group[0]["series_id"]) in ids:
            raise ValueError("External data overlaps a frozen series.")
        for game in group:
            key = signature(game)
            if str(game["game_id"]) in games or key in drafts:
                raise ValueError("External data overlaps a frozen or previously supplied game.")
            drafts.add(key)


class EventForecaster:
    accepts_event = True

    def __init__(self, parent, rows, *, recipe="fixed", pick=None, pair=None):
        if recipe not in RECIPES or ("fusion" in recipe) != (pick is not None):
            raise ValueError("Invalid event pick recipe or missing fitted pick artifact.")
        self.parent = parent
        self.frozen = parent.frozen
        self.catalog = self.frozen.catalog
        self.recipe, self.pick, self.pair = recipe, pick, pair
        self.series = completed_series(rows)
        self.days = tuple(completion_day(series) for series in self.series)
        self.indices = {series[0]["series_id"]: i for i, series in enumerate(self.series)}
        verify_new_games(self.frozen, self.series)
        self.events = sorted({s[0]["event"] for s in self.series if s[0].get("event")})
        self._cache = OrderedDict()
        self._lock = Lock()
        self.updated = self._make_ranker() if "refresh" in recipe else None

    def released_games(self, omit=None):
        return [{**g, "finished_at": self.days[i] + "T23:59:59+00:00"}
                for i, series in enumerate(self.series) if i != omit for g in series]

    def _make_ranker(self, omit=None):
        return DraftForecaster(self.catalog, [*self.frozen.games, *self.released_games(omit)],
                               models=self.frozen.models, serving=self.frozen.serving)

    @classmethod
    def load(cls, parent, bundle, parent_policy, policy):
        policy = Path(policy)
        data = json.loads(policy.read_text())
        if (data["version"] != 1 or data["incumbent"] != incumbent_signature(bundle)
                or data["parent_sha256"] != digest(parent_policy)):
            raise ValueError("Event policy requires its evaluated parent and bundle.")
        paths = {}
        for key, entry in data["files"].items():
            path = policy.parent / entry["path"]
            if digest(path) != entry["sha256"]:
                raise ValueError("An event artifact differs from its evaluated snapshot.")
            paths[key] = path
        if set(paths) - {"pick", "pair", "events"} or "events" not in paths:
            raise ValueError("Invalid event policy files.")
        pick = pair = None
        if "pick" in paths:
            from fearless.patch_picks import PatchPickModel

            pick = PatchPickModel.load(paths["pick"], bundle)
        if "pair" in paths:
            from fearless.adaptive_pairs import AdaptivePairModel

            pair = AdaptivePairModel.load(paths["pair"], bundle)
        return cls(parent, read_event_games(paths["events"]), recipe=data["recipe"], pick=pick, pair=pair)

    def _snapshot(self, day, series_id):
        n = bisect_left(self.days, day)
        omit = self.indices.get(series_id)
        omit = omit if omit is not None and omit < n else None
        key = (n, omit)
        with self._lock:
            if key not in self._cache:
                ready = [g for i, s in enumerate(self.series[:n]) if i != omit for g in s]
                pick = self.pick.with_completed_series(ready) if self.pick and ready else self.pick
                pair = self.pair.with_completed_series(ready) if self.pair and ready else self.pair
                ranker = self._make_ranker(omit) if self.updated and omit is not None else self.updated
                last = max((self.days[i] for i in range(n) if i != omit), default=None)
                self._cache[key] = (pick, pair, ranker, last, n - int(omit is not None))
                if len(self._cache) > 4:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
            return self._cache[key]

    def forecast(self, *, limit=3, event=None, series_id=None, **context):
        current = context["current"]
        if (self.recipe == "fixed" or len(current) >= DRAFT_LENGTH
                or schedule(context["first"], context["second"])[len(current)][1] != "pick"):
            return self.parent.forecast(limit=limit, **context)
        pick, _, updated, last, count = self._snapshot(context["date"], series_id)
        if not count:
            return self.parent.forecast(limit=limit, **context)
        base = updated or self.parent
        full = base.forecast(limit=len(self.catalog.names) if pick else limit, **context)
        if not full.get("calibrated") or not full["choices"]:
            return {**full, "choices": full["choices"][:max(0, limit)]}
        adjusted = pick.adjust(choices=full["choices"], event=event, series_id=series_id, **context) if pick else None
        if not adjusted and updated is None:
            return {**full, "choices": full["choices"][:max(0, limit)]}
        detail = (adjusted or {}).get("patch_adaptation", {})
        return {**full, "choices": (adjusted or full)["choices"][:max(0, limit)],
                "method": "patch_pick_adaptation" if adjusted else "refreshed_pick",
                "as_of": max(filter(None, (last, full.get("as_of")))), "calibrated": False,
                "calibration_through": None, "model_source": "private_event", "probability_quality": "estimated",
                "event_evidence": {"series_through": last, "released_series": count, **detail}}

    def rank_pairs(self, *, event=None, series_id=None, **context):
        if self.pair is None:
            return self.parent.rank_pairs(**context)
        _, pair, _, _, _ = self._snapshot(context["date"], series_id)
        ranked = pair.rank_pairs(forecaster=self.frozen, event=event, series_id=series_id, **context)
        return ranked or self.parent.rank_pairs(**context)
