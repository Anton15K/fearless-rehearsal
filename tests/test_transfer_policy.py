import json
from math import fsum

import numpy as np
import pytest

from fearless.ban_challenger import BanChallenger
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_confidence import EventConfidence
from fearless.event_forecaster import EventForecaster, digest
from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog
from fearless.pair_model import PairChallenger
from fearless.rules import schedule
from fearless.transfer_policy import TransferPolicy


CHAMPIONS = tuple(f"Champion {index:02d}" for index in range(45))
PATCH = "16.20"
TEMP = 1.3


@pytest.fixture
def data():
    class Ranker:
        def predict(self, features, *, thread_count):
            return np.zeros(len(features))

    teams = ("first", "second")
    actions = [{"team": team, "kind": kind, "champion": champion}
               for (team, kind), champion in zip(schedule(*teams), CHAMPIONS)]
    prior = {"series_id": "released-series", "game_id": "released-series/1", "series_games": 1, "game_number": 1,
             "date": "2026-10-15", "drafted_at": "2026-10-15T22:00:00Z", "finished_at": "2026-10-16T01:00:00Z",
             "event": "Prior cup", "region": "CN", "patch": PATCH, "first": teams[0], "second": teams[1],
             "eligible": True, "finished": True, "fearless": True, "actions": actions, "locked": [], "disabled": []}
    catalog = ChampionCatalog(releases={}, patches={PATCH: CHAMPIONS})
    frozen = DraftForecaster(catalog, [], serving={"pick": {"method": "baseline", "temperature": 1.0, "calibrated": True}})
    ban = BanChallenger(frozen, Ranker(), {"weight": 0.7, "temperature": 1.0, "challenger_temperature": 1.0})
    pair = PairChallenger({"version": 1, "beam_width": 8, "train_through": "2025-12-31", "weights": [0] * 4})
    parent = DraftChallengers(frozen, ban=ban, pair=pair)
    return parent, prior


def at(prior, index, *, day="2026-10-17", series_id="active-series"):
    return {**{field: prior[field] for field in ("patch", "region", "first", "second", "game_number", "locked")},
            "pool": CHAMPIONS, "current": prior["actions"][:index], "date": day, "event": "Active cup",
            "series_id": series_id}


def test_temperature_normalizes_the_full_pick_distribution_before_truncation(data):
    parent, prior = data
    source = EventForecaster(parent, [prior], recipe="refresh")
    model = TransferPolicy(source, confidence=EventConfidence(TEMP))
    ctx = at(prior, 6)
    incumbent = source.forecast(**ctx, limit=len(CHAMPIONS))
    adjusted = model.forecast(**ctx, limit=len(CHAMPIONS))

    assert incumbent["method"] == "refreshed_pick"
    assert adjusted["method"] == "temperature_refreshed_pick"
    assert {**incumbent["event_evidence"], "temperature": TEMP} == adjusted["event_evidence"]
    assert [row["champion"] for row in adjusted["choices"]] == [row["champion"] for row in incumbent["choices"]]
    assert fsum(row["probability"] for row in adjusted["choices"]) == pytest.approx(1.0)
    assert model.forecast(**ctx, limit=3)["choices"] == adjusted["choices"][:3]
    weights = [row["probability"] ** (1 / TEMP) for row in incumbent["choices"]]
    assert adjusted["choices"][0]["probability"] == pytest.approx(weights[0] / fsum(weights))
    assert adjusted["choices"][0]["evidence"] == incumbent["choices"][0]["evidence"]


def test_cold_and_same_series_fallbacks_preserve_the_original_payload(data):
    parent, prior = data
    source = EventForecaster(parent, [prior], recipe="refresh")
    model = TransferPolicy(source, ban=True, confidence=EventConfidence(TEMP))

    for index in (0, 6, 12, 20):
        for ctx in (at(prior, index, day="2026-10-16"), at(prior, index, series_id=prior["series_id"])):
            expected = source.forecast(**ctx, limit=2)
            assert model.forecast(**ctx, limit=2) == expected
    assert model.released_games() == source.released_games()


def test_ban_refresh_is_independent_and_pairs_delegate_unchanged(data):
    parent, prior = data
    source = EventForecaster(parent, [prior], recipe="refresh")
    confidence = EventConfidence(TEMP)
    both = TransferPolicy(source, ban=True, confidence=confidence)
    bans_only = TransferPolicy(source, ban=True)
    confidence_only = TransferPolicy(source, confidence=confidence)

    for index, method in ((0, "refreshed_opening_ban"), (12, "refreshed_second_phase_ban")):
        ctx = at(prior, index)
        assert both.forecast(**ctx) == bans_only.forecast(**ctx)
        assert both.forecast(**ctx)["method"] == method
        assert confidence_only.forecast(**ctx) == source.forecast(**ctx)
    assert bans_only.forecast(**at(prior, 6)) == source.forecast(**at(prior, 6))
    original_pairs = source.rank_pairs(**at(prior, 7))
    assert original_pairs
    assert both.rank_pairs(**at(prior, 7)) == original_pairs


def test_loading_checks_the_parent_policy_and_relative_confidence_artifact(data, tmp_path):
    parent, prior = data
    source = EventForecaster(parent, [prior], recipe="refresh")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"models": {}}))
    (bundle / "history.jsonl.gz").write_bytes(b"synthetic frozen archive")
    event_policy = tmp_path / "parent.json"
    event_policy.write_text(json.dumps({"version": 1, "recipe": "refresh"}))
    assets = tmp_path / "assets"
    assets.mkdir()
    confidence = assets / "confidence.json"
    confidence.write_text(json.dumps({"version": 1, "incumbent": incumbent_signature(bundle),
                                     "recipe": "refresh", "temperature": TEMP}))
    path = tmp_path / "transfer.json"
    artifact = {"version": 1, "event_policy_sha256": digest(event_policy), "ban": False,
                "confidence": {"path": "assets/confidence.json", "sha256": digest(confidence)}}
    path.write_text(json.dumps(artifact))
    policy = TransferPolicy.load(source, bundle, event_policy, path)
    assert policy.forecast(**at(prior, 6))["method"] == "temperature_refreshed_pick"

    event_policy.write_text(json.dumps({"version": 1, "recipe": "fixed"}))
    with pytest.raises(ValueError, match="verified tournament forecast"):
        TransferPolicy.load(source, bundle, event_policy, path)
    path.write_text(json.dumps({**artifact, "event_policy_sha256": digest(event_policy)}))
    confidence.write_text(confidence.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        TransferPolicy.load(source, bundle, event_policy, path)


def test_confidence_requires_the_refresh_recipe(data):
    parent, prior = data
    source = EventForecaster(parent, [prior], recipe="fixed")
    with pytest.raises(ValueError, match="refreshed pick"):
        TransferPolicy(source, confidence=EventConfidence(TEMP))
