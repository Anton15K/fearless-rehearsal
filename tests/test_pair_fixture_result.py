from collections import Counter

import pytest

from fearless.rules import schedule
from scripts import pair_fixture_result as result


@pytest.fixture
def source():
    stage = "Legends Premier League/Season 5"
    match = stage + "_Round 1_1"
    first, second = "Colossal Gaming", "Forsaken (Polish Team)"
    game = {"OverviewPage": stage, "GameId": match + "_1"}
    teams = {"Team1": second, "Team2": first}
    raw = {
        "series": {"OverviewPage": stage, "MatchId": match, "start": "2026-11-20 10:00:00",
                   **teams, "BestOf": "3", "IsNullified": "0", "DisabledChampions": "Champion 25"},
        "score": {**game, **teams, "MatchId": match, "start": "2026-11-20 10:07:00",
                  "number": "1", "Patch": "26.23"},
        "duration": {**game, "duration": "31:07"},
        "schedule": {**game, "Blue": second, "Red": first, "FirstPick": first},
        "draft": {**game, "Team1": first, "Team2": second, "IsFilled": "1", "IsComplete": "1"},
    }
    slots = Counter()
    for i, (team, kind) in enumerate(schedule(first, second), 1):
        slots[team, kind] += 1
        side = 1 if team == first else 2
        raw["draft"][f"Team{side}{kind.title()}{slots[team, kind]}"] = f"Champion {i}"
    raw["draft"]["Team1Ban1"] = "No ban"
    return {"fetched_at": "2026-11-20T10:45:00+00:00", "tables": raw}


def parse(source):
    return result.reconcile(source, {"16.23": [f"Champion {i}" for i in range(1, 31)]})


def test_reconciles_actual_red_first_order_and_records_post_result_times(source):
    game = parse(source)
    assert game["first"] == "Colossal Gaming"
    assert game["second"] == "Forsaken (Polish Team)"
    assert game["patch"] == "16.23"
    assert game["game_started_at"] == "2026-11-20T10:07:00+00:00"
    assert game["estimated_finished_at"] == "2026-11-20T10:38:07+00:00"
    assert game["collected_at"] == "2026-11-20T10:45:00+00:00"
    assert game["actions"][0] == {"team": "Colossal Gaming", "kind": "ban", "champion": None}
    assert [action["champion"] for action in game["actions"][7:9]] == ["Champion 8", "Champion 9"]
    assert len(game["actions"]) == 20
    assert game["disabled"] == ["Champion 25"]
    assert game["fearless"] is None
    assert game["target_lock_times"] == {}


def test_waits_for_explicit_first_pick_even_when_blue_equals_draft_team1(source):
    selection = source["tables"]["schedule"]
    selection["Blue"], selection["Red"], selection["FirstPick"] = selection["Red"], selection["Blue"], ""
    with pytest.raises(result.PendingResult, match="first pick"):
        parse(source)


def test_rejects_conflicting_first_pick(source):
    selection = source["tables"]["schedule"]
    selection["FirstPick"] = selection["Blue"]
    with pytest.raises(ValueError, match="first pick disagrees"):
        parse(source)


def test_rejects_snapshot_for_a_different_fixture(source):
    source["tables"]["score"]["MatchId"] = "different match"
    with pytest.raises(ValueError, match="registered fixture"):
        parse(source)
