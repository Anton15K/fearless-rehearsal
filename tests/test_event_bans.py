import json

import numpy as np
import pytest

from fearless.ban_challenger import ROLE_FEATURES, BanChallenger
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_bans import EventBanRefresh
from fearless.event_confidence import EventConfidence
from fearless.event_forecaster import EventForecaster, digest
from fearless.event_series import completed_series
from fearless.model import DraftForecaster
from fearless.model_features import FEATURES, ChampionCatalog
from fearless.rules import schedule, series_locks
from scripts.transfer_forecasts import replay, reports, verify_cohort


CHAMPIONS = tuple(f"Champion {i:02d}" for i in range(60))


def game(series, number, day, champions, *, size=1, ending=None, locked=(), event="First Cup"):
    actions = [{"team": team, "kind": kind, "champion": champion}
               for (team, kind), champion in zip(schedule("blue", "red"), champions)]
    return {"series_id": series, "game_id": f"{series}/{number}", "game_number": number, "series_games": size,
            "date": day, "drafted_at": day + "T18:00:00Z", "finished_at": (ending or day) + "T19:00:00Z",
            "first": "blue", "second": "red", "patch": "16.20", "region": "CN", "event": event,
            "eligible": True, "finished": True, "fearless": True, "disabled": [], "locked": list(locked),
            "actions": actions, "rosters": {"blue": [{"player": "blue-starter", "champion": actions[9]["champion"],
                                                       "role": "top"}]}}


def policy():
    class Ranker:
        def predict(self, features, *, thread_count):
            return 15 * features[:, len(FEATURES) + ROLE_FEATURES.index("weighted_team_role_picks")]

    catalog = ChampionCatalog(releases={}, patches={"16.20": CHAMPIONS})
    frozen = DraftForecaster(catalog, [], serving={"pick": {"method": "baseline", "temperature": 1, "calibrated": True}})
    ban = BanChallenger(frozen, Ranker(), {"weight": 0.7, "challenger_temperature": 1.0, "temperature": 1.0})
    return DraftChallengers(frozen, ban=ban)


def ask(model, day, prefix=(), series_id=None):
    return model.forecast(date=day, patch="16.20", region="CN", first="blue", second="red", game_number=1,
                          current=prefix, pool=CHAMPIONS, limit=len(CHAMPIONS), series_id=series_id)


def test_refresh_waits_for_entire_series_and_uses_released_role_history():
    first = game("source", 1, "2026-10-15", CHAMPIONS[:20], size=2)
    second = game("source", 2, "2026-10-16", CHAMPIONS[20:40], size=2, ending="2026-10-17",
                  locked=series_locks([first["actions"]]))
    target = game("target", 1, "2026-10-18", CHAMPIONS[40:])["actions"]
    source = EventForecaster(policy(), [first, second], recipe="refresh")
    bans = EventBanRefresh(source)

    for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
        assert ask(bans, day, target[:12]) == ask(source, day, target[:12])

    late = ask(bans, "2026-10-18", target[:12])
    assert late["method"] == "refreshed_second_phase_ban" and late["underlying_method"] == "second_phase_ban_challenger"
    assert late["event_evidence"] == {"series_through": "2026-10-17", "released_series": 1}
    assert late["choices"][0]["champion"] in {CHAMPIONS[9], CHAMPIONS[29]}
    assert late["choices"] != ask(source, "2026-10-18", target[:12])["choices"]
    assert ask(bans, "2026-10-18", target[:12], "source") == ask(source, "2026-10-18", target[:12], "source")


def test_bans_refresh_openings_and_delegate_the_entire_pick_forecast():
    row = game("played", 1, "2026-10-15", CHAMPIONS[:20])
    source = EventForecaster(policy(), [row], recipe="refresh")
    bans = EventBanRefresh(source)
    refreshed = ask(bans, "2026-10-16")

    assert refreshed["choices"] != ask(source, "2026-10-16")["choices"]
    assert refreshed["method"] == "refreshed_opening_ban" and refreshed["underlying_method"] == "baseline"
    assert refreshed["calibrated"] is False
    assert ask(bans, "2026-10-16", row["actions"][:6]) == ask(source, "2026-10-16", row["actions"][:6])


def test_calibration_preserves_ties_and_metadata_for_a_full_distribution(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"models": {}}))
    (bundle / "history.jsonl.gz").write_bytes(b"test archive")
    artifact = tmp_path / "confidence.json"
    artifact.write_text(json.dumps({"version": 1, "incumbent": incumbent_signature(bundle),
                                    "recipe": "refresh", "temperature": 2.0}))
    model = EventConfidence.load(artifact, bundle)
    rows = [{"champion": name, "probability": value, "evidence": [name]}
            for name, value in [("Zoe", .45), ("Ahri", .45), ("Ashe", .10)]]

    calibrated = model.apply(rows)
    assert [r["champion"] for r in calibrated] == ["Zoe", "Ahri", "Ashe"]
    assert [r["evidence"] for r in calibrated] == [r["evidence"] for r in rows]
    p = [r["probability"] for r in calibrated]
    assert p[0] == p[1] < .45 and p[2] > .10
    assert np.sum(p) == pytest.approx(1)
    with pytest.raises(ValueError, match="sum to one"):
        model.apply(rows[:2])
    (bundle / "history.jsonl.gz").write_bytes(b"different archive")
    with pytest.raises(ValueError, match="incumbent"):
        EventConfidence.load(artifact, bundle)


def test_locked_cohort_refuses_changed_content_and_preserves_three_strata(tmp_path):
    path = tmp_path / "games.jsonl"
    rows = [game(f"series {i}", 1, "2026-10-15", CHAMPIONS[:20], event=f"Cup {i}") for i in range(3)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    lock = {"sha256": digest(path), "games": 3, "series": 3}
    series = verify_cohort(path, lock)
    assert len(series) == 3
    with pytest.raises(ValueError, match="counts"):
        verify_cohort(path, {**lock, "games": 2})
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="digest"):
        verify_cohort(path, lock)


def test_replay_counts_all_turns_and_calibrates_only_non_cold_picks():
    games = [game(f"series {i}", 1, f"2026-10-{15+i}", CHAMPIONS[:20], event=f"Cup {i}") for i in range(3)]
    bans, picks, coverage = replay(policy(), completed_series(games), {"16.20": CHAMPIONS}, EventConfidence(1.2))
    summary = reports(bans, picks)

    assert coverage == {"refreshed_bans": 20, "calibrated_picks": 20, "unchanged_pick_rankings": 30}
    assert len(bans["opening"]) == 18 and len(bans["late"]) == 12
    assert len(picks) == 30
    assert summary["confidence"]["hit3_delta_ci95"] == [0, 0]
    assert picks[0][2]["incumbent"] == picks[0][2]["candidate"]


def test_prior_archive_adds_only_earlier_evidence_and_is_never_scored():
    target = game("target", 1, "2026-10-15", CHAMPIONS[:20])
    past = game("past", 1, "2026-10-14", CHAMPIONS[20:40])
    future = game("future", 1, "2026-10-16", CHAMPIONS[40:])
    params = (completed_series([target]), {"16.20": CHAMPIONS}, EventConfidence(1.2))
    observed = replay(policy(), *params, prior=completed_series([past]))

    assert observed == replay(policy(), *params, prior=completed_series([past, future]))
    bans, picks, coverage = observed
    assert len(picks) == len(bans["opening"]) + len(bans["late"]) == 10
    assert coverage == {"refreshed_bans": 10, "calibrated_picks": 10, "unchanged_pick_rankings": 10}


def test_passed_bans_stay_in_the_prefix_and_outside_named_denominators():
    row = game("passed", 1, "2026-10-15", CHAMPIONS[:20])
    row["actions"][0]["champion"] = None
    row["actions"][12]["champion"] = None
    bans, picks, coverage = replay(policy(), completed_series([row]), {"16.20": CHAMPIONS}, EventConfidence(1.2))

    scores = reports(bans, picks)
    assert scores["ban"]["all"]["incumbent"]["turns"] == 8
    assert len(bans["opening"]) == 5 and len(bans["late"]) == 3
    assert coverage["passed_opening_bans"] == coverage["passed_late_bans"] == 1
    assert len(picks) == 10
