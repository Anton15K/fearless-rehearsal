"""Run the promoted ban models with evidence from released whole series."""

from collections import OrderedDict
from threading import Lock

from fearless.ban_challenger import BanChallenger
from fearless.event_forecaster import EventForecaster
from fearless.rules import DRAFT_LENGTH, schedule


class EventBanRefresh:
    accepts_event = True

    def __init__(self, forecaster):
        if forecaster.parent.ban is None:
            raise ValueError("Event ban refresh requires the promoted ban challenger.")
        self.forecaster = forecaster
        self.catalog = forecaster.catalog
        self.evidence = forecaster if forecaster.updated is not None else EventForecaster(
            forecaster.parent, (game for series in forecaster.series for game in series), recipe="refresh",
        )
        self._challengers = OrderedDict()
        self._lock = Lock()

    def _challenger(self, updated):
        with self._lock:
            if updated not in self._challengers:
                promoted = self.forecaster.parent.ban
                self._challengers[updated] = BanChallenger(updated, promoted.ranker, promoted.config)
                if len(self._challengers) > 4:
                    self._challengers.popitem(last=False)
            self._challengers.move_to_end(updated)
            return self._challengers[updated]

    def forecast(self, *, limit=3, event=None, series_id=None, **context):
        current = context["current"]
        if (len(current) >= DRAFT_LENGTH
                or schedule(context["first"], context["second"])[len(current)][1] != "ban"):
            return self.forecaster.forecast(limit=limit, event=event, series_id=series_id, **context)
        _, _, updated, last, count = self.evidence._snapshot(context["date"], series_id)
        if not count:
            return self.forecaster.parent.forecast(limit=limit, **context)
        result = self._challenger(updated).forecast(limit=limit, **context)
        method = "refreshed_opening_ban" if len(current) < 6 else "refreshed_second_phase_ban"
        return {**result, "method": method, "underlying_method": result.get("method"),
                "calibrated": False, "calibration_through": None, "model_source": "private_event",
                "probability_quality": "estimated", "event_evidence": {"series_through": last, "released_series": count}}

    def rank_pairs(self, **context):
        return self.forecaster.rank_pairs(**context)
