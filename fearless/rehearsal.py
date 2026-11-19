"""Build draft states and apply changes to a saved rehearsal tree."""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fearless.app_catalog import Match, MatchCatalog, PROSPECTIVE_ID
from fearless.app_error import RehearsalError
from fearless.rules import (
    DRAFT_LENGTH,
    DraftAction,
    legal_champions,
    pin_status,
    schedule,
    series_locks,
    validate_action,
)


def create_catalog_plan(catalog: MatchCatalog, settings: dict) -> dict:
    if settings.get("match_id") == PROSPECTIVE_ID:
        match = catalog.prospective(settings)
    else:
        if any(settings.get(field) is not None for field in ("teams", "coach_team", "date", "format")):
            raise RehearsalError(422, "Choose New matchup to set the teams and series details.")
        match = catalog.matches.get(settings.get("match_id"))
        if match is None:
            raise RehearsalError(404, "Match not found.")
    patch = settings.get("patch") or match.metadata["patch"]
    pool = catalog.pool(match, patch)
    if pool is None:
        raise RehearsalError(422, "The champion catalog does not include that patch.")
    return new_plan(match, pool, settings)


def new_plan(match: Match, pool: tuple[str, ...], settings: dict) -> dict:
    meta = match.metadata
    patch = settings.get("patch") or meta["patch"]
    first = settings.get("first") or meta["first"]
    _check_team(meta["teams"], first)
    max_game = int(meta["format"][-1])
    target_game = settings.get("target_game") or min(3, max_game)
    if target_game > max_game:
        raise RehearsalError(422, f"Choose a priority game within this {meta['format']} series.")

    rosters = {**meta["rosters"], **(settings.get("rosters") or {})}
    if set(rosters) - set(meta["teams"]):
        raise RehearsalError(422, "Roster names must use a team in this match.")

    seeded = (
        meta["source"] == "illustrative"
        and patch == meta["patch"]
        and first == meta["first"]
    )
    root = uuid4().hex
    coach = match.coach_team
    plan = {
        "id": uuid4().hex,
        "match_id": meta["id"],
        "title": settings.get("title") or meta["title"],
        "teams": meta["teams"],
        "patch": patch,
        "date": meta["date"],
        "format": meta["format"],
        "source": meta["source"],
        "provenance": meta.get("provenance"),
        "team_history": meta.get("team_history"),
        "roster_dates": meta.get("roster_dates"),
        "root_id": root,
        "selected_node": root,
        "nodes": [{
            "id": root, "parent_id": None, "game": len(match.previous) + 1,
            "team": None, "kind": "start", "champion": None,
            "origin": "rehearsal", "first": first,
        }],
        "pins": [],
        "brief": [],
        "priority": settings.get("priority") or ("Preserve Poppy for Game 3" if seeded else None),
        "target_game": target_game,
        "player": settings.get("player") or ("Blaber" if seeded else None),
        "rosters": rosters,
        "roster_overrides": list(settings.get("rosters") or {}),
        "coach_team": coach,
        "region": match.region,
        "team_ids": match.team_ids,
        "player_ids": match.player_ids,
        "pool": list(pool),
        "prior": match.previous,
        "opening": match.opening if seeded else [],
        "replay": {str(game): row for game, row in match.historical.items()} if patch == meta["patch"] else {},
    }
    if seeded:
        apply_action(plan, root, "Poppy")
        apply_action(plan, root, "Vi")
        apply_action(plan, plan["selected_node"], "Poppy")
        plan["selected_node"] = root
        if target_game == 3:
            plan["pins"].append({
                "champion": "Poppy", "game": 3, "player": plan["player"], "team": coach, "backup": "Maokai",
            })
    return plan


def _find_node(plan: dict, node_id: str) -> dict:
    for node in plan["nodes"]:
        if node["id"] == node_id:
            return node
    raise RehearsalError(404, "Draft position not found in this plan.")


def _check_team(teams: list[str], team: str) -> None:
    if team not in teams:
        raise RehearsalError(422, "First pick must belong to a team in this match.")


def _action(node: dict) -> DraftAction:
    return {"team": node["team"], "kind": node["kind"], "champion": node["champion"]}


@dataclass(frozen=True)
class Position:
    node: dict
    first: str
    second: str
    games: list[list[DraftAction]]
    actions: list[dict]
    locked: set[str]
    team: str | None
    kind: str

    @property
    def game(self) -> int:
        return self.node["game"]

    @property
    def current(self) -> list[DraftAction]:
        return self.games[-1]


def position_at(plan: dict, node_id: str) -> Position:
    nodes = {node["id"]: node for node in plan["nodes"]}
    try:
        selected = nodes[node_id]
    except KeyError as error:
        raise RehearsalError(404, "Draft position not found in this plan.") from error

    path = []
    cursor = selected
    while cursor:
        path.append(cursor)
        cursor = nodes.get(cursor["parent_id"])

    previous = list(plan["prior"])
    actions = [{**action, "origin": "rehearsal"} for action in plan["opening"]]
    for node in reversed(path):
        if node["kind"] in ("pick", "ban"):
            actions.append({**_action(node), "origin": node["origin"]})
        elif node["parent_id"] is not None:
            previous.append([_action(action) for action in actions])
            actions = []

    first = selected["first"]
    second = next(team for team in plan["teams"] if team != first)
    if len(actions) < DRAFT_LENGTH:
        team, kind = schedule(first, second)[len(actions)]
    else:
        team = None
        kind = "complete" if selected["game"] >= int(plan["format"][-1]) else "next_game"
    return Position(
        selected, first, second, [*previous, [_action(action) for action in actions]],
        actions, series_locks(previous), team, kind,
    )


def history_at(plan: dict, pos: Position) -> dict | None:
    replay = plan["replay"]
    recorded = replay.get(str(pos.game))
    if not recorded or pos.first != recorded["first"]:
        return None
    for game, actions in enumerate(pos.games[len(plan["prior"]):], start=len(plan["prior"]) + 1):
        actual = replay.get(str(game))
        if not actual or actions != actual["actions"][:len(actions)]:
            return None
    if pos.kind == "next_game":
        return {"team": None, "kind": "next_game", "champion": None} if str(pos.game + 1) in replay else None
    if pos.kind not in ("pick", "ban"):
        return None
    return recorded["actions"][len(pos.actions)]


def apply_action(
    plan: dict, parent_id: str, champion: str | None, first: str | None = None, *, historical: bool = False,
) -> None:
    pos = position_at(plan, parent_id)
    if pos.kind == "complete":
        raise RehearsalError(409, "This series is complete.")

    order = first or pos.first
    _check_team(plan["teams"], order)
    if pos.kind == "next_game":
        if champion is not None:
            raise RehearsalError(422, "Start the next game before choosing a champion.")
        order = first or pos.second
        game, team, kind = pos.game + 1, None, "start"
    else:
        if pos.actions and order != pos.first:
            raise RehearsalError(422, "First pick can only change at a game opening.")
        other = next(team for team in plan["teams"] if team != order)
        team, kind = schedule(order, other)[len(pos.actions)]
        try:
            validate_action(plan["pool"], pos.locked, pos.current, order, other, team, kind, champion)
        except ValueError as error:
            raise RehearsalError(422, str(error)) from error
        game = pos.game

    for node in plan["nodes"]:
        if (
            node["parent_id"] == parent_id and node["champion"] == champion
            and node["kind"] == kind and node["first"] == order
        ):
            plan["selected_node"] = node["id"]
            return

    node_id = uuid4().hex
    plan["nodes"].append({
        "id": node_id, "parent_id": parent_id, "game": game, "first": order, "team": team,
        "kind": kind, "champion": champion, "origin": "historical" if historical else "rehearsal",
    })
    plan["selected_node"] = node_id


def advance_history(plan: dict, parent_id: str) -> None:
    pos = position_at(plan, parent_id)
    history = history_at(plan, pos)
    if history is None:
        raise RehearsalError(409, "This branch has no next historical action.")
    first = plan["replay"][str(pos.game + 1)]["first"] if pos.kind == "next_game" else None
    apply_action(plan, parent_id, history["champion"], first, historical=True)


def _pin_team(plan: dict, player: str) -> str:
    names = {}
    for team, roster in plan["rosters"].items():
        players = roster.values() if isinstance(roster, dict) else roster
        for name in players:
            names[name.casefold()] = team
    return names.get(player.casefold(), plan["coach_team"])


def save_pin(plan: dict, pin: dict) -> None:
    _check_champion(plan, pin["champion"])
    _check_champion(plan, pin["backup"])
    if pin["champion"] == pin["backup"]:
        raise RehearsalError(422, "Choose a different champion as the backup.")
    if not len(plan["prior"]) < pin["game"] <= int(plan["format"][-1]):
        raise RehearsalError(422, f"Choose a target game in the remaining {plan['format']} draft.")

    if pin.get("node_id"):
        _find_node(plan, pin["node_id"])
        plan["selected_node"] = pin.pop("node_id")
    else:
        pin.pop("node_id", None)
    pin["team"] = _pin_team(plan, pin["player"])
    key = (pin["champion"], pin["game"], pin["player"].casefold())
    plan["pins"] = [
        old for old in plan["pins"]
        if (old["champion"], old["game"], old["player"].casefold()) != key
    ]
    plan["pins"].append(pin)


def save_brief(plan: dict, brief: dict) -> None:
    _check_champion(plan, brief["backup"])
    _find_node(plan, brief["node_id"])
    plan["brief"] = [old for old in plan["brief"] if old["node_id"] != brief["node_id"]]
    plan["brief"].append(brief)
    plan["selected_node"] = brief["node_id"]


def _check_champion(plan: dict, champion: str | None) -> None:
    if champion is not None and champion not in plan["pool"]:
        raise RehearsalError(422, "Choose an enabled champion from the patch pool.")


def _pin_reason(pin: dict, status: str, games: list[list[DraftAction]]) -> str:
    champ, target = pin["champion"], pin["game"]
    for game, actions in enumerate(games[:target - 1], 1):
        if any(action["kind"] == "pick" and action["champion"] == champ for action in actions):
            return f"Picked in Game {game} and locked for the rest of the series."

    if target <= len(games):
        action = next((action for action in games[target - 1] if action["champion"] == champ), None)
        if action:
            verb = "picked" if action["kind"] == "pick" else "banned"
            return f"{action['team']} {verb} {champ} in Game {target}."
        if status == "conflict":
            return f"No eligible pick remains for {pin['team']} in Game {target}."
    if status == "protected":
        if len(games) >= target - 1 and any(
            action["kind"] == "ban" and action["champion"] == champ for action in games[target - 2]
        ):
            return f"Banned in Game {target - 1}. That ban expires before Game {target}."
        return f"The branch preserves availability at the start of Game {target}."
    return f"A pick by either side before Game {target} will lock {champ}."


def evaluated_pins(plan: dict, games: list[list[DraftAction]]) -> list[dict]:
    result = []
    for pin in plan["pins"]:
        status = pin_status(plan["pool"], games, pin["game"], pin["champion"], pin["team"])
        result.append({
            **{key: pin[key] for key in ("champion", "game", "player", "backup")},
            "status": status,
            "reason": _pin_reason(pin, status, games),
        })
    return result


def plan_summary(plan: dict) -> dict:
    return {key: plan[key] for key in ("id", "match_id", "title", "teams", "patch", "source")}


def public_plan(plan: dict) -> dict:
    return {
        **plan_summary(plan),
        **{key: plan[key] for key in (
            "date", "format", "root_id", "selected_node", "priority", "target_game", "player",
            "rosters", "brief",
        )},
        "provenance": plan.get("provenance"),
        "team_history": plan.get("team_history"),
        "coach_team": plan["coach_team"],
        "nodes": [{key: value for key, value in node.items() if key != "first"} for node in plan["nodes"]],
        "pins": evaluated_pins(plan, position_at(plan, plan["selected_node"]).games),
    }


def _illustrative_options(pos: Position, legal: list[str]) -> list[dict]:
    if pos.kind == "ban":
        options = [
            ("Poppy", "A ban preserves Poppy for later games by taking her off both current pick lists."),
            ("Vi", "Ban Vi here and leave Poppy available on the opponent’s next pick."),
            ("Maokai", "This ban removes one engage option while leaving Poppy open."),
        ]
    else:
        options = [
            ("Poppy", "Picking Poppy here takes her out of every later draft for both teams."),
            ("Maokai", "Maokai provides an engage option while leaving Poppy in the current draft pool."),
            ("Sejuani", "Sejuani provides a frontline option if the draft still needs engage."),
        ]
    return [
        {"champion": champion, "score": None, "reason": reason, "games": None, "roles": []}
        for champion, reason in options if champion in legal
    ]


def _model_rosters(plan: dict) -> dict:
    rosters = {}
    for team, roster in plan["rosters"].items():
        names = roster.values() if isinstance(roster, dict) else roster
        players = {name.casefold(): player for name, player in plan["player_ids"].get(team, {}).items()}
        rosters[plan["team_ids"][team]] = [players[name.casefold()] for name in names if name.casefold() in players]
    return rosters


def _public_roster_evidence(plan: dict, result: dict) -> dict:
    status = result.get("roster_evidence") or {}
    public = {}
    for name, team_id in plan["team_ids"].items():
        if team_id not in status:
            continue
        evidence = dict(status[team_id])
        if evidence.get("source") == "supplied" and name not in plan.get("roster_overrides", []):
            if plan["source"] == PROSPECTIVE_ID:
                evidence["source"] = "historical_team_roster"
                evidence["as_of"] = plan["roster_dates"].get(name)
            else:
                evidence["source"] = "recorded_previous_game"
                evidence["as_of"] = plan["date"]
        public[name] = evidence
    return public


def forecast_at(plan: dict, pos: Position, forecaster: Any, prefix: list[DraftAction], limit: int) -> dict:
    ids = plan["team_ids"]
    current = [{**action, "team": ids[action["team"]]} for action in prefix]
    return forecaster.forecast(
        date=plan["date"], patch=plan["patch"], region=plan["region"],
        first=ids[pos.first], second=ids[pos.second], game_number=pos.game, current=current,
        locked=sorted(pos.locked), pool=plan["pool"], rosters=_model_rosters(plan), limit=limit,
    )


def _probability_quality(result: dict) -> str | None:
    if result.get("calibrated"):
        return "calibrated"
    if (
        result.get("model_source") == "public" and result.get("method") == "public_frequencies"
        and result.get("probability_quality") == "estimated"
    ):
        return "estimated"
    return None


def _opponent_outlook(plan: dict, pos: Position, forecaster: Any, initial: dict) -> dict | None:
    quality = _probability_quality(initial)
    if pos.team == plan["coach_team"] or quality is None:
        return None

    from fearless.outlook import lookahead

    def forecast(prefix):
        if list(prefix) == pos.current:
            return initial
        return forecast_at(plan, pos, forecaster, prefix, limit=3)

    future = [pin for pin in plan["pins"] if pin["game"] >= pos.game]
    pin = min(
        future,
        key=lambda pin: (pin["game"] != plan["target_game"], pin["player"] != plan["player"], pin["game"]),
        default=None,
    )

    def evaluate(prefix):
        if pin is None:
            return None
        games = [*pos.games[:-1], list(prefix)]
        return pin_status(plan["pool"], games, pin["game"], pin["champion"], pin["team"])

    def scored_forecast(prefix):
        result = forecast(prefix)
        if _probability_quality(result) != quality:
            return {"status": "unavailable", "choices": []}
        return result

    result = lookahead(
        pool=plan["pool"], locked=pos.locked, current=pos.current,
        first=pos.first, second=pos.second, coach_team=plan["coach_team"],
        forecast=scored_forecast, evaluate_pin=evaluate if pin else None,
    )
    if result["status"] != "scored":
        return None
    return {
        **result, "calibrated": quality == "calibrated", "probability_quality": quality,
        "model_source": initial.get("model_source"),
        **({field: pin[field] for field in ("champion", "game", "player")} if pin else {}),
    }


def node_state(plan: dict, node_id: str, forecaster: Any = None, frequencies: Any = None) -> dict:
    pos = position_at(plan, node_id)
    legal = legal_champions(plan["pool"], pos.locked, pos.current) if pos.team else []
    suggestions = []
    source = "Forecast unavailable"
    forecast = None
    outlook = None
    if plan["source"] == "illustrative":
        if pos.team:
            suggestions = _illustrative_options(pos, legal)
        source = "Illustrative scenario · authored options"
    elif legal:
        result = forecast_at(plan, pos, forecaster, pos.current, limit=3) if forecaster is not None else {}
        if result.get("as_of") and result["choices"]:
            method = result["method"].replace("_", " ")
            quality = _probability_quality(result)
            source = f"Historical {method} ranking · evidence through {result['as_of']}"
            if quality == "calibrated":
                source += " · calibrated for named champion actions"
            elif quality == "estimated":
                source = f"Open LCS draft estimate · training through {result['as_of']}"
            forecast = {
                key: result.get(key) for key in (
                    "as_of", "method", "calibration_through", "probability_condition", "model_source",
                )
            }
            forecast["calibrated"] = quality == "calibrated"
            forecast["probability_quality"] = quality
            forecast["roster_evidence"] = _public_roster_evidence(plan, result)
            for choice in result["choices"]:
                if choice["champion"] in legal:
                    suggestions.append({
                        "champion": choice["champion"],
                        "score": choice["probability"] if quality else None,
                        "reason": " · ".join(choice["evidence"]) or "Historical legal-action evidence.",
                        "games": choice.get("games"),
                        "roles": choice.get("roles", []),
                    })
            outlook = _opponent_outlook(plan, pos, forecaster, result)
        elif frequencies is not None:
            observed = frequencies.estimate(
                as_of=plan["date"], region=plan["region"], kind=pos.kind, legal=legal,
            )
            if observed["choices"]:
                suggestions = observed["choices"]
                source = f"Historical frequency ranking · evidence through {observed['as_of']}"
                forecast = {
                    "method": "frequency", "calibrated": False,
                    "as_of": observed["as_of"], "sample": observed["sample"],
                }

    return {
        "node_id": node_id, "game": pos.game, "first": pos.first, "second": pos.second,
        "team": pos.team, "kind": pos.kind, "actions": pos.actions, "locked": sorted(pos.locked),
        "legal": legal, "suggestions": suggestions, "source": source, "forecast": forecast, "outlook": outlook,
        "pins": evaluated_pins(plan, pos.games), "completed": pos.kind == "complete",
        "history": history_at(plan, pos),
    }
