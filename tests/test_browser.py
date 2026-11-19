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
