"""Transport adapter for browser-local rehearsals using only public data."""

import json
from pathlib import Path

from fearless.app_catalog import MatchCatalog
from fearless.app_error import RehearsalError
from fearless.rehearsal import (
    advance_history,
    apply_action,
    create_catalog_plan,
    node_state,
    plan_summary,
    public_plan,
    save_brief,
    save_pin,
)


class BrowserRehearsal:
    def __init__(self, catalog: MatchCatalog, public_forecasts: dict | None = None):
        self.catalog = catalog
        self.public_forecasts = public_forecasts or {}

    def request(self, path: str, body: str | None, stored: str) -> str:
        """Execute one request over the saved snapshots supplied by the IndexedDB transaction."""
        data = json.loads(body) if body is not None else None
        plans = json.loads(stored)
        try:
            response = self._dispatch(path, data, plans)
        except RehearsalError as error:
            response = {"status": error.status, "detail": error.detail}
        return json.dumps(response, ensure_ascii=False, allow_nan=False)

    def _dispatch(self, path: str, data: dict | None, plans: list[dict]) -> dict:
        if path == "/bootstrap" and data is None:
            return {"data": self.catalog.bootstrap()}
        if path == "/plans":
            if data is None:
                return {"data": [plan_summary(plan) for plan in plans]}
            plan = create_catalog_plan(self.catalog, data)
            return {"data": public_plan(plan), "plans": [plan, *plans]}

        parts = path.removeprefix("/").split("/")
        if len(parts) < 2 or parts[0] != "plans":
            raise RehearsalError(404, "Rehearsal endpoint not found.")
        _, plan_id, *endpoint = parts
        plan = next((plan for plan in plans if plan["id"] == plan_id), None)
        if plan is None:
            raise RehearsalError(404, "Rehearsal plan not found.")
        if data is None:
            if not endpoint:
                return {"data": public_plan(plan)}
            if len(endpoint) == 2 and endpoint[0] == "nodes":
                forecaster = self.public_forecasts.get(plan["match_id"])
                return {"data": node_state(plan, endpoint[1], forecaster, self.catalog.frequencies)}
            raise RehearsalError(404, "Rehearsal endpoint not found.")

        match endpoint:
            case ["actions"]:
                apply_action(plan, data["parent_id"], data["champion"], data.get("first"))
            case ["advance-history"]:
                advance_history(plan, data["parent_id"])
            case ["pins"]:
                save_pin(plan, {"backup": None, **data})
            case ["brief"]:
                save_brief(plan, {"backup": None, **data})
            case _:
                raise RehearsalError(404, "Rehearsal endpoint not found.")
        return {"data": public_plan(plan), "plans": plans}


def open_demo():
    assets = Path(__file__).with_name("assets")
    catalog = MatchCatalog(None, assets / "champions.json", assets / "public_games.jsonl")
    forecasts = {}
    parameters = assets / "public_forecast.json"
    if parameters.is_file():
        from fearless.public_model import SHOWCASE_SERIES, PublicForecaster

        forecasts[SHOWCASE_SERIES] = PublicForecaster.load(parameters)
    return BrowserRehearsal(catalog, forecasts).request
