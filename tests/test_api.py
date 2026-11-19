from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from fearless.api import create_app
from fearless.rules import schedule


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(database=tmp_path / "plans.sqlite3")) as client:
        yield client


def post(client, path, body):
    result = client.post(path, json=body)
    assert result.status_code < 300, result.text
    return result.json()


def create_plan(client, **fields):
    match_id = client.get("/api/bootstrap").json()["default_match"]
    return post(client, "/api/plans", {"match_id": match_id, **fields})


def node(client, plan, node_id=None):
    return client.get(
        f"/api/plans/{plan['id']}/nodes/{node_id or plan['selected_node']}"
    ).json()


def act(client, plan, champion, **fields):
    return post(
        client,
        f"/api/plans/{plan['id']}/actions",
        {"parent_id": plan["selected_node"], "champion": champion, **fields},
    )


def test_offline_scenario_shows_poppy_tradeoff_and_no_probabilities(client):
    plan = create_plan(client)
    state = node(client, plan)

    assert plan["source"] == "illustrative"
    assert state["game"] == 2
    assert len(state["actions"]) == 5
    assert len(state["locked"]) == 10
    assert state["team"] == "Cloud9 Kia"
    assert state["kind"] == "ban"
    assert state["history"] is None
    assert state["suggestions"][0]["champion"] == "Poppy"
    assert state["suggestions"][0]["score"] is None
    assert state["suggestions"][0]["games"] is None

    poppy_ban = next(n for n in plan["nodes"] if n["champion"] == "Poppy" and n["kind"] == "ban")
    vi_ban = next(n for n in plan["nodes"] if n["champion"] == "Vi" and n["kind"] == "ban")
    poppy_pick = next(n for n in plan["nodes"] if n["champion"] == "Poppy" and n["kind"] == "pick")
    assert poppy_ban["parent_id"] == vi_ban["parent_id"] == plan["root_id"]
    assert poppy_pick["parent_id"] == vi_ban["id"]
    assert node(client, plan, poppy_ban["id"])["pins"][0]["status"] == "protected"
    assert node(client, plan, vi_ban["id"])["pins"][0]["status"] == "still_possible"
    assert node(client, plan, poppy_pick["id"])["pins"][0]["status"] == "conflict"


def test_rejects_invalid_actions_and_scopes_nodes_to_their_plan(client):
    plan = create_plan(client)
    state = node(client, plan)
    path = f"/api/plans/{plan['id']}/actions"
    for champion in ("Not a champion", state["locked"][0], state["actions"][0]["champion"]):
        assert client.post(path, json={"parent_id": state["node_id"], "champion": champion}).status_code == 422

    assert client.post(path, json={"parent_id": state["node_id"], "champion": "Poppy", "first": "Other"}).status_code == 422
    draft = act(client, plan, None)
    passed = node(client, draft)
    assert len(passed["actions"]) == 6
    assert passed["actions"][-1]["champion"] is None
    assert passed["legal"] == state["legal"]
    assert client.post(path, json={"parent_id": passed["node_id"], "champion": None}).status_code == 422

    different = create_plan(client)
    assert client.get(f"/api/plans/{different['id']}/nodes/{state['node_id']}").status_code == 404


def test_games_preserve_picks_reset_bans_and_accept_independent_first(client):
    plan = create_plan(client)
    state = node(client, plan)
    earlier_ban = state["actions"][0]["champion"]
    while len(state["actions"]) < 20:
        plan = act(client, plan, state["legal"][0] if state["kind"] == "pick" else None)
        state = node(client, plan)
    assert state["kind"] == "next_game"
    assert state["legal"] == []
    played = {action["champion"] for action in state["actions"] if action["kind"] == "pick"}

    g3 = act(client, plan, None, first="LYON")
    opening = node(client, g3)
    assert opening["game"] == 3
    assert opening["actions"] == []
    assert opening["first"] == "LYON"
    assert opening["team"] == "LYON"
    assert earlier_ban in opening["legal"]
    assert set(opening["locked"]) == set(state["locked"]) | played

    g3 = act(client, g3, opening["legal"][0], first="Cloud9 Kia")
    assert node(client, g3)["first"] == "Cloud9 Kia"


def test_edits_survive_restarts_and_concurrent_forks(tmp_path):
    database = tmp_path / "plans.sqlite3"
    with TestClient(create_app(database=database)) as client:
        plan = create_plan(client, title="Playoff prep", target_game=3)
        state = node(client, plan)
        choices = [champ for champ in state["legal"] if champ not in {"Poppy", "Vi"}][:2]
        with ThreadPoolExecutor(max_workers=2) as executor:
            updated = list(executor.map(lambda champion: act(client, plan, champion), choices))
        added_ids = {result["selected_node"] for result in updated}
        assert len(added_ids) == 2

        post(client, f"/api/plans/{plan['id']}/pins", {
            "champion": "Vi", "game": 4, "player": "Blaber", "backup": "Wukong",
        })
        post(client, f"/api/plans/{plan['id']}/brief", {
            "node_id": updated[0]["selected_node"], "text": "Hold the flex.", "backup": "Wukong",
        })
        browser_cookies = dict(client.cookies)

    with TestClient(create_app(database=database)) as restarted:
        restarted.cookies.update(browser_cookies)
        restored = restarted.get(f"/api/plans/{plan['id']}").json()
        assert "Playoff prep" == restarted.get("/api/plans").json()[0]["title"]
        assert added_ids <= {item["id"] for item in restored["nodes"]}
        assert any(pin["champion"] == "Vi" and pin["backup"] == "Wukong" for pin in restored["pins"])
        assert restored["brief"] == [{
            "node_id": updated[0]["selected_node"], "text": "Hold the flex.", "backup": "Wukong",
        }]


def test_historical_replay_uses_matching_prefix_and_prior_evidence(tmp_path):
    pool = [f"Champion {n}" for n in range(70)]
    games_path = tmp_path / "normal.jsonl"
    games = []
    for number, first, second in ((1, "alpha", "beta"), (2, "beta", "alpha"), (3, "beta", "alpha")):
        games.append({
            "series_id": "3000378",
            "game_number": number,
            "first": first,
            "second": second,
            "team_names": {"alpha": "Alpha", "beta": "Beta"},
            "date": "2026-10-03",
            "patch": "16.17",
            "region": "NA",
            "format": "Bo3",
            "eligible": True,
            "fearless": True,
            "rosters": {"alpha": [{
                "player": "stable-player", "player_name": "Actual player",
                "champion": pool[30], "role": "jungle",
            }]},
            "actions": [
                {"team": team, "kind": kind, "champion": pool[(number - 1) * 20 + index]}
                for index, (team, kind) in enumerate(schedule(first, second))
            ],
        })
    games_path.write_text("\n".join(json.dumps(row) for row in games), encoding="utf8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"16.17": pool}), encoding="utf8")

    class Forecaster:
        def forecast(self, **kwargs):
            assert kwargs["date"] == "2026-10-03"
            assert kwargs["patch"] == "16.17"
            legal = set(kwargs["pool"]) - set(kwargs["locked"]) - {
                action["champion"] for action in kwargs["current"]
            }
            assert kwargs["rosters"]["alpha"] == ["stable-player"]
            return {
                "as_of": "2026-10-01", "method": "baseline", "choices": [
                    {"champion": sorted(legal)[-1], "probability": 0.18, "games": 2,
                     "roles": ["jungle"], "evidence": ["Prior professional drafts"]},
                ],
            }

    with TestClient(create_app(
        database=tmp_path / "plans.sqlite3", games=games_path, catalog=catalog, forecaster=Forecaster(),
    )) as client:
        match = next(item for item in client.get("/api/bootstrap").json()["matches"] if item["source"] == "historical")
        plan = post(client, "/api/plans", {"match_id": match["id"], "rosters": {"Alpha": ["Actual player"]}})
        state = node(client, plan)
        assert state["first"] == "Beta"
        assert state["kind"] == "ban"
        assert state["actions"] == []
        assert set(state["locked"]) == {action["champion"] for action in games[0]["actions"] if action["kind"] == "pick"}
        assert state["history"]["champion"] == pool[20]
        assert state["history"]["kind"] == "ban"
        assert state["suggestions"][0]["reason"] == "Prior professional drafts"
        assert state["suggestions"][0]["games"] == 2
        assert state["suggestions"][0]["roles"] == ["jungle"]
        assert state["suggestions"][0]["score"] is None
        assert "baseline" in state["source"].lower()

        route = f"/api/plans/{plan['id']}/advance-history"
        alternative = act(client, plan, pool[69])
        assert node(client, alternative)["history"] is None
        assert client.post(route, json={"parent_id": alternative["selected_node"]}).status_code == 409

        replay = plan
        for _ in range(20):
            replay = post(client, route, {"parent_id": replay["selected_node"]})
        assert node(client, replay)["kind"] == "next_game"
        replay = post(client, route, {"parent_id": replay["selected_node"]})
        assert node(client, replay)["first"] == "Beta"
        assert node(client, replay)["history"]["champion"] == pool[40]
        for _ in range(20):
            replay = post(client, route, {"parent_id": replay["selected_node"]})
        assert node(client, replay)["completed"] is True
        assert client.post(route, json={"parent_id": replay["selected_node"]}).status_code == 409


def test_static_server_serves_only_built_assets(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "assets").mkdir()
    (web / "index.html").write_text("<main>Rehearsal</main>", encoding="utf8")
    (web / "assets" / "bundle.js").write_text("console.log('ready')", encoding="utf8")
    (web / "private.jsonl").write_text("private", encoding="utf8")
    with TestClient(create_app(database=tmp_path / "plans.sqlite3", web=web)) as client:
        assert "Rehearsal" in client.get("/").text
        assert client.get("/assets/bundle.js").status_code == 200
        assert client.get("/private.jsonl").status_code == 404
        assert client.get("/api/unknown").status_code == 404


def test_frequency_evidence_deduplicates_games_and_excludes_match_day(tmp_path):
    from fearless.app_forecast import HistoricalFrequencies

    def game(name, day, champion, **extra):
        return {
            "game_id": name, "date": day, "region": "NA", "eligible": True, "finished": True,
            "actions": [{"kind": "pick", "champion": champion}], **extra,
        }

    older = game("past", "2026-08-01", "Poppy")
    evidence = HistoricalFrequencies([
        older, older,
        game("same-day", "2026-08-02", "Vi"),
        game("unfinished", "2026-08-01", "Vi", finished_at="2026-08-02T00:10:00+00:00"),
        game("future", "2026-08-03", "Sejuani"),
    ])
    result = evidence.estimate(as_of="2026-08-02", region="NA", kind="pick", legal=["Poppy", "Vi", "Sejuani"])
    assert result["sample"] == 1
    assert result["as_of"] == "2026-08-01"
    assert [(choice["champion"], choice["games"], choice["score"]) for choice in result["choices"]] == [
        ("Poppy", 1, None)
    ]


def test_plan_access_is_bound_to_an_opaque_browser_session(tmp_path):
    app = create_app(database=tmp_path / "plans.sqlite3")
    with TestClient(app) as coach, TestClient(app) as visitor:
        config = coach.get("/api/bootstrap").json()
        response = coach.post("/api/plans", json={"match_id": config["default_match"]})
        assert response.status_code == 201
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        plan = response.json()
        route = f"/api/plans/{plan['id']}"
        assert coach.get(route).status_code == 200
        assert visitor.get("/api/plans").json() == []
        assert visitor.get(route).status_code == 404
        assert visitor.get(f"{route}/nodes/{plan['root_id']}").status_code == 404
        assert visitor.post(f"{route}/actions", json={
            "parent_id": plan["root_id"], "champion": "Poppy",
        }).status_code == 404


def test_pre_session_database_is_preserved_and_hidden(tmp_path):
    import sqlite3

    database = tmp_path / "plans.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE plans (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
        db.execute("INSERT INTO plans VALUES (?, ?)", ("legacy", '{"private":"notes"}'))

    with TestClient(create_app(database=database)) as client:
        assert client.get("/api/plans").json() == []
        assert client.get("/api/plans/legacy").status_code == 404
        assert create_plan(client)["id"] != "legacy"

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT snapshot, owner FROM plans WHERE id = 'legacy'").fetchone() == (
            '{"private":"notes"}', None,
        )
