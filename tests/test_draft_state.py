import json

from fastapi.testclient import TestClient
import pytest

from fearless.api import create_app
from fearless.app_catalog import Match, MatchCatalog
from fearless.rehearsal import advance_history, create_catalog_plan, new_plan, node_state, select_opening
from fearless.rules import schedule


@pytest.fixture
def board(tmp_path):
    with TestClient(create_app(database=tmp_path / "series.sqlite3")) as client:
        yield client


def submit(board, url, **data):
    response = board.post(url, json=data)
    assert response.status_code < 300, response.text
    return response.json()


def matchup(board, **overrides):
    fields = {
        "match_id": "prospective", "teams": ["Alpha", "Beta"], "coach_team": "Alpha", "format": "Bo3",
        "patch": "16.17", "date": "2026-11-23", "first": "Alpha", "blue": "Beta", **overrides,
    }
    plan = submit(board, "/api/plans", **fields)
    return f"/api/plans/{plan['id']}", plan["root_id"]


def state(board, url, node_id):
    response = board.get(f"{url}/nodes/{node_id}")
    assert response.status_code == 200, response.text
    return response.json()


def choose(board, url, parent, **data):
    return submit(board, f"{url}/selection", parent_id=parent, **data)["selected_node"]


def draft(board, url, parent):
    position = state(board, url, parent)
    while position["kind"] in {"pick", "ban"}:
        plan = submit(
            board, f"{url}/actions", parent_id=position["node_id"],
            champion=position["legal"][0] if position["kind"] == "pick" else None,
        )
        position = state(board, url, plan["selected_node"])
    return position


def winner(board, url, parent, team):
    return submit(board, f"{url}/result", parent_id=parent, winner=team)["selected_node"]


def test_persisted_opening_variation_leaves_existing_descendants_intact(board):
    url, root = matchup(board)
    original = state(board, url, root)
    assert (original["first"], original["blue"], original["red"], original["team"]) == (
        "Alpha", "Beta", "Alpha", "Alpha",
    )
    original_ban = submit(board, f"{url}/actions", parent_id=root, champion=original["legal"][0])
    alternate = choose(board, url, root, first="Beta", blue="Beta")
    plan = board.get(url).json()
    assert plan["selected_node"] == alternate
    assert next(node for node in plan["nodes"] if node["id"] == alternate)["kind"] == "selection"

    varied = state(board, url, alternate)
    assert (varied["first"], varied["blue"], varied["team"], varied["actions"]) == ("Beta", "Beta", "Beta", [])
    original_child = state(board, url, original_ban["selected_node"])
    assert original_child["actions"][0]["team"] == "Alpha"
    assert original_child["first"] == "Alpha"
    assert choose(board, url, alternate, first="Alpha", blue="Beta") == root
    assert board.post(f"{url}/selection", json={
        "parent_id": original_child["node_id"], "first": "Beta", "blue": "Beta",
    }).status_code == 409


def test_confirmed_results_control_loser_selection_and_clinching(board):
    url, root = matchup(board)
    game1 = draft(board, url, root)
    assert game1["series_status"] == "awaiting_result"
    assert game1["score"] == {"Alpha": 0, "Beta": 0}
    assert board.post(f"{url}/selection", json={
        "parent_id": game1["node_id"], "first": "Alpha", "blue": "Beta", "choice": "map_side",
    }).status_code == 409

    result1 = winner(board, url, game1["node_id"], "Alpha")
    assert winner(board, url, game1["node_id"], "Alpha") == result1
    assert board.post(f"{url}/result", json={
        "parent_id": game1["node_id"], "winner": "Beta",
    }).status_code == 409
    confirmed = state(board, url, result1)
    assert confirmed["selection_right"] == "Beta"
    assert confirmed["series_status"] == "awaiting_selection"
    assert confirmed["score"] == {"Alpha": 1, "Beta": 0}
    assert confirmed["game_winner"] == "Alpha"

    for incomplete in ({"blue": None, "choice": "map_side"}, {"blue": "Beta"}):
        assert board.post(f"{url}/selection", json={
            "parent_id": result1, "first": "Alpha", **incomplete,
        }).status_code == 422
    game2_open = choose(board, url, result1, first="Alpha", blue="Beta", choice="map_side")
    opened = state(board, url, game2_open)
    assert (opened["first"], opened["blue"], opened["selection_choice"], opened["selection_right"]) == (
        "Alpha", "Beta", "map_side", "Beta",
    )
    assert set(opened["locked"]) == {action["champion"] for action in game1["actions"] if action["kind"] == "pick"}

    game2 = draft(board, url, game2_open)
    result2 = winner(board, url, game2["node_id"], "Alpha")
    final = state(board, url, result2)
    assert final["completed"] is True
    assert final["series_status"] == "clinched"
    assert final["series_winner"] == "Alpha"
    assert final["score"] == {"Alpha": 2, "Beta": 0}
    assert final["can_rehearse_next"] is True
    assert board.post(f"{url}/selection", json={
        "parent_id": result2, "first": "Beta", "blue": "Alpha", "choice": "first_pick",
    }).status_code == 409

    prep = choose(board, url, result2, first="Beta", blue=None, hypothetical=True)
    rehearsed = state(board, url, prep)
    assert (rehearsed["game"], rehearsed["progression"], rehearsed["blue"]) == (3, "hypothetical", None)
    assert rehearsed["score"] == final["score"]
    assert rehearsed["series_winner"] == "Alpha"
    game3 = draft(board, url, prep)
    assert game3["series_status"] == "rehearsal_complete"
    assert game3["can_rehearse_next"] is False
    assert board.post(f"{url}/result", json={
        "parent_id": game3["node_id"], "winner": "Beta",
    }).status_code == 409


def test_decider_still_requires_a_result_and_next_first_pick_is_independent(board):
    url, root = matchup(board)
    game1 = draft(board, url, root)
    result1 = winner(board, url, game1["node_id"], "Alpha")
    game2_open = choose(board, url, result1, first="Beta", blue="Alpha", choice="first_pick")
    game2 = draft(board, url, game2_open)
    result2 = winner(board, url, game2["node_id"], "Beta")
    game3_open = choose(board, url, result2, first="Alpha", blue="Beta", choice="first_pick")
    game3 = draft(board, url, game3_open)
    assert game3["score"] == {"Alpha": 1, "Beta": 1}
    assert game3["series_status"] == "awaiting_result"
    assert game3["completed"] is False
    assert game3["can_rehearse_next"] is False
    result3 = winner(board, url, game3["node_id"], "Beta")
    final = state(board, url, result3)
    assert final["series_status"] == "clinched"
    assert final["score"] == {"Alpha": 1, "Beta": 2}


def test_historical_replay_reveals_result_after_last_action_and_counts_prior_wins():
    champions = [f"Champion {i}" for i in range(40)]
    past, current = [
        [
            {"team": team, "kind": kind, "champion": champion}
            for (team, kind), champion in zip(schedule(first, second), pool)
        ]
        for first, second, pool in [
            ("Alpha", "Beta", champions[:20]),
            ("Beta", "Alpha", champions[20:]),
        ]
    ]
    match = Match(
        metadata={
            "id": "archive", "title": "Alpha vs Beta", "teams": ["Alpha", "Beta"], "source": "historical",
            "date": "2026-11-23", "patch": "16.17", "format": "Bo3", "first": "Beta", "blue": "Alpha",
            "prior_winners": ["Alpha"], "rosters": {},
        },
        region="NA", team_ids={"Alpha": "alpha", "Beta": "beta"}, player_ids={},
        previous=[past], historical={2: {"first": "Beta", "blue": "Alpha", "winner": "Alpha", "actions": current}},
        opening=[], coach_team="Alpha",
    )
    plan = new_plan(match, tuple(champions), {})
    opening = node_state(plan, plan["root_id"])
    assert opening["blue"] == "Alpha"
    assert opening["game_winner"] is None
    assert opening["score"] == {"Alpha": 1, "Beta": 0}
    select_opening(plan, plan["root_id"], "Beta", "Beta")
    changed = node_state(plan, plan["selected_node"])
    assert changed["history"] is None
    select_opening(plan, changed["node_id"], "Beta", "Alpha", disabled=[])
    assert plan["selected_node"] == plan["root_id"]

    for _ in range(20):
        advance_history(plan, plan["selected_node"])
    drafted = node_state(plan, plan["selected_node"])
    assert drafted["game_winner"] is None
    assert drafted["history"]["kind"] == "result"
    advance_history(plan, plan["selected_node"])
    final = node_state(plan, plan["selected_node"])
    assert final["progression"] == "historical"
    assert final["series_winner"] == "Alpha"
    assert final["completed"] is True
    assert final["history"] is None


def test_opening_variation_refreshes_forecast_for_selected_pick_order(tmp_path):
    class Forecaster:
        def forecast(self, *, first, current, **_):
            assert current == []
            return {
                "as_of": "2026-11-22", "method": "fixture",
                "choices": [{
                    "champion": "Poppy" if first == "prospective:alpha" else "Vi",
                    "probability": 0.5, "evidence": [first],
                }],
            }

    with TestClient(create_app(database=tmp_path / "forecast.sqlite3", forecaster=Forecaster())) as board:
        url, root = matchup(board)
        assert state(board, url, root)["suggestions"][0]["champion"] == "Poppy"
        alternate = choose(board, url, root, first="Beta", blue="Beta")
        refreshed = state(board, url, alternate)
        assert refreshed["actions"] == []
        assert refreshed["suggestions"][0]["champion"] == "Vi"
        assert state(board, url, root)["suggestions"][0]["champion"] == "Poppy"


def test_disables_are_branch_local_inherited_and_replaceable(board):
    url, root = matchup(board)
    restricted = choose(board, url, root, first="Alpha", blue="Beta", disabled=["poppy"])
    assert "Poppy" in state(board, url, root)["legal"]
    assert "Poppy" not in state(board, url, restricted)["legal"]
    assert board.post(f"{url}/actions", json={"parent_id": restricted, "champion": "Poppy"}).status_code == 422
    assert board.post(f"{url}/selection", json={
        "parent_id": restricted, "first": "Alpha", "blue": "Beta", "disabled": ["Not a champion"],
    }).status_code == 422

    edited = choose(board, url, restricted, first="Beta", blue="Beta")
    assert "Poppy" not in state(board, url, edited)["legal"]
    game1 = draft(board, url, edited)
    result1 = winner(board, url, game1["node_id"], "Alpha")
    game2 = choose(board, url, result1, first="Beta", blue="Alpha", choice="first_pick")
    assert "Poppy" not in state(board, url, game2)["legal"]
    cleared = choose(board, url, game2, first="Beta", blue="Alpha", disabled=[])
    assert "Poppy" in state(board, url, cleared)["legal"]
    assert "Poppy" not in state(board, url, game2)["legal"]


def test_standard_picks_reset_at_next_game_and_remain_eligible_for_pins(tmp_path):
    class FearlessOnlyModel:
        def forecast(self, **_):
            raise AssertionError("The Fearless model should be guarded for Standard drafts.")

    with TestClient(create_app(database=tmp_path / "standard.sqlite3", forecaster=FearlessOnlyModel())) as board:
        url, root = matchup(board, draft_mode="standard")
        assert board.get(url).json()["draft_mode"] == "standard"
        opening = state(board, url, root)
        assert opening["draft_mode"] == "standard"
        assert opening["forecast"] is None
        repeated = opening["legal"][0]
        submit(board, f"{url}/pins", champion=repeated, game=2, player="Top")
        game1 = draft(board, url, root)
        assert repeated in [action["champion"] for action in game1["actions"] if action["kind"] == "pick"]
        assert game1["pins"][0]["status"] == "protected"
        result1 = winner(board, url, game1["node_id"], "Alpha")
        game2 = choose(board, url, result1, first="Alpha", blue="Beta", choice="map_side")
        opened = state(board, url, game2)
        assert opened["locked"] == []
        assert repeated in opened["legal"]
        assert opened["pins"][0]["status"] == "protected"
        assert "locked" not in opened["pins"][0]["reason"]


def test_bo1_is_only_complete_after_recording_the_winner(board):
    url, root = matchup(board, format="Bo1")
    game = draft(board, url, root)
    assert game["series_status"] == "awaiting_result"
    assert game["can_rehearse_next"] is False
    assert game["completed"] is False
    result = state(board, url, winner(board, url, game["node_id"], "Alpha"))
    assert result["score"] == {"Alpha": 1, "Beta": 0}
    assert result["series_winner"] == "Alpha"
    assert result["completed"] is True


def test_standard_frequency_ranking_uses_only_prior_standard_games(tmp_path):
    games = tmp_path / "games.jsonl"
    games.write_text("\n".join(
        json.dumps({
            "game_id": champion, "date": day, "fearless": fearless,
            "actions": [{"champion": champion, "kind": "ban"}],
        })
        for champion, day, fearless in (
            ("Poppy", "2026-11-22", False), ("Vi", "2026-11-22", True), ("Maokai", "2026-11-23", False),
        )
    ), encoding="utf8")
    with TestClient(create_app(database=tmp_path / "standard.sqlite3", games=games)) as board:
        url, root = matchup(board, draft_mode="standard")
        opening = state(board, url, root)
        assert opening["source"].startswith("Standard draft frequency")
        assert opening["forecast"]["sample"] == 1
        assert [(row["champion"], row["score"]) for row in opening["suggestions"]] == [("Poppy", None)]


def test_recorded_standard_bo1_has_first_pick_separate_from_map_side(tmp_path):
    pool = [f"Champion {i}" for i in range(20)]
    games, champions = tmp_path / "games.jsonl", tmp_path / "champions.json"
    champions.write_text(json.dumps({"16.17": pool}), encoding="utf8")
    games.write_text(json.dumps({
        "series_id": "3000378", "game_number": 1, "best_of": 1, "date": "2026-11-23", "patch": "16.17",
        "first": "beta", "second": "alpha", "team_names": {"alpha": "Alpha", "beta": "Beta"},
        "sides": {"alpha": "blue", "beta": "red"}, "winner": "alpha", "fearless": False,
        "actions": [
            {"team": team, "kind": kind, "champion": champion}
            for (team, kind), champion in zip(schedule("beta", "alpha"), pool)
        ],
    }), encoding="utf8")
    catalog = MatchCatalog(games, champions)
    plan = create_catalog_plan(catalog, {"match_id": "3000378"})
    opening = node_state(plan, plan["root_id"])
    assert (plan["draft_mode"], plan["format"], opening["game"]) == ("standard", "Bo1", 1)
    assert (opening["blue"], opening["first"]) == ("Alpha", "Beta")
    assert opening["game_winner"] is None

    for _ in range(21):
        advance_history(plan, plan["selected_node"])
    result = node_state(plan, plan["selected_node"])
    assert result["series_status"] == "clinched"
    assert result["series_winner"] == "Alpha"
