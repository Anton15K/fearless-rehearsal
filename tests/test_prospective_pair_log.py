from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3

import pytest

from fearless.prospective_pair_log import TrialLedger
from fearless.rules import schedule


PROTOCOL = "d" * 64
MODELS = {"parent": "a" * 64, "direct": "b" * 64, "role_first": "c" * 64}
SOURCE = "verified draft feed"
POOL = [f"C{i:02}" for i in range(50)]
ACTIONS = [
    {"team": team, "kind": kind, "champion": champ}
    for (team, kind), champ in zip(schedule("Home", "Away"), POOL)
]
TRIAL = {"trial_id": "first test"}
GAME = {**TRIAL, "game_id": "game-1"}
BINDING = {"protocol_sha256": PROTOCOL, "models": MODELS}


def actual_context(**overrides):
    return {
        "game_id": GAME["game_id"], "patch": "17.1", "first": "Home", "second": "Away",
        "game_number": 1, "locked": [], "pool": POOL, **overrides,
    }


def time(hm):
    return f"2027-01-01T{hm}:00+00:00"


def ranked(slot):
    return {
        "parent": [[POOL[slot + 1], POOL[slot]]],
        "direct": [[POOL[24], POOL[25]]],
        "role_first": [],
    }


class Clock:
    def __init__(self, hm):
        self.set(hm)

    def set(self, hm):
        self.instant = datetime.fromisoformat(time(hm))

    def __call__(self):
        return self.instant


def freeze(ledger, *, games=None):
    return ledger.freeze_trial(
        **TRIAL, **BINDING, enrollment_deadline=time("10:20"), timing_sources=[SOURCE],
        games=games or [{"game_id": "game-1", "starts_at": time("11:00"), "eligible": True}],
    )


def commit(ledger, **overrides):
    return ledger.commit_game(**{
        **GAME, **BINDING, "evidence_sha256": "e" * 64, "patch": "17.1", "first": "Home", "second": "Away",
        "date": "2027-01-01", "region": "International", "event": "Winter Cup", "series_id": "cup/1",
        "game_number": 1, "locked": [], "pool": POOL, "rosters": {"Home": ["Known player"]}, **overrides,
    })


def window(ledger, slot=7, **overrides):
    return ledger.record_window(**{
        **GAME, **BINDING, "prefix": ACTIONS[:slot], "rankings": ranked(slot),
        "observed_at": time("11:03"), "source": SOURCE, **overrides,
    })


@pytest.fixture
def ledgers(tmp_path):
    clock = Clock("10:00")
    path = tmp_path / "private.sqlite3"
    ledger = TrialLedger(path, clock=clock)
    freeze(ledger)
    return ledger, clock, path


def test_pre_draft_and_live_windows_receive_separate_scores(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:04")
    window(ledger)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), source=SOURCE, draft_started_at=time("11:00"),
                          target_times={"7": time("11:05")})

    report = ledger.score(TRIAL["trial_id"])
    assert report["games"]["eligible"] == 1
    for panel in ("pre_draft", "conditional"):
        stage = report[panel]
        assert stage["eligible_windows"] == 3
        assert stage["statuses"] == {"missed": 2, "scored": 1}
        assert stage["models"]["parent"] == {"hits": 1, "scored": 1, "abstained": 0}
        assert stage["models"]["direct"]["hits"] == 0
        assert stage["models"]["role_first"]["abstained"] == 1
    with pytest.raises(ValueError, match="outcome"):
        window(ledger, slot=9)


def test_scheduled_start_does_not_prove_pre_draft_timing(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:05")
    window(ledger)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), source=SOURCE)

    result = ledger.score(TRIAL["trial_id"])
    assert result["pre_draft"]["statuses"] == {"missed": 2, "retrospective_unknown": 1}
    assert result["conditional"]["statuses"] == {"missed": 2, "retrospective_unknown": 1}


def test_live_still_open_attestations_can_verify_both_predictions(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("10:31")
    ledger.attest_unopened_draft(**GAME, observed_at=time("10:31"), source=SOURCE)
    clock.set("11:04")
    window(ledger)
    with pytest.raises(ValueError, match="after"):
        ledger.attest_open_window(**GAME, prefix=ACTIONS[:7], observed_at=time("11:03"), source=SOURCE)
    clock.set("11:05")
    ledger.attest_open_window(**GAME, prefix=ACTIONS[:7], observed_at=time("11:05"), source=SOURCE)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), source=SOURCE)

    report = ledger.score(TRIAL["trial_id"])
    assert report["pre_draft"]["statuses"] == {"missed": 2, "scored": 1}
    assert report["conditional"]["statuses"] == {"missed": 2, "scored": 1}


def test_actual_clock_and_exact_prefix_prevent_backfilled_hits(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:06")
    window(ledger)
    wrong = [{**ACTIONS[0], "champion": POOL[35]}, *ACTIONS[1:9]]
    clock.set("11:08")
    window(ledger, slot=9, prefix=wrong, observed_at=time("11:07"))
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), draft_started_at=time("10:25"), source=SOURCE,
                          target_times={"7": time("11:05"), "9": time("11:10")})

    result = ledger.score(TRIAL["trial_id"])
    assert result["pre_draft"]["statuses"] == {"missed": 2, "retrospective_late": 1}
    assert result["conditional"]["statuses"] == {"missed": 1, "retrospective_late": 2}
    assert result["conditional"]["models"]["parent"]["hits"] == 0


def test_prefix_mismatch_with_valid_clocks_never_gets_a_score(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger)
    clock.set("11:04")
    window(ledger, prefix=[{**ACTIONS[0], "champion": POOL[35]}, *ACTIONS[1:7]])
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), source=SOURCE, draft_started_at=time("11:00"),
                          target_times={"7": time("11:05")})
    assert ledger.score(TRIAL["trial_id"])["conditional"]["statuses"] == {"missed": 2, "prefix_mismatch": 1}


def test_first_write_wins_and_bound_hashes_cannot_be_swapped(ledgers):
    ledger, clock, path = ledgers
    clock.set("10:30")
    original = commit(ledger)
    with pytest.raises(ValueError, match="binding"):
        commit(ledger, models={**MODELS, "direct": "f" * 64})
    with pytest.raises(ValueError, match="immutable"):
        commit(ledger, patch="17.2")
    clock.set("11:04")
    assert commit(ledger) == original

    def write(slot_ranks):
        try:
            return window(TrialLedger(path, clock=clock), rankings=slot_ranks)
        except ValueError as error:
            assert "immutable" in str(error)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = list(pool.map(write, [ranked(7), ranked(9)]))
    assert sum(value is not None for value in writes) == 1

    with sqlite3.connect(path) as db:
        for statement in (
            "UPDATE forecasts SET recorded_at='old'",
            "DELETE FROM forecasts",
            "INSERT OR REPLACE INTO forecasts SELECT * FROM forecasts",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)


def test_unenrolled_and_cancelled_games_keep_fixed_coverage(tmp_path):
    clock = Clock("10:00")
    ledger = TrialLedger(tmp_path / "private.sqlite3", clock=clock)
    schedule = [
        {"game_id": "game-1", "starts_at": time("11:00"), "eligible": True},
        {"game_id": "decider", "starts_at": time("12:00"), "eligible": True},
        {"game_id": "exhibition", "starts_at": time("11:00"), "eligible": False, "exclusion_reason": "not competitive"},
    ]
    freeze(ledger, games=schedule)
    with pytest.raises(ValueError, match="immutable"):
        freeze(ledger)
    clock.set("11:05")
    with pytest.raises(ValueError, match="deadline"):
        commit(ledger)
    with pytest.raises(ValueError, match="eligible"):
        commit(ledger, game_id="exhibition")
    with pytest.raises(ValueError, match="registered"):
        commit(ledger, game_id="unregistered")
    ledger.record_cancelled(**TRIAL, game_id="decider", reason="series ended 2-0", source=SOURCE)

    result = ledger.score(TRIAL["trial_id"])
    assert result["games"] == {"registered": 3, "eligible": 2, "committed": 0, "completed": 0,
                               "cancelled": 1, "awaiting_outcome": 1}
    assert result["conditional"]["eligible_windows"] == 6
    assert result["conditional"]["statuses"] == {"cancelled": 3, "missed": 3}


def test_requires_verified_prefix_and_refuses_future_observations(ledgers):
    ledger, clock, path = ledgers
    clock.set("10:30")
    commit(ledger)
    clock.set("11:04")
    with pytest.raises(ValueError, match="timing source"):
        window(ledger, source="unverified")
    with pytest.raises(ValueError, match="future"):
        window(ledger, observed_at=time("11:05"))
    with pytest.raises(ValueError, match="pair"):
        window(ledger, rankings={"parent": [[POOL[0], POOL[11]]], "direct": [], "role_first": []})
    with pytest.raises(ValueError, match="timing source"):
        ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), draft_started_at=time("11:00"))
    assert path.stat().st_mode & 0o077 == 0


def test_trial_cannot_be_registered_after_utc_deadline(tmp_path):
    ledger = TrialLedger(tmp_path / "private.sqlite3", clock=Clock("10:20"))
    with pytest.raises(ValueError, match="deadline"):
        freeze(ledger)


def test_verified_times_can_arrive_after_the_immutable_outcome(ledgers):
    ledger, clock, path = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:04")
    window(ledger)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(), source=SOURCE)
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT * FROM outcomes").fetchall()

    clock.set("11:45")
    ledger.record_verified_timing(**GAME, source=SOURCE, draft_started_at=time("11:00"),
                                  target_times={7: time("11:05")})
    result = ledger.score(TRIAL["trial_id"])
    for panel in ("pre_draft", "conditional"):
        assert result[panel]["statuses"] == {"missed": 2, "scored": 1}
    with pytest.raises(ValueError, match="immutable"):
        ledger.record_verified_timing(**GAME, source=SOURCE, target_times={7: time("11:03")})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM outcomes").fetchall() == original


def test_rejects_backfill_after_verified_target_without_waiting_for_outcome(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger)
    clock.set("11:05")
    ledger.record_verified_timing(**GAME, target_times={"7": time("11:05")}, source=SOURCE)
    with pytest.raises(ValueError, match="target action"):
        window(ledger)


def test_readback_contains_only_frozen_context_and_time(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    receipt = commit(ledger, pre_draft_rankings={"7": ranked(7)})
    context = ledger.game_context(TRIAL["trial_id"], GAME["game_id"])
    assert {k: context[k] for k in ("date", "region", "event", "series_id", "recorded_at")} == {
        "date": "2027-01-01", "region": "International", "event": "Winter Cup", "series_id": "cup/1",
        "recorded_at": receipt,
    }
    assert context["evidence_sha256"] == "e" * 64
    assert context["models"] == MODELS
    assert "rankings" not in context and "actions" not in context
    context["rosters"]["Home"].append("later player")
    assert ledger.game_context(TRIAL["trial_id"], GAME["game_id"])["rosters"] == {"Home": ["Known player"]}


@pytest.mark.parametrize("changes", [
    {"patch": "17.2"}, {"locked": [POOL[-1]]}, {"pool": POOL[:-1]}, {"game_id": "a different game"},
    {"fearless": False},
])
def test_verified_fixture_differences_are_unscored_even_when_actions_were_legal(ledgers, changes):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:04")
    window(ledger)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, actual_context=actual_context(**changes),
                          source=SOURCE, draft_started_at=time("11:00"), target_times={7: time("11:05")})

    result = ledger.score(TRIAL["trial_id"])
    for panel in ("pre_draft", "conditional"):
        assert result[panel]["statuses"] == {"missed": 2, "context_mismatch": 1}
        assert result[panel]["models"]["parent"]["scored"] == 0


def test_fixture_context_can_be_verified_after_the_raw_outcome(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    commit(ledger, pre_draft_rankings={"7": ranked(7)})
    clock.set("11:04")
    window(ledger)
    clock.set("11:40")
    ledger.record_outcome(**GAME, actions=ACTIONS, source=SOURCE, draft_started_at=time("11:00"),
                          target_times={7: time("11:05")})
    assert ledger.score(TRIAL["trial_id"])["conditional"]["statuses"] == {"missed": 2, "context_unknown": 1}
    ledger.record_verified_context(**GAME, actual_context=actual_context(), source=SOURCE)
    assert ledger.score(TRIAL["trial_id"])["conditional"]["statuses"] == {"missed": 2, "scored": 1}


def test_cannot_freeze_a_future_evidence_date_relative_to_registered_game(ledgers):
    ledger, clock, _ = ledgers
    clock.set("10:30")
    with pytest.raises(ValueError, match="date"):
        commit(ledger, date="2027-01-02")
