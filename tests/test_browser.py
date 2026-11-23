import json

from fearless.app_catalog import MatchCatalog
from fearless.browser import BrowserRehearsal


def test_browser_adapter_uses_same_forks_and_saved_briefs():
    app = BrowserRehearsal(MatchCatalog(None, None))
    stored = "[]"

    def request(path, body=None):
        nonlocal stored
        result = json.loads(app.request(path, json.dumps(body) if body is not None else None, stored))
        if result.get("plans"):
            stored = json.dumps(result["plans"])
        return result

    match = request("/bootstrap")["data"]["default_match"]
    plan = request("/plans", {"match_id": match})["data"]
    base = f"/plans/{plan['id']}"
    root = request(f"{base}/nodes/{plan['root_id']}")["data"]
    assert root["team"] == "Cloud9 Kia"
    assert root["forecast"] is None
    assert root["outlook"] is None

    protected = request(f"{base}/actions", {"parent_id": plan["root_id"], "champion": "Poppy"})["data"]
    active = request(f"{base}/nodes/{protected['selected_node']}")["data"]
    assert active["pins"][0]["status"] == "protected"

    request(f"{base}/brief", {
        "node_id": plan["root_id"], "text": "Keep Poppy free for Blaber.", "backup": "Maokai",
    })
    reopened = BrowserRehearsal(MatchCatalog(None, None))
    saved = json.loads(reopened.request(base, None, stored))["data"]
    assert saved["brief"][0]["text"] == "Keep Poppy free for Blaber."
    assert request(f"{base}/actions", {"parent_id": plan["root_id"], "champion": "not-a-champion"})["status"] == 422
    assert len(request("/plans")["data"]) == 1


def test_browser_reopens_selection_result_and_legacy_fixture():
    catalog = MatchCatalog(None, None)
    app = BrowserRehearsal(catalog)
    stored = []

    def request(path, body=None):
        nonlocal stored
        encoded = json.dumps(body) if body is not None else None
        response = json.loads(app.request(path, encoded, json.dumps(stored)))
        assert "status" not in response, response
        if "plans" in response:
            stored = response["plans"]
        return response["data"]

    demos = request("/bootstrap")
    legacy = request("/plans", {"match_id": demos["default_match"]})
    del stored[0]["prior_winners"]
    del stored[0]["draft_mode"]
    del stored[0]["nodes"][0]["blue"]
    old_opening = request(f"/plans/{legacy['id']}/nodes/{legacy['root_id']}")
    assert old_opening["draft_mode"] == "fearless"
    assert old_opening["blue"] is None
    assert old_opening["red"] is None
    assert old_opening["score_complete"] is False
    assert old_opening["progression"] == "hypothetical"

    plan = request("/plans", {
        "match_id": "prospective", "teams": ["Alpha", "Beta"], "coach_team": "Alpha",
        "format": "Bo3", "patch": "16.17", "date": "2026-11-23", "first": "Alpha",
    })
    url = f"/plans/{plan['id']}"
    selection = request(f"{url}/selection", {"parent_id": plan["root_id"], "first": "Beta", "blue": "Alpha"})
    active = request(f"{url}/nodes/{selection['selected_node']}")
    assert (active["first"], active["blue"], active["team"]) == ("Beta", "Alpha", "Beta")
    while active["kind"] in {"pick", "ban"}:
        drafted = request(f"{url}/actions", {
            "parent_id": active["node_id"], "champion": active["legal"][0] if active["kind"] == "pick" else None,
        })
        active = request(f"{url}/nodes/{drafted['selected_node']}")
    saved = request(f"{url}/result", {"parent_id": active["node_id"], "winner": "Alpha"})
    reopened = BrowserRehearsal(catalog)
    persisted = json.loads(reopened.request(f"{url}/nodes/{saved['selected_node']}", None, json.dumps(stored)))["data"]
    assert persisted["game_winner"] == "Alpha"
    assert persisted["score"] == {"Alpha": 1, "Beta": 0}
    assert persisted["selection_right"] == "Beta"
