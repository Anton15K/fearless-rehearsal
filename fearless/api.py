"""Same-origin API for a local draft rehearsal workspace."""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from fearless.app_catalog import MatchCatalog
from fearless.app_store import PlanStore
from fearless.rehearsal import (
    RehearsalError,
    advance_history,
    apply_action,
    create_catalog_plan,
    node_state,
    plan_summary,
    public_plan,
    save_brief,
    save_pin,
)


LOGGER = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("assets")
SESSION_COOKIE = "fearless_session"
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Patch = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=30)]
Game = Annotated[int, Field(strict=True, ge=1, le=5)]
Roster = Annotated[list[Name] | dict[Name, Name], Field(min_length=1, max_length=10)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanInput(Payload):
    match_id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    priority: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    target_game: Game | None = None
    player: Name | None = None
    first: Name | None = None
    patch: Patch | None = None
    rosters: Annotated[dict[Name, Roster], Field(max_length=2)] | None = None


class ParentInput(Payload):
    parent_id: Identifier


class ActionInput(ParentInput):
    champion: Name | None
    first: Name | None = None


class PinInput(Payload):
    champion: Name
    game: Game
    player: Name
    backup: Name | None = None
    node_id: Identifier | None = None


class BriefInput(Payload):
    node_id: Identifier
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    backup: Name | None = None


def _path(value: str | Path | None, env: str, fallback: Path | None = None) -> Path | None:
    value = value or os.environ.get(env)
    return Path(value).expanduser() if value else fallback


def _bundled(name: str) -> Path | None:
    path = ASSETS / name
    return path if path.is_file() else None


def _forecaster(bundle: Path | None) -> Any:
    if bundle is None:
        return None
    try:
        from fearless.model import DraftForecaster

        return DraftForecaster.load(bundle)
    except (ImportError, OSError, ValueError, KeyError):
        LOGGER.warning("The configured draft forecast bundle could not be loaded.")
        return None


def _browser_owner(request: Request, response: Response) -> str:
    response.headers["Cache-Control"] = "private, no-store"
    token = request.cookies.get(SESSION_COOKIE, "")
    if SESSION_PATTERN.fullmatch(token) is None:
        token = token_urlsafe(32)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=365 * 24 * 60 * 60,
            secure=request.url.scheme == "https", httponly=True, samesite="strict",
        )
    return token


Owner = Annotated[str, Depends(_browser_owner)]


def create_app(
    *,
    database: str | Path | None = None,
    games: str | Path | None = None,
    catalog: str | Path | None = None,
    web: str | Path | None = None,
    forecaster: Any = None,
) -> FastAPI:
    database_path = _path(database, "FEARLESS_DB", Path(".local/rehearsal.sqlite3"))
    games_path = _path(games, "FEARLESS_GAMES")
    public_games = _bundled("public_games.jsonl")
    catalog_path = _path(catalog, "FEARLESS_CATALOG", _bundled("champions.json"))
    web_path = _path(web, "FEARLESS_WEB", Path("web/dist"))
    store = PlanStore(database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        app.state.matches = MatchCatalog(games_path, catalog_path, public_games)
        bundle_path = _path(None, "FEARLESS_BUNDLE")
        app.state.forecaster = forecaster if forecaster is not None else _forecaster(bundle_path)
        yield

    app = FastAPI(title="Fearless Rehearsal", lifespan=lifespan)

    @app.exception_handler(RehearsalError)
    async def rehearsal_error(_request: Request, error: RehearsalError):
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    @app.get("/api/bootstrap")
    def bootstrap():
        return app.state.matches.bootstrap()

    @app.get("/api/plans")
    def list_plans(owner: Owner):
        return [plan_summary(plan) for plan in store.list(owner)]

    @app.post("/api/plans", status_code=201)
    def create_plan(payload: PlanInput, owner: Owner):
        plan = create_catalog_plan(app.state.matches, payload.model_dump())
        store.create(owner, plan)
        return public_plan(plan)

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: Identifier, owner: Owner):
        return public_plan(store.get(owner, plan_id))

    @app.get("/api/plans/{plan_id}/nodes/{node_id}")
    def get_node(plan_id: Identifier, node_id: Identifier, owner: Owner):
        return node_state(
            store.get(owner, plan_id), node_id, app.state.forecaster, app.state.matches.frequencies,
        )

    @app.post("/api/plans/{plan_id}/actions")
    def add_action(plan_id: Identifier, payload: ActionInput, owner: Owner):
        plan = store.edit(
            owner, plan_id, lambda current: apply_action(current, payload.parent_id, payload.champion, payload.first),
        )
        return public_plan(plan)

    @app.post("/api/plans/{plan_id}/advance-history")
    def replay_action(plan_id: Identifier, payload: ParentInput, owner: Owner):
        plan = store.edit(owner, plan_id, lambda current: advance_history(current, payload.parent_id))
        return public_plan(plan)

    @app.post("/api/plans/{plan_id}/pins")
    def pin(plan_id: Identifier, payload: PinInput, owner: Owner):
        return public_plan(store.edit(owner, plan_id, lambda current: save_pin(current, payload.model_dump())))

    @app.post("/api/plans/{plan_id}/brief")
    def brief(plan_id: Identifier, payload: BriefInput, owner: Owner):
        return public_plan(store.edit(owner, plan_id, lambda current: save_brief(current, payload.model_dump())))

    if web_path is not None and (web_path / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(web_path / "index.html")

        assets = web_path / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="built-assets")

    return app


app = create_app()
