import json
from math import fsum, log

import pytest

from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_confidence import EventConfidence, PickConfidenceTurn, fit_temperature, score_refresh
from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog
from fearless.rules import schedule


@pytest.fixture
def artifact(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"models": {}}))
    (bundle / "history.jsonl.gz").write_bytes(b"test frozen archive")
    path = tmp_path / "confidence.json"
    data = {"version": 1, "incumbent": incumbent_signature(bundle), "recipe": "refresh", "temperature": 1.4}
    path.write_text(json.dumps(data))
    return path, bundle, data


def test_temperature_preserves_rank_full_normalization_and_choice_evidence(artifact):
    path, bundle, _ = artifact
    model = EventConfidence.load(path, bundle)
    choices = [
        {"champion": "Ahri", "probability": 0.7, "evidence": ["earlier picks"]},
        {"champion": "Vi", "probability": 0.15, "games": 3},
        {"champion": "Braum", "probability": 0.15},
    ]
    updated = model.apply(choices)
    assert [row["champion"] for row in updated] == ["Ahri", "Vi", "Braum"]
    assert 0 < updated[1]["probability"] == updated[2]["probability"] < updated[0]["probability"] < 0.7
    assert fsum(row["probability"] for row in updated) == pytest.approx(1.0)
    assert updated[0]["evidence"] == ["earlier picks"]
    assert updated[1]["games"] == 3
    assert choices[0]["probability"] == 0.7
    assert EventConfidence(1.0).apply(choices) == choices
    with pytest.raises(ValueError, match="(?i)full legal"):
        model.apply(choices[:2])


@pytest.mark.parametrize("bad_data", [
    {"recipe": "fusion"}, {"incumbent": "different"}, {"temperature": 0.01}, {"temperature": float("nan")},
])
def test_artifact_rejects_wrong_recipe_bundle_or_temperature(artifact, bad_data):
    path, bundle, recorded = artifact
    path.write_text(json.dumps({**recorded, **bad_data}))
    with pytest.raises(ValueError):
        EventConfidence.load(path, bundle)


def test_fit_improves_nll_and_does_not_apply_to_unchanged_picks():
    records = [
        PickConfidenceTurn(tournament, (0.8, 0.2), index % 2, True)
        for tournament in ("a", "b", "c") for index in range(8)
    ]
    refreshed_only, before_cold = fit_temperature(records)
    records.extend(
        PickConfidenceTurn(tournament, (0.8, 0.2), 0, False)
        for tournament in ("a", "b", "c") for _ in range(20)
    )
    calibration, report = fit_temperature(records)
    assert calibration.temperature == refreshed_only.temperature > 1
    nll_refreshed = before_cold["refit"]["calibrated"]["nll"]
    assert report["refit"]["calibrated"]["nll"] == pytest.approx((24 * nll_refreshed - 60 * log(0.8)) / 84)
    assert report["refit"]["eligible_turns"] == 24
    assert report["refit"]["turns"] == 84
    assert report["leave_one_tournament_out"]["calibrated"]["nll"] < report["leave_one_tournament_out"]["raw"]["nll"]
    assert report["refit"]["raw"]["hit_at_3"] == report["refit"]["calibrated"]["hit_at_3"]
    assert report["refit"]["raw"]["hit_at_1"] == report["refit"]["calibrated"]["hit_at_1"]


def test_scoring_only_marks_picks_after_earlier_completed_series():
    names = tuple(f"Champion {index:02}" for index in range(40))
    champions = ChampionCatalog.from_json({"16.20": list(names)})
    parent = DraftChallengers(DraftForecaster(champions, [], serving={
        "pick": {"method": "baseline", "calibrated": True, "temperature": 1.0},
    }))
    def game(series, date, end):
        return {
            "game_id": f"{series}/1", "series_id": series, "event": "synthetic", "game_number": 1,
            "series_games": 1, "eligible": True, "finished": True, "fearless": True,
            "region": "International", "date": date, "patch": "16.20", "first": "a", "second": "b",
            "drafted_at": f"{date}T23:00:00Z", "finished_at": end, "locked": [], "disabled": [],
            "actions": [
                {"team": team, "kind": kind, "champion": name}
                for (team, kind), name in zip(schedule("a", "b"), names)
            ],
        }
    rows = [
        game("first", "2026-10-15", "2026-10-16T01:00:00Z"),
        game("same-completion-day", "2026-10-16", "2026-10-16T23:55:00Z"),
        game("later", "2026-10-17", "2026-10-17T23:55:00Z"),
    ]
    turns = list(score_refresh(parent, rows, {"16.20": names}))
    assert [turn.refreshed for turn in turns] == [False] * 20 + [True] * 10
    assert all(fsum(turn.probabilities) == pytest.approx(1) for turn in turns)
