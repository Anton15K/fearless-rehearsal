from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fearless.event_forecaster import digest
from fearless.model_features import ChampionCatalog
from fearless.pair_trial import METHODS
from fearless.rules import schedule
from scripts.pair_pilot import make_envelope, read_verified_seal, score, write_exclusive


NAMES = tuple(f"C{i:02d}" for i in range(30))
POOLS = {"16.22": list(NAMES), "16.23": list(NAMES)}
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
PUBLISHED = "2026-11-19T12:01:00Z"
PUBLIC_RECORD = "https://github.com/example/repo/issues/1#issuecomment-5"


@pytest.fixture
def fixture():
    return dict(version=1, fixture_id="record-1", series_id="series-1", date="2026-11-19", region="EMEA",
                event="Pilot Cup", teams=["home", "away"], patches=["16.22", "16.23"], disabled=[NAMES[-1]],
                fearless=False, schedule_source="https://official.example/schedule",
                draft_source="https://official.example/draft", timing_source="https://official.example/log",
                draft_deadline="2026-11-19T13:00:00+00:00")


@pytest.fixture
def trial():
    def before_draft(ctx, *, event, series_id):
        calls.append((ctx, event, series_id))
        offset = 0 if ctx["first"] == "away" else 20
        chosen = {"7": (7, 8), "9": (9, 10), "17": (17, 18)}
        return {slot: {name: [[NAMES[(a + offset) % 29], NAMES[(b + offset) % 29]]]
                       for name in METHODS} for slot, (a, b) in chosen.items()}

    calls = []
    return SimpleNamespace(frozen=SimpleNamespace(catalog=ChampionCatalog.from_json(POOLS)),
                           before_draft=before_draft, ends={}, calls=calls)


def actual(fixture, *, patch="16.23", first="away", second="home"):
    return {**{key: fixture[key] for key in ("fixture_id", "series_id", "date", "event", "region", "fearless",
                                            "disabled", "draft_source", "timing_source")},
            "version": 1, "verified": True, "patch": patch, "first": first, "second": second, "game_number": 1,
            "drafted_at": "2026-11-19T13:25:09Z", "finished_at": "2026-11-19T14:05:09Z",
            "timing": {"kind": "official_draft_start", "timestamp": "2026-11-19T14:25:09+01:00"},
            "actions": [{"team": team, "kind": kind, "champion": champion}
                        for (team, kind), champion in zip(schedule(first, second), NAMES)]}


def test_envelope_seals_all_registered_game_one_scenarios_without_target_inputs(fixture, trial):
    envelope = make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: NOW)

    assert set((s["patch"], s["first"], s["second"]) for s in envelope["scenarios"]) == {
        (patch, first, second) for patch in fixture["patches"] for first, second in (("home", "away"), ("away", "home"))}
    assert Counter((context["patch"], context["first"]) for context, _, _ in trial.calls) == {
        (s["patch"], s["first"]): 1 for s in envelope["scenarios"]}
    for ctx, event, sid in trial.calls:
        assert ctx["game_number"] == 1 and ctx["locked"] == [] and ctx["fearless"] is False
        assert ctx["pool"] == list(NAMES[:-1]) and "rosters" not in ctx and "actions" not in ctx
        assert event == fixture["event"] and sid == fixture["series_id"]


def test_atomic_first_write_and_independent_public_hash(fixture, trial, tmp_path):
    output = tmp_path / "sealed.json"
    envelope = make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: NOW)
    deadline = datetime.fromisoformat(fixture["draft_deadline"])
    write_exclusive(output, envelope, deadline=deadline, clock=lambda: NOW)
    recorded = digest(output)
    with pytest.raises(FileExistsError):
        write_exclusive(output, {"different": True}, deadline=deadline, clock=lambda: NOW)
    assert digest(output) == recorded and output.stat().st_mode & 0o777 == 0o600
    assert read_verified_seal(output, recorded, fixture["fixture_id"]) == envelope
    with pytest.raises(ValueError, match="public SHA"):
        read_verified_seal(output, "0" * 64, fixture["fixture_id"])
    with pytest.raises(ValueError, match="fixture"):
        read_verified_seal(output, recorded, "different")


def test_selects_exactly_one_canonical_order_and_scores_three_panels(fixture, trial):
    envelope = make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: NOW)
    report = score(envelope, actual(fixture), POOLS, trial.frozen.catalog, PUBLISHED, PUBLIC_RECORD)
    assert report["scope"] == "pilot" and report["operating_mode"] == "standard Game 1"
    assert report["scenario"] == {"patch": "16.23", "first": "away", "second": "home"}
    assert set(report["methods"]) == set(METHODS)
    assert all(summary["hits_at_3"] == 3 for summary in report["methods"].values())
    switched = actual(fixture, first="home", second="away")
    assert all(summary["hits_at_3"] == 0 for summary in score(
        envelope, switched, POOLS, trial.frozen.catalog, PUBLISHED, PUBLIC_RECORD)["methods"].values())


@pytest.mark.parametrize("update,match", [
    ({"patch": "16.24"}, "scenario"), ({"first": "Home"}, "scenario|teams"),
    ({"disabled": []}, "disables"), ({"game_number": 2}, "Game 1"), ({"fearless": True}, "format"),
    ({"timing_source": "https://different.example/log"}, "timing source"),
    ({"timing": {"kind": "vod_broadcast_start", "timestamp": "2026-11-19T13:25:09Z"}}, "official draft start"),
    ({"timing": {"kind": "official_draft_start", "timestamp": "2026-11-19T13:33:00Z"}}, "timestamp"),
    ({"verified": False}, "verified"),
])
def test_refuses_unregistered_or_unverified_outcomes(fixture, trial, update, match):
    envelope = make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: NOW)
    with pytest.raises(ValueError, match=match):
        score(envelope, {**actual(fixture), **update}, POOLS, trial.frozen.catalog, PUBLISHED, PUBLIC_RECORD)


def test_rejects_late_publication_duplicate_scenario_and_deadline_crossing(fixture, trial):
    envelope = make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: NOW)
    with pytest.raises(ValueError, match="deadline|publication"):
        score(envelope, actual(fixture), POOLS, trial.frozen.catalog, "2026-11-19T13:02:00Z", PUBLIC_RECORD)
    duplicated = deepcopy(envelope)
    duplicated["scenarios"].append(duplicated["scenarios"][0])
    with pytest.raises(ValueError, match="scenario"):
        score(duplicated, actual(fixture), POOLS, trial.frozen.catalog, PUBLISHED, PUBLIC_RECORD)
    times = iter((NOW, datetime.fromisoformat(fixture["draft_deadline"])))
    with pytest.raises(ValueError, match="deadline"):
        make_envelope(fixture, trial, POOLS, "freeze-hash", clock=lambda: next(times))
