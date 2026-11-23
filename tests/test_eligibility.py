import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from fearless.api import create_app
from fearless.app_catalog import DEMO_POOL, MatchCatalog
from fearless.app_error import RehearsalError
from fearless.browser import open_demo
from fearless.rehearsal import apply_action, create_catalog_plan, node_state, public_plan, save_pin, validate_game_disabled
from fearless.rules import schedule


PROSPECTIVE = {
    "match_id": "prospective",
    "teams": ["Home", "Away"],
    "coach_team": "Away",
    "first": "Home",
    "format": "Bo3",
    "date": "2026-07-03",
    "patch": "16.13",
}


class Recorder:
    def __init__(self):
        self.context = None

    def forecast(self, **kwargs):
        self.context = kwargs
        return {}


def test_api_restricts_lec_release_patch_and_keeps_manual_disables_separate(tmp_path):
    catalog = tmp_path / "champions.json"
    catalog.write_text(json.dumps({"16.13": list(DEMO_POOL), "16.14": list(DEMO_POOL)}))
    forecaster = Recorder()
    with TestClient(create_app(database=tmp_path / "plans.sqlite3", catalog=catalog, forecaster=forecaster)) as client:
        listed = {item["id"]: item for item in client.get("/api/bootstrap").json()["rulesets"]}
        assert listed["lec-2026"]["source"].startswith("https://")
        response = client.post("/api/plans", json={**PROSPECTIVE, "ruleset": "lec-2026", "disabled": ["poppy"]})
        assert response.status_code == 201, response.text
        plan = response.json()
        assert plan["disabled"] == ["Locke", "Poppy"]
        restrictions = {row["champion"]: row for row in plan["restrictions"]}
        assert restrictions["Locke"]["reason"] == "release_patch"
        assert restrictions["Locke"]["source"] == listed["lec-2026"]["source"]
        assert restrictions["Poppy"] == {"champion": "Poppy", "reason": "manual", "source": None}
        assert plan["client_pool_size"] - plan["effective_pool_size"] == 2
        prefix = f"/api/plans/{plan['id']}"
        state = client.get(f"{prefix}/nodes/{plan['root_id']}").json()
        assert state["ruleset"] == "lec-2026"
        assert {"Locke", "Poppy"}.isdisjoint(state["legal"])
        assert {"Locke", "Poppy"}.isdisjoint(forecaster.context["pool"])
        assert client.post(f"{prefix}/actions", json={"parent_id": plan["root_id"], "champion": "Locke"}).status_code == 422
        for changes in ({"patch": "16.14", "ruleset": "lec-2026"}, {"ruleset": "manual"}):
            response = client.post("/api/plans", json={**PROSPECTIVE, **changes})
            assert response.status_code == 201
            assert "Locke" not in response.json()["disabled"]
        automatic = client.post("/api/plans", json={**PROSPECTIVE, "event": "LEC - Summer 2026"}).json()
        assert automatic["ruleset"] == "lec-2026" and automatic["disabled"] == ["Locke"]
        playoffs = client.post("/api/plans", json={**PROSPECTIVE, "event": "LEC Summer 2026 Playoffs"}).json()
        assert playoffs["ruleset"] == "lec-2026" and playoffs["disabled"] == ["Locke"]
        unrelated = client.post("/api/plans", json={**PROSPECTIVE, "event": "Example LEC training cup"}).json()
        assert unrelated["ruleset"] == "manual" and unrelated["disabled"] == []


def test_standalone_uses_the_same_restrictions_and_rejects_unknown_rulesets():
    request = open_demo()
    bootstrap = json.loads(request("/bootstrap", None, "[]"))["data"]
    assert "lec-2026" in {item["id"] for item in bootstrap["rulesets"]}
    good = json.loads(request("/plans", json.dumps({**PROSPECTIVE, "ruleset": "lec-2026"}), "[]"))
    plan = good["data"]
    state = json.loads(request(f"/plans/{plan['id']}/nodes/{plan['root_id']}", None, json.dumps(good["plans"])))["data"]
    assert state["disabled"] == ["Locke"]
    bad = json.loads(request("/plans", json.dumps({**PROSPECTIVE, "ruleset": "custom"}), "[]"))
    assert bad["status"] == 422


def test_historical_pools_apply_to_each_game_and_retain_verified_match_metadata(tmp_path):
    assets = Path(__file__).parents[1] / "fearless" / "assets"
    games = tmp_path / "history.jsonl"
    rows = []
    for game in range(1, 4):
        first, second = ("a", "b") if game != 2 else ("b", "a")
        rows.append({
            "series_id": "3000378", "game_number": game, "team_names": {"a": "Home", "b": "Away"},
            "date": "2026-06-27", "patch": "16.13", "region": "EMEA", "event": "Example Cup",
            "source": "https://organizer.example/schedule", "format": "Bo5", "first": first, "second": second,
            "sides": {"a": "red", "b": "blue"}, "winner": "a" if game != 2 else "b",
            "disabled": ["Poppy"] if game == 2 else ["Maokai"] if game == 3 else [],
            "actions": [{"team": team, "kind": kind, "champion": champion}
                        for (team, kind), champion in zip(schedule(first, second),
                                                          DEMO_POOL[(game - 1) * 20:game * 20], strict=True)],
        })
    games.write_text("".join(json.dumps(row) + "\n" for row in rows))
    catalog = MatchCatalog(games, assets / "champions.json")
    match = catalog.matches["3000378"]
    assert match.metadata["blue"] == "Away"
    assert match.metadata["prior_winners"] == ["Home"]
    assert match.historical[2]["winner"] == "Away"
    assert match.historical[3]["sides"] == {"a": "red", "b": "blue"}

    plan = create_catalog_plan(catalog, {"match_id": "3000378"})
    root = plan["root_id"]
    game_two = node_state(plan, root)
    assert game_two["disabled"] == ["Poppy"] and "Maokai" in game_two["legal"]
    assert game_two["restrictions"] == [{"champion": "Poppy", "reason": "recorded",
                                          "source": "https://organizer.example/schedule"}]
    for row in match.historical[2]["actions"]:
        apply_action(plan, plan["selected_node"], row["champion"])
    apply_action(plan, plan["selected_node"], None)
    game_three_id = plan["selected_node"]
    forecaster = Recorder()
    game_three = node_state(plan, game_three_id, forecaster)
    assert game_three["disabled"] == ["Maokai"] and "Poppy" in game_three["legal"]
    assert "Maokai" not in forecaster.context["pool"]
    assert public_plan(plan)["disabled"] == ["Maokai"]
    with pytest.raises(RehearsalError):
        apply_action(plan, game_three_id, "Maokai")
    with pytest.raises(RehearsalError):
        save_pin(plan, {"champion": "Maokai", "backup": None, "game": 3, "player": "Player"})
    assert node_state(plan, root)["disabled"] == ["Poppy"]


def test_game_overrides_follow_branch_ancestry_and_cannot_clear_tournament_rules():
    assets = Path(__file__).parents[1] / "fearless" / "assets"
    catalog = MatchCatalog(None, assets / "champions.json")
    plan = create_catalog_plan(catalog, {**PROSPECTIVE, "event": "LEC", "disabled": ["Poppy"]})
    root = plan["root_id"]
    plan["nodes"][0]["game_disabled"] = validate_game_disabled(plan, 1, ["shyvana"])
    state = node_state(plan, root)
    assert state["game_disabled"] == ["Shyvana"]
    assert {"Shyvana", "Poppy", "Locke"}.isdisjoint(state["legal"])
    with pytest.raises(RehearsalError):
        validate_game_disabled(plan, 1, ["non-champion"])
    lineup = state["legal"][:20]
    for champion in lineup:
        apply_action(plan, plan["selected_node"], champion)
    complete = plan["selected_node"]
    apply_action(plan, complete, None, first="Away")
    inherited = plan["selected_node"]
    assert node_state(plan, inherited)["game_disabled"] == ["Shyvana"]
    apply_action(plan, complete, None, first="Home")
    cleared = plan["selected_node"]
    plan["nodes"][-1]["game_disabled"] = validate_game_disabled(plan, 2, [])
    forecaster = Recorder()
    state = node_state(plan, cleared, forecaster)
    assert state["game_disabled"] == []
    assert state["disabled"] == ["Locke", "Poppy"]
    assert "Shyvana" in state["legal"] and "Shyvana" in forecaster.context["pool"]
    assert node_state(plan, inherited)["game_disabled"] == ["Shyvana"]
