"""Compose independently enabled private predictors against their original frozen bundle."""
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from fearless.model import DraftForecaster
from fearless.rules import DRAFT_LENGTH, schedule


def incumbent_signature(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    files = sorted({"manifest.json", "history.jsonl.gz", *manifest["models"].values()})
    digest = sha256()
    for name in files:
        digest.update(name.encode() + b"\0" + sha256((bundle / name).read_bytes()).digest())
    return digest.hexdigest()


class DraftChallengers:
    def __init__(self, frozen, *, ban=None, calibration=None, pair=None):
        self.frozen = frozen
        self.ban = ban
        self.calibration = calibration
        self.pair = pair
        self.catalog = frozen.catalog

    @classmethod
    def load(cls, bundle, policy):
        from fearless.calibration import CalibrationSidecar, bundle_fingerprint

        policy = Path(policy)
        settings = json.loads(policy.read_text())
        if settings.get("version") != 1 or settings.get("incumbent") != incumbent_signature(bundle):
            raise ValueError("Challenger policy is incompatible with the configured incumbent bundle.")
        components = settings["components"]
        if not isinstance(components, dict) or set(components) - {"ban", "pair", "calibration"}:
            raise ValueError("Unknown challenger components.")
        paths = {name: policy.parent / value for name, value in components.items()}
        ban = None
        if "ban" in paths:
            from fearless.ban_challenger import BanChallenger

            ban = BanChallenger.load(paths["ban"], bundle)
        frozen = ban.frozen if ban is not None else DraftForecaster.load(bundle)
        pair = None
        if "pair" in paths:
            from fearless.pair_model import PairChallenger

            pair = PairChallenger.load(paths["pair"], incumbent=bundle)
        calibration = (CalibrationSidecar.load(paths["calibration"], model_id=bundle_fingerprint(bundle))
                       if "calibration" in paths else None)
        return cls(frozen, ban=ban, calibration=calibration, pair=pair)

    def forecast(self, *, limit=3, **context):
        current = context["current"]
        if len(current) == DRAFT_LENGTH:
            return self.frozen.forecast(limit=limit, **context)
        _, kind = schedule(context["first"], context["second"])[len(current)]
        if kind == "ban" and self.ban is not None:
            return self.ban.forecast(limit=limit, **context)
        if kind != "pick" or self.calibration is None:
            return self.frozen.forecast(limit=limit, **context)
        day, patch = context["date"], context["patch"]
        decision = self.calibration.decision_for(day, patch)
        if decision.temperature == 1.0:
            return self.frozen.forecast(limit=limit, **context)
        from fearless.calibration import adjust_probabilities

        result = self.frozen.forecast(limit=len(self.catalog.names), **context)
        if not result.get("calibrated") or not result["choices"]:
            return {**result, "choices": result["choices"][:max(0, limit)]}
        probabilities = adjust_probabilities([row["probability"] for row in result["choices"]], decision.temperature)
        choices = [{**row, "probability": p} for row, p in zip(result["choices"][:max(0, limit)], probabilities)]
        return {**result, "choices": choices, "pick_calibration": asdict(decision),
                "calibration_through": decision.evidence_through or result["calibration_through"]}

    def rank_pairs(self, **context):
        if self.pair is None:
            return []
        return self.pair.rank_pairs(forecaster=self.frozen, **context)
