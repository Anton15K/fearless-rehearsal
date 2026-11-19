import json

from fastapi.testclient import TestClient
import pytest

from fearless.api import create_app


POOL = ["Ahri", "Braum", "Poppy", "Vi"]
MATCHUP = {
    "match_id": "prospective",
    "teams": ["Northstar", "Open Circuit"],
    "coach_team": "Open Circuit",
    "first": "Northstar",
    "date": "2026-01-10",
    "format": "Bo3",
    "patch": "16.2",
}


class RecordingForecaster:
    def __init__(self):
        self.requests = []

    def forecast(self, **kwargs):
        self.requests.append(kwargs)
        return {}


@pytest.fixture
def app_and_forecaster(tmp_path):
    games = tmp_path / "games.jsonl"
    earlier = {
        "series_id": "one", "game_number": 1, "date": "2026-01-09", "region": "NA",
        "team_names": {"north-id": "Northstar", "other-id": "Other team"},
        "rosters": {
            "north-id": [{"player": "existing-player", "player_name": "Known"}],
            "other-id": [
                {"player": "transfer-id", "player_name": "Transfer"},
                {"player": "duplicate-one", "player_name": "Shared"},
            ],
        },
        "actions": [{"team": "north-id", "kind": "ban", "champion": "Vi"}],
    }
    older = {
        "series_id": "older", "game_number": 1, "date": "2026-01-08", "region": "NA",
        "team_names": {"another-id": "Another team"},
        "rosters": {"another-id": [{"player": "duplicate-two", "player_name": "Shared"}]},
        "actions": [],
    }
    same_day = {
        "series_id": "two", "game_number": 1, "date": "2026-01-10", "region": "NA",
        "team_names": {"new-id": "Open Circuit", "north-id": "Northstar"},
        "rosters": {"north-id": [{"player": "future-player", "player_name": "Future"}]},
        "actions": [{"team": "new-id", "kind": "ban", "champion": "Ahri"}],
    }
    games.write_text("\n".join(json.dumps(game) for game in (earlier, older, same_day)), encoding="utf-8")
    catalog = tmp_path / "champions.json"
    catalog.write_text(json.dumps({"16.2": POOL, "16.9": POOL, "16.17": POOL}), encoding="utf-8")
    recorder = RecordingForecaster()
    app = create_app(database=tmp_path / "plans.sqlite3", games=games, catalog=catalog, forecaster=recorder)
    return app, recorder


def test_prospective_starts_clean_and_excludes_same_day_team_and_player_evidence(app_and_forecaster):
    app, recorder = app_and_forecaster
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").json()["patches"] == ["16.17", "16.9", "16.2"]
        result = client.post("/api/plans", json={
            **MATCHUP,
            "rosters": {"Northstar": ["known", "tRaNsFeR", "Shared", "Future"], "Open Circuit": ["Unpublished"]},
        })
        assert result.status_code == 201, result.text
        plan = result.json()
        assert plan["source"] == "prospective"
        assert plan["coach_team"] == "Open Circuit"
        assert plan["format"] == "Bo3"
        assert plan["team_history"] == {"Northstar": {"as_of": "2026-01-09"}, "Open Circuit": None}
        assert len(plan["nodes"]) == 1
        assert plan["pins"] == []

        state_path = f"/api/plans/{plan['id']}/nodes/{plan['root_id']}"
        state = client.get(state_path).json()
        assert state["game"] == 1
        assert state["team"] == state["first"] == "Northstar"
        assert state["actions"] == state["locked"] == []
        assert state["legal"] == POOL
        assert state["history"] is None
        assert state["suggestions"][0]["champion"] == "Vi"

        forecast = recorder.requests[-1]
        assert forecast["date"] == MATCHUP["date"]
        assert forecast["first"] == "north-id"
        assert forecast["second"] != "new-id"
        assert forecast["rosters"][forecast["first"]] == ["existing-player", "transfer-id"]
        assert forecast["rosters"][forecast["second"]] == []
        assert client.get(f"/api/plans/{plan['id']}").json()["source"] == "prospective"


@pytest.mark.parametrize("changes", [
    {"teams": [" Northstar ", "northSTAR"]},
    {"coach_team": "Other team"},
    {"first": "Other team"},
    {"date": "2026-02-30"},
    {"patch": "99.99"},
    {"format": "Bo7"},
])
def test_prospective_validates_rehearsal_settings(app_and_forecaster, changes):
    app, _ = app_and_forecaster
    with TestClient(app) as client:
        assert client.post("/api/plans", json={**MATCHUP, **changes}).status_code == 422


def test_named_replays_reject_prospective_identity_fields(app_and_forecaster):
    app, _ = app_and_forecaster
    with TestClient(app) as client:
        result = client.post("/api/plans", json={**MATCHUP, "match_id": "poppy-game-three"})
        assert result.status_code == 422


def test_public_browser_and_server_limit_estimates_to_dates_after_validation(tmp_path, monkeypatch):
    from fearless.browser import open_demo

    monkeypatch.delenv("FEARLESS_BUNDLE", raising=False)
    monkeypatch.delenv("FEARLESS_GAMES", raising=False)
    monkeypatch.delenv("FEARLESS_CATALOG", raising=False)
    browser = open_demo()

    def browser_node(payload):
        created = json.loads(browser("/plans", json.dumps(payload), "[]"))
        plan = created["data"]
        state = json.loads(browser(
            f"/plans/{plan['id']}/nodes/{plan['root_id']}", None, json.dumps(created["plans"]),
        ))
        return state["data"]

    with TestClient(create_app(database=tmp_path / "public-plans.sqlite3")) as client:
        for cutoff, quality in (("2026-06-01", "estimated"), ("2026-04-10", None)):
            request = {**MATCHUP, "patch": "16.17", "date": cutoff}
            stored = client.post("/api/plans", json=request)
            assert stored.status_code == 201, stored.text
            plan = stored.json()
            server_state = client.get(f"/api/plans/{plan['id']}/nodes/{plan['root_id']}").json()
            local_state = browser_node(request)
            assert server_state["suggestions"] == local_state["suggestions"]
            for state in (server_state, local_state):
                forecast = state["forecast"]
                assert forecast["as_of"] < cutoff
                assert forecast.get("probability_quality") == quality
                assert bool(state["outlook"]) == bool(quality)
                assert all((row["score"] is not None) == bool(quality) for row in state["suggestions"])


def test_prospective_removes_match_specific_disables_from_every_branch(app_and_forecaster):
    app, _ = app_and_forecaster
    with TestClient(app) as client:
        response = client.post("/api/plans", json={**MATCHUP, "disabled": ["pOpPy"]})
        response.raise_for_status()
        plan = response.json()
        path = f"/api/plans/{plan['id']}/nodes/{plan['selected_node']}"
        assert "Poppy" not in client.get(path).json()["legal"]
        invalid = client.post("/api/plans", json={**MATCHUP, "disabled": ["Unknown Champion"]})
        assert invalid.status_code == 422
