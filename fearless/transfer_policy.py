"""Serve the independently confirmed updates to an existing tournament forecast."""
import json
from pathlib import Path

from fearless.event_bans import EventBanRefresh
from fearless.event_confidence import EventConfidence
from fearless.event_forecaster import digest
from fearless.rules import DRAFT_LENGTH, schedule


class TransferPolicy:
    accepts_event = True

    def __init__(self, source, *, ban=False, confidence=None):
        if confidence and source.recipe != "refresh":
            raise ValueError("Confidence calibration requires the refreshed pick policy.")
        self.source = source
        self.frozen = source.frozen
        self.catalog, self.events = source.catalog, source.events
        self.ban = EventBanRefresh(source) if ban else None
        self.confidence = confidence

    def released_games(self):
        return self.source.released_games()

    @classmethod
    def load(cls, source, bundle, source_path, path):
        path = Path(path)
        data = json.loads(path.read_text())
        if data.get("version") != 1 or data.get("event_policy_sha256") != digest(source_path):
            raise ValueError("Transfer policy requires its verified tournament forecast.")
        if type(data["ban"]) is not bool:
            raise ValueError("Unrecognized transfer ban setting.")
        confidence = None
        if data.get("confidence") is not None:
            ref = data["confidence"]
            model = path.parent / ref["path"]
            if digest(model) != ref["sha256"]:
                raise ValueError("Pick confidence changed since confirmation.")
            confidence = EventConfidence.load(model, bundle)
        return cls(source, ban=data["ban"], confidence=confidence)

    def forecast(self, *, limit=3, **context):
        position = len(context["current"])
        if position < DRAFT_LENGTH:
            kind = schedule(context["first"], context["second"])[position][1]
            if kind == "ban" and self.ban:
                return self.ban.forecast(limit=limit, **context)
            if kind == "pick" and self.confidence:
                result = self.source.forecast(limit=len(self.catalog.names), **context)
                if result.get("method") == "refreshed_pick" and result["choices"]:
                    corrected = self.confidence.apply(result["choices"])
                    return {**result, "method": "temperature_refreshed_pick", "choices": corrected[:max(0, limit)],
                            "event_evidence": {**result["event_evidence"], "temperature": self.confidence.temperature}}
                return {**result, "choices": result["choices"][:max(0, limit)]}
        return self.source.forecast(limit=limit, **context)

    def rank_pairs(self, **context):
        return self.source.rank_pairs(**context)
