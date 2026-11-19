from copy import deepcopy
import json
from math import fsum

import pytest

from fearless.calibration import bundle_fingerprint
from fearless.patch_picks import PatchPickModel, fit
from fearless.rules import schedule


NAMES = tuple(f"C{i:02}" for i in range(50))
POOL = NAMES[:8] + NAMES[30:33]
CURRENT = [
    {"team": team, "kind": kind, "champion": name}
    for (team, kind), name in zip(schedule("A", "B"), NAMES[:6])
]
CHOICES = [
    {"champion": NAMES[6], "probability": 0.10, "evidence": ["frozen pick history"]},
    {"champion": NAMES[7], "probability": 0.10, "games": ["prior game"]},
    {"champion": NAMES[30], "probability": 0.30},
    {"champion": NAMES[31], "probability": 0.25},
    {"champion": NAMES[32], "probability": 0.25},
]


def game(*, day=1, end_day=None, series_id="past", number=1, total=1,
         patch="16.20", event="Event A", region="International"):
    end_day = day if end_day is None else end_day
    names = NAMES[(number - 1) * 20:number * 20]
    return {
        "date": f"2026-10-{day:02}", "patch": patch, "event": event, "region": region,
        "drafted_at": f"2026-10-{day:02}T01:00:00+00:00",
        "finished_at": f"2026-10-{end_day:02}T02:00:00+00:00",
        "eligible": True, "finished": True, "fearless": True,
        "first": "A", "second": "B", "series_id": series_id, "game_id": f"{series_id}-{number}",
        "series_games": total, "game_number": number,
        "actions": [
            {"team": team, "kind": kind, "champion": name}
            for (team, kind), name in zip(schedule("A", "B"), names, strict=True)
        ],
        "locked": [a["champion"] for a in game()["actions"] if a["kind"] == "pick"] if number == 2 else [],
    }


@pytest.fixture
def artifact(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({
        "models": {}, "catalog": {"patches": {"16.20": NAMES, "16.21": NAMES}}
    }))
    (bundle / "history.jsonl.gz").write_bytes(b"frozen history")
    path = tmp_path / "pick.json"
    path.write_text(json.dumps({
        "version": 1, "model_id": bundle_fingerprint(bundle),
        "parameters": {"max_mix": 0.8, "prior_strength": 20, "other_phase_weight": 0,
                       "other_game_weight": 1, "half_life_days": 21},
    }))
    return path, bundle


def adjust(model, **overrides):
    return model.adjust(**{
        "choices": CHOICES, "date": "2026-10-03", "patch": "16.20", "region": "International",
        "first": "A", "second": "B", "game_number": 1, "current": CURRENT, "pool": POOL,
        "event": "Event A", "series_id": "future", **overrides,
    })


def test_restraint_and_full_legal_distribution_from_one_completed_series(artifact):
    cold = PatchPickModel.load(*artifact)
    record = game()
    original_choices = deepcopy(CHOICES)
    warm = cold.with_completed_series([record])
    record["actions"][6]["champion"] = NAMES[32]

    result = adjust(warm)
    assert adjust(cold) is None
    assert result["method"] == "patch_pick_adaptation"
    assert result["patch_adaptation"]["series"] == 1
    assert result["patch_adaptation"]["evidence_through"] == "2026-10-01"
    assert 0 < result["patch_adaptation"]["mix_weight"] < 0.1
    updated = result["choices"]
    probabilities = [row["probability"] for row in updated]
    assert probabilities == sorted(probabilities, reverse=True)
    assert all(value > 0 for value in probabilities)
    assert fsum(probabilities) == pytest.approx(1.0)
    assert {row["champion"] for row in updated} == set(POOL) - set(NAMES[:6])
    target = next(row for row in updated if row["champion"] == NAMES[6])
    assert target["evidence"] == ["frozen pick history"]
    assert target["probability"] > 0.10
    assert CHOICES == original_choices


@pytest.mark.parametrize("overrides", [
    {"date": "2026-10-01"}, {"date": "2026-10-02"}, {"series_id": "past"},
    {"event": "Other event"}, {"patch": "16.21"}, {"event": None, "region": "Europe"},
    {"current": []}, {"current": game()["actions"]}, {"fearless": False},
])
def test_ineligible_evidence_or_turn_falls_back(artifact, overrides):
    cold = PatchPickModel.load(*artifact)
    warm = cold.with_completed_series([game(end_day=2)])
    assert adjust(warm, **overrides) is None


def test_whole_series_releases_after_its_last_game(artifact):
    cold = PatchPickModel.load(*artifact)
    first = game(total=2)
    second = game(day=2, number=2, total=2)
    with pytest.raises(ValueError):
        cold.with_completed_series([first])

    warm = cold.with_completed_series([second, first])
    assert adjust(warm, date="2026-10-02") is None
    assert adjust(warm, date="2026-10-03", patch="26.20")["patch_adaptation"]["series"] == 1
    assert adjust(warm.with_completed_series([first, second])) == adjust(warm)


def test_incomplete_choice_rows_cannot_be_rescaled(artifact):
    warm = PatchPickModel.load(*artifact).with_completed_series([game()])
    partial = [{"champion": NAMES[6], "probability": 1.0}]
    assert adjust(warm, choices=partial) is None


def test_locked_and_disabled_champions_do_not_receive_mass(artifact):
    warm = PatchPickModel.load(*artifact).with_completed_series([game()])
    result = adjust(warm, pool=[name for name in POOL if name != NAMES[31]], locked=[NAMES[7]],
                    choices=[{**row, "probability": row["probability"] / 0.65}
                             for row in CHOICES if row["champion"] not in {NAMES[7], NAMES[31]}])
    assert {row["champion"] for row in result["choices"]} == {NAMES[6], NAMES[30], NAMES[32]}
    assert fsum(row["probability"] for row in result["choices"]) == pytest.approx(1.0)


def test_model_is_bound_to_frozen_distribution(artifact):
    path, bundle = artifact
    (bundle / "history.jsonl.gz").write_bytes(b"changed history")
    with pytest.raises(ValueError, match="bundle"):
        PatchPickModel.load(path, bundle)


def test_fit_requires_disjoint_whole_series_around_the_cutoff(artifact):
    _, bundle = artifact
    games = [game(day=1, total=2), game(day=2, total=2, number=2), game(day=3, series_id="later")]
    with pytest.raises(ValueError, match="fit cutoff"):
        fit(bundle, games, fit_through="2026-10-01")
