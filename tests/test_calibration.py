from dataclasses import replace

import pytest

from fearless.calibration import CalibrationPolicy, CalibrationSidecar, ScoredPick, adjust_probabilities


def record(series, day, completed, patch="16.17", target=1, split="later"):
    return ScoredPick(day, completed, patch, series, series, split, (0.8, 0.15, 0.05), target, position=6)


def test_adjusts_full_distribution_without_changing_rank_and_rejects_partial_choices():
    baseline = (0.8, 0.15, 0.05)
    softened = adjust_probabilities(baseline, 1.2)
    assert sum(softened) == pytest.approx(1.0)
    assert 0 < softened[2] < softened[1] < softened[0] < baseline[0]
    assert adjust_probabilities(baseline, 1.0) == baseline
    with pytest.raises(ValueError):
        adjust_probabilities(baseline[:2], 1.3)


def test_walk_forward_uses_only_earlier_whole_series_and_restricts_patch():
    policy = CalibrationPolicy(min_series=2, min_turns=2)
    rows = [
        record("prior", "2026-09-01", "2026-09-01"),
        record("finished-next-day", "2026-09-01", "2026-09-03"),
        record("future", "2026-09-04", "2026-09-04"),
        record("unclosed", "2026-09-01", None),
        record("too-far", "2026-09-01", "2026-09-01", patch="16.12"),
    ]
    sidecar = CalibrationSidecar.fit(rows, through="2026-09-06", policy=policy)
    assert sidecar.decision_for("2026-09-03", "16.17").source == "frozen"
    fitted = sidecar.decision_for("2026-09-04", "26.17")
    assert fitted.temperature > 1.0
    assert fitted.evidence_through == "2026-09-03"
    assert fitted.series == fitted.turns == 2
    assert fitted.source == "same_patch"
    assert sidecar.decision_for("2026-09-04", "16.17", series_id="prior").temperature == 1.0
    assert sidecar.decision_for("2026-09-04", "16.18").source == "nearby_patches"
    assert sidecar.temperature_for("2026-09-04", "16.20") == 1.0
    assert sidecar.temperature_for("2027-01-01", "16.17") == 1.0


def test_artifact_honors_requested_cutoff_and_model_identity(tmp_path):
    rows = [record("early", "2026-09-01", "2026-09-01"), record("late", "2026-09-01", "2026-09-04")]
    sidecar = CalibrationSidecar.fit(rows, through="2026-09-02", model_id="bundle-a")
    path = tmp_path / "pick.json"
    sidecar.save(path)
    loaded = CalibrationSidecar.load(path, model_id="bundle-a")
    assert loaded.fit_through == "2026-09-02"
    assert loaded.decision_for("2026-09-07", "16.17").series == 1
    with pytest.raises(ValueError, match="frozen bundle"):
        CalibrationSidecar.load(path, model_id="bundle-b")
    with pytest.raises(ValueError, match="series completion"):
        CalibrationSidecar.fit([rows[0], replace(rows[0], completed_day="2026-09-02")], through="2026-09-06")


def test_refresh_only_adds_unseen_completed_series_and_preserves_earlier_decisions(tmp_path):
    policy = CalibrationPolicy(min_series=2, min_turns=2)
    old = record("prior", "2026-09-01", "2026-09-01")
    original = CalibrationSidecar.fit([old], through="2026-09-02", policy=policy, model_id="bundle-a")
    output = tmp_path / "original.json"
    original.save(output)
    original = CalibrationSidecar.load(output, model_id="bundle-a")
    new = record("new", "2026-09-03", "2026-09-04")
    future = record("future", "2026-09-05", "2026-09-07")
    refreshed = original.refreshed([old, new, new, future], through="2026-09-05")

    assert refreshed.model_id == original.model_id
    assert refreshed.fit_through == "2026-09-05"
    assert refreshed.decision_for("2026-09-04", "16.17") == original.decision_for("2026-09-04", "16.17")
    decision = refreshed.decision_for("2026-09-05", "16.17")
    assert decision.series == decision.turns == 2
    assert decision.evidence_through == "2026-09-04"
    assert refreshed.decision_for("2026-09-08", "16.17").series == 2
    assert refreshed.decision_for("2026-09-08", "16.17", series_id="new").temperature == 1.0
    assert original.decision_for("2026-09-08", "16.17").series == 1
    with pytest.raises(ValueError, match="conflicting calibration evidence"):
        refreshed.refreshed([replace(old, target=0)], through="2026-09-06")
    with pytest.raises(ValueError, match="prior fit"):
        refreshed.refreshed([], through="2026-09-04")


def test_completion_uses_last_finished_game_in_utc_and_rejects_gaps():
    from fearless.calibration_replay import closed_series_days

    def game(series_id, game_number, **changes):
        return {
            "series_id": series_id, "game_number": game_number, "date": "2026-09-01",
            "actions": [None] * 20, "finished": True, "eligible": True, **changes,
        }

    assert closed_series_days([
        game("complete", 1),
        game("complete", 2, finished_at="2026-09-01T23:55:00-02:00"),
        game("missing-first", 2),
        game("unfinished", 1, finished=False),
        game("explicitly-partial", 1, series_complete=False),
        game("gapped", 1),
        game("gapped", 3),
    ]) == {"complete": "2026-09-02"}
