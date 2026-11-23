"""Build draft states and apply changes to a saved rehearsal tree."""

from dataclasses import KW_ONLY, dataclass, field
from typing import Any
from uuid import uuid4

from fearless.app_catalog import Match, MatchCatalog, PROSPECTIVE_ID, eligibility, normalize_disables, ruleset_for
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
        if any(settings.get(field) is not None for field in ("teams", "coach_team", "date", "format", "event", "disabled", "ruleset")):
            raise RehearsalError(422, "Choose New matchup to set the teams and series details.")
        match = catalog.matches.get(settings.get("match_id"))
        if match is None:
            raise RehearsalError(404, "Match not found.")
    patch = settings.get("patch") or match.metadata["patch"]
    pool = catalog.pool(match, patch)
    if pool is None:
        raise RehearsalError(422, "The champion catalog does not include that patch.")
    ruleset = ruleset_for(match.metadata.get("event"), match.metadata["date"], settings.get("ruleset"))
    manual = settings.get("disabled")
    if manual is not None and (not isinstance(manual, list) or len(manual) > 30):
        raise RehearsalError(422, "Select up to 30 additional disables.")
    base = eligibility(pool, patch, ruleset, manual=manual if manual is not None else (),
                       historical=match.metadata["source"] == "historical")
    plan = new_plan(match, tuple(base["pool"]), settings, disabled=tuple(base["disabled"]))
    plan.update(base)
    if patch == match.metadata["patch"]:
        plan["game_eligibility"] = {str(game): game_data["eligibility"]
                                    for game, game_data in match.historical.items()}
    return plan


def new_plan(match: Match, pool: tuple[str, ...], settings: dict, *, disabled: tuple[str, ...] = ()) -> dict:
    meta = match.metadata
    patch = settings.get("patch") or meta["patch"]
    recorded_mode = meta.get("draft_mode", "fearless")
    draft_mode = settings.get("draft_mode") or recorded_mode
    if draft_mode not in ("fearless", "standard"):
        raise RehearsalError(422, "Choose Fearless Draft or Standard Tournament Draft.")
    if meta["source"] != PROSPECTIVE_ID and draft_mode != recorded_mode:
        raise RehearsalError(422, "Choose New matchup to change the draft mode.")
    first = settings.get("first") or meta["first"]
    _check_team(meta["teams"], first)
    blue = settings.get("blue") if settings.get("blue") is not None else meta.get("blue")
    if blue is not None:
        _check_team(meta["teams"], blue, "Blue side")
    prior_winners = list(meta.get("prior_winners") or [])
    prior_winner = prior_winners[-1] if prior_winners else None
    selection_right = _opponent(meta["teams"], prior_winner) if prior_winner else None
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
        "event": meta.get("event"),
        "disabled": list(disabled),
        "title": settings.get("title") or meta["title"],
        "teams": meta["teams"],
        "patch": patch,
        "date": meta["date"],
        "format": meta["format"],
        "draft_mode": draft_mode,
        "source": meta["source"],
        "provenance": meta.get("provenance"),
        "team_history": meta.get("team_history"),
        "roster_dates": meta.get("roster_dates"),
        "root_id": root,
        "selected_node": root,
        "nodes": [{
            "id": root, "parent_id": None, "game": len(match.previous) + 1,
            "team": None, "kind": "start", "champion": None,
            "origin": "rehearsal", "first": first, "blue": blue, "selection_right": selection_right,
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
        "prior_winners": prior_winners,
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


def _base_eligibility(plan: dict, game: int) -> dict:
    row = plan.get("game_eligibility", {}).get(str(game))
    return row if row is not None else plan


def inherited_game_disabled(plan: dict, game: int, node_id: str | None) -> list[str]:
    nodes = {node["id"]: node for node in plan.get("nodes", ())}
    cursor = nodes.get(node_id)
    while cursor is not None:
        if cursor["game"] <= game and "game_disabled" in cursor:
            return cursor["game_disabled"]
        cursor = nodes.get(cursor["parent_id"])
    return []


def validate_game_disabled(plan: dict, game: int, values: list[str]) -> list[str]:
    base = _base_eligibility(plan, game)
    values = normalize_disables(base.get("client_pool", [*base["pool"], *base.get("disabled", ())]), values)
    if len(values) > 30 or not set(base["pool"]) - set(values):
        raise RehearsalError(422, "Select up to 30 additional disables and leave champions available.")
    return values


def pool_at(plan: dict, game: int, node_id: str | None = None) -> list[str]:
    base = _base_eligibility(plan, game)
    disabled = set(inherited_game_disabled(plan, game, node_id or plan.get("selected_node")))
    return [champion for champion in base["pool"] if champion not in disabled]


def _public_eligibility(plan: dict, game: int, node_id: str) -> dict:
    base = _base_eligibility(plan, game)
    disabled = base.get("disabled", [])
    game_disabled = inherited_game_disabled(plan, game, node_id)
    restrictions = base.get("restrictions", [
        {"champion": name, "reason": "manual", "source": None} for name in disabled
    ])
    extra = [name for name in game_disabled if name not in disabled]
    return {
        "ruleset": base.get("ruleset", "manual"), "game_disabled": game_disabled,
        "restriction_status": base.get("restriction_status", "unknown" if plan["source"] == "historical" else "manual"),
        "disabled": sorted(set(disabled).union(game_disabled), key=str.casefold),
        "restrictions": sorted([*restrictions, *[
            {"champion": name, "reason": "organizer", "source": None} for name in extra
        ]], key=lambda item: item["champion"].casefold()),
        "client_pool_size": len(base.get("client_pool", ())) or len(base["pool"]) + len(disabled),
        "effective_pool_size": len(pool_at(plan, game, node_id)),
    }


def _check_team(teams: list[str], team: str, field: str = "First pick") -> None:
    if team not in teams:
        raise RehearsalError(422, f"{field} must belong to a team in this match.")


def _opponent(teams: list[str], team: str) -> str:
    return next(other for other in teams if other != team)


def _is_fearless(plan: dict) -> bool:
    return plan.get("draft_mode", "fearless") == "fearless"


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
    _: KW_ONLY
    opening: dict | None = None
    blue: str | None = None
    selection_right: str | None = None
    selection_choice: str | None = None
    game_disabled: list[str] | None = None
    progression: str = "hypothetical"
    series_status: str = "drafting"
    game_winner: str | None = None
    series_winner: str | None = None
    score: dict[str, int] = field(default_factory=dict)
    score_complete: bool = False
    can_rehearse_next: bool = False
    recorded: dict | None = None

    @property
    def game(self) -> int:
        return self.node["game"]

    @property
    def current(self) -> list[DraftAction]:
        return self.games[-1]

    @property
    def red(self) -> str | None:
        if self.blue is None:
            return None
        return self.second if self.blue == self.first else self.first


def _matching_replay(plan: dict, game: int, first: str, blue: str | None, games: list[list[DraftAction]]) -> dict | None:
    replay = plan["replay"]
    recorded = replay.get(str(game))
    if not recorded or first != recorded["first"] or (recorded.get("blue") and blue != recorded["blue"]):
        return None
    prior = len(plan["prior"])
    for number, actions in enumerate(games[prior:], start=prior + 1):
        actual = replay.get(str(number))
        if not actual or actions != actual["actions"][:len(actions)]:
            return None
    return recorded


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
    opening = path.pop()
    first, blue = opening["first"], opening.get("blue")
    selection_right, selection_choice = opening.get("selection_right"), opening.get("selection_choice")
    game_disabled = opening.get("game_disabled")
    progression = {"illustrative": "hypothetical", "historical": "historical"}.get(plan["source"], "actual")
    previous = list(plan["prior"])
    actions = [{**action, "origin": "rehearsal"} for action in plan["opening"]]
    winners = dict(enumerate(plan.get("prior_winners", ()), 1))
    for node in reversed(path):
        if node["kind"] in ("pick", "ban"):
            first = node["first"]
            actions.append({**_action(node), "origin": node["origin"]})
        elif node["kind"] == "result":
            winners[node["game"]] = node["team"]
        else:
            if node["kind"] == "start":
                previous.append([_action(action) for action in actions])
                actions = []
                opening = node
            first, blue = node["first"], node.get("blue")
            selection_right, selection_choice = node.get("selection_right"), node.get("selection_choice")
            game_disabled = node.get("game_disabled", game_disabled)
            progression = node.get("progression", "historical" if node["origin"] == "historical" else "hypothetical")

    game = selected["game"]
    teams = plan["teams"]
    second = _opponent(teams, first)
    games = [*previous, [_action(action) for action in actions]]
    recorded = _matching_replay(plan, game, first, blue, games) if progression == "historical" else None
    if progression == "historical" and not recorded:
        progression = "hypothetical"
    score = {team: sum(winner == team for winner in winners.values()) for team in teams}
    max_game = int(plan["format"][-1])
    series_winner = next((team for team in teams if score[team] >= max_game // 2 + 1), None)
    game_winner = winners.get(game)
    score_complete = all(winners.get(number) is not None for number in range(1, game))
    finished_draft = len(actions) == DRAFT_LENGTH
    can_rehearse_next = finished_draft and game < max_game
    if not finished_draft:
        team, kind = schedule(first, second)[len(actions)]
        status = "drafting"
    else:
        team = None
        if game_winner is None and (progression == "actual" or (recorded and recorded.get("winner"))):
            kind, status = "result", "awaiting_result"
        elif series_winner and progression != "hypothetical":
            kind, status = "complete", "clinched"
            selection_right = None
        elif can_rehearse_next and (progression != "historical" or str(game + 1) in plan["replay"]):
            kind, status = "next_game", "awaiting_selection"
            selection_right = _opponent(teams, game_winner) if game_winner else None
        else:
            kind, status = "complete", "rehearsal_complete"
            selection_right = None
    return Position(
        node=selected, opening=opening, first=first, second=second, blue=blue,
        selection_right=selection_right, selection_choice=selection_choice, game_disabled=game_disabled,
        games=games, actions=actions, locked=series_locks(previous, fearless=_is_fearless(plan)), team=team, kind=kind,
        progression=progression, series_status=status, game_winner=game_winner, series_winner=series_winner,
        score=score, score_complete=score_complete, can_rehearse_next=can_rehearse_next, recorded=recorded,
    )


def history_at(plan: dict, pos: Position) -> dict | None:
    if pos.recorded is None:
        return None
    if pos.kind == "result":
        return {"team": None, "kind": "result", "champion": None}
    if pos.kind == "next_game":
        if str(pos.game + 1) in plan["replay"]:
            return {"team": None, "kind": "next_game", "champion": None}
        return None
    if pos.kind not in ("pick", "ban"):
        return None
    return pos.recorded["actions"][len(pos.actions)]


def _select_child(plan: dict, parent_id: str, **values) -> None:
    for node in plan["nodes"]:
        if node["parent_id"] == parent_id and all(node.get(key) == value for key, value in values.items()):
            plan["selected_node"] = node["id"]
            return
    new_id = uuid4().hex
    plan["nodes"].append({"id": new_id, "parent_id": parent_id, **values})
    plan["selected_node"] = new_id


def _save_selection(
    plan: dict, pos: Position, kind: str, first: str, blue: str | None, *,
    progression: str, choice: str | None = None, right: str | None = None, disabled: list[str] | None = None,
) -> None:
    opening_edit = kind == "selection"
    _select_child(
        plan, pos.opening["id"] if opening_edit else pos.node["id"],
        kind=kind, game=pos.game if opening_edit else pos.game + 1, first=first, blue=blue, team=None, champion=None,
        selection_choice=choice, selection_right=right, progression=progression,
        origin="historical" if progression == "historical" else "rehearsal",
        **({"game_disabled": disabled} if disabled is not None else {}),
    )


def select_opening(
    plan: dict, parent_id: str, first: str, blue: str | None, *,
    choice: str | None = None, hypothetical: bool = False, disabled: list[str] | None = None,
) -> None:
    pos = position_at(plan, parent_id)
    _check_team(plan["teams"], first)
    if blue is not None:
        _check_team(plan["teams"], blue, "Blue side")
    if not isinstance(hypothetical, bool):
        raise RehearsalError(422, "Choose whether this is a preparation branch.")
    if choice is not None and choice not in ("first_pick", "map_side"):
        raise RehearsalError(422, "Choose draft order or map side for the team with selection rights.")
    at_opening = not pos.actions
    if not at_opening and not pos.can_rehearse_next:
        raise RehearsalError(409, "Opening selections can only change before the first action or between games.")
    game = pos.game if at_opening else pos.game + 1
    restrictions = validate_game_disabled(plan, game, disabled) if disabled is not None else pos.game_disabled

    if at_opening:
        original = position_at(plan, pos.opening["id"])
        requested = (first, blue, restrictions or [])
        original_selection = (original.first, original.blue, original.game_disabled or [])
        if original.progression == "historical" and not hypothetical and requested == original_selection:
            plan["selected_node"] = original.node["id"]
            return
        progression = "hypothetical" if hypothetical or pos.progression == "historical" else pos.progression
        right = pos.selection_right if progression != "hypothetical" else None
        choice = (choice or pos.selection_choice) if right is not None else None
        if progression == "actual" and pos.game > 1 and blue is None:
            raise RehearsalError(422, "Choose the blue-side team for this game.")
        if requested == original_selection and (choice, right, progression) == (
            original.selection_choice, original.selection_right, original.progression,
        ):
            plan["selected_node"] = original.node["id"]
            return
        _save_selection(
            plan, pos, "selection", first, blue, choice=choice, right=right,
            progression=progression, disabled=restrictions,
        )
        return

    if hypothetical:
        _save_selection(plan, pos, "start", first, blue, progression="hypothetical", disabled=restrictions)
        return
    if pos.progression != "actual" or pos.series_status != "awaiting_selection" or not pos.game_winner:
        raise RehearsalError(409, "Record the game winner before starting another confirmed game.")
    if blue is None or choice is None:
        raise RehearsalError(422, "Record blue side and which selection the previous game's loser made.")
    _save_selection(
        plan, pos, "start", first, blue, choice=choice, right=pos.selection_right,
        progression="actual", disabled=restrictions,
    )


def apply_action(
    plan: dict, parent_id: str, champion: str | None, first: str | None = None, *, historical: bool = False,
) -> None:
    pos = position_at(plan, parent_id)
    if pos.kind == "complete":
        raise RehearsalError(409, "This series is complete.")
    if pos.kind in ("next_game", "result"):
        if champion is not None:
            raise RehearsalError(422, "Start the next game before choosing a champion.")
        if pos.progression == "actual" and first is None:
            raise RehearsalError(422, "Choose the opening selections for the next game.")
        # Older clients still open preparation branches via an empty action.
        select_opening(plan, parent_id, first or pos.second, None, hypothetical=True)
        return

    order = first or pos.first
    _check_team(plan["teams"], order)
    if pos.actions and order != pos.first:
        raise RehearsalError(422, "First pick can only change at a game opening.")
    other = _opponent(plan["teams"], order)
    team, kind = schedule(order, other)[len(pos.actions)]
    try:
        validate_action(pool_at(plan, pos.game, parent_id), pos.locked, pos.current, order, other, team, kind, champion)
    except ValueError as error:
        raise RehearsalError(422, str(error)) from error

    _select_child(
        plan, parent_id, game=pos.game, first=order, team=team, kind=kind, champion=champion,
        origin="historical" if historical else "rehearsal",
    )


def record_result(plan: dict, parent_id: str, winner: str, *, historical: bool = False) -> None:
    pos = position_at(plan, parent_id)
    expected_progression = "historical" if historical else "actual"
    if pos.progression != expected_progression or pos.series_status != "awaiting_result":
        raise RehearsalError(409, "A result can only be recorded after the corresponding draft is complete.")
    _check_team(plan["teams"], winner, "Winner")
    for node in plan["nodes"]:
        if node["parent_id"] == parent_id and node["kind"] == "result" and node["team"] != winner:
            raise RehearsalError(409, "A different result is already recorded for this draft.")
    _select_child(
        plan, parent_id, game=pos.game, first=pos.first, team=winner, kind="result", champion=None,
        origin="historical" if historical else "rehearsal",
    )


def advance_history(plan: dict, parent_id: str) -> None:
    pos = position_at(plan, parent_id)
    history = history_at(plan, pos)
    if history is None:
        raise RehearsalError(409, "This branch has no next historical action.")
    if pos.kind == "result":
        record_result(plan, parent_id, pos.recorded["winner"], historical=True)
    elif pos.kind == "next_game":
        next_game = plan["replay"][str(pos.game + 1)]
        _save_selection(
            plan, pos, "start", next_game["first"], next_game.get("blue"),
            progression="historical", right=pos.selection_right, disabled=pos.game_disabled,
        )
    else:
        apply_action(plan, parent_id, history["champion"], historical=True)


def _pin_team(plan: dict, player: str) -> str:
    names = {}
    for team, roster in plan["rosters"].items():
        players = roster.values() if isinstance(roster, dict) else roster
        for name in players:
            names[name.casefold()] = team
    return names.get(player.casefold(), plan["coach_team"])


def save_pin(plan: dict, pin: dict) -> None:
    if not len(plan["prior"]) < pin["game"] <= int(plan["format"][-1]):
        raise RehearsalError(422, f"Choose a target game in the remaining {plan['format']} draft.")
    node_id = pin.get("node_id") or plan["selected_node"]
    available = pool_at(plan, pin["game"], node_id)
    _check_champion(available, pin["champion"])
    _check_champion(available, pin["backup"])
    if pin["champion"] == pin["backup"]:
        raise RehearsalError(422, "Choose a different champion as the backup.")

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
    node = _find_node(plan, brief["node_id"])
    _check_champion(pool_at(plan, node["game"], node["id"]), brief["backup"])
    plan["brief"] = [old for old in plan["brief"] if old["node_id"] != brief["node_id"]]
    plan["brief"].append(brief)
    plan["selected_node"] = brief["node_id"]


def _check_champion(pool: list[str], champion: str | None) -> None:
    if champion is not None and champion not in pool:
        raise RehearsalError(422, "Choose an enabled champion from the patch pool.")


def _pin_reason(pin: dict, status: str, games: list[list[DraftAction]], *, fearless: bool) -> str:
    champ, target = pin["champion"], pin["game"]
    if fearless:
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


def evaluated_pins(plan: dict, games: list[list[DraftAction]], node_id: str | None = None) -> list[dict]:
    result = []
    for pin in plan["pins"]:
        available = pool_at(plan, pin["game"], node_id)
        fearless = _is_fearless(plan)
        status = pin_status(available, games, pin["game"], pin["champion"], pin["team"], fearless=fearless)
        reason = (_pin_reason(pin, status, games, fearless=fearless) if pin["champion"] in available
                  else f"Not eligible in Game {pin['game']} under these tournament restrictions.")
        result.append({
            **{key: pin[key] for key in ("champion", "game", "player", "backup")},
            "status": status,
            "reason": reason,
        })
    return result


def plan_summary(plan: dict) -> dict:
    node = _find_node(plan, plan["selected_node"])
    return {
        **{key: plan[key] for key in ("id", "match_id", "title", "teams", "patch", "source")},
        "draft_mode": plan.get("draft_mode", "fearless"),
        **_public_eligibility(plan, node["game"], node["id"]),
    }


def public_plan(plan: dict) -> dict:
    return {
        **plan_summary(plan),
        **{key: plan[key] for key in (
            "date", "format", "root_id", "selected_node", "priority", "target_game", "player",
            "rosters", "brief",
        )},
        "event": plan.get("event"),
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


def _forecast_context(plan: dict, pos: Position, prefix: list[DraftAction], forecaster=None, overrides_only=False) -> dict:
    ids = plan["team_ids"]
    current = [{**action, "team": ids[action["team"]]} for action in prefix]
    rosters = _model_rosters(plan)
    if plan.get("source") == PROSPECTIVE_ID and (overrides_only or getattr(forecaster, "accepts_event", False)):
        rosters = {ids[name]: rosters[ids[name]] for name in plan.get("roster_overrides", ()) if ids[name] in rosters}
    recorded = plan.get("replay", {}).get(str(pos.game), {})
    return dict(
        date=recorded.get("date", plan["date"]), patch=recorded.get("patch", plan["patch"]), region=plan["region"],
        first=ids[pos.first], second=ids[pos.second], game_number=pos.game, current=current,
        locked=sorted(pos.locked), pool=pool_at(plan, pos.game, pos.node.get("id")), rosters=rosters,
        **({"fearless": False} if not _is_fearless(plan) else {}),
        **({"event": plan.get("event"), "series_id": plan.get("match_id")}
           if getattr(forecaster, "accepts_event", False) else {}),
    )


def forecast_at(plan: dict, pos: Position, forecaster: Any, prefix: list[DraftAction], limit: int, *, overrides_only=False) -> dict:
    return forecaster.forecast(**_forecast_context(plan, pos, prefix, forecaster, overrides_only), limit=limit)


def _probability_quality(result: dict) -> str | None:
    if result.get("calibrated"):
        return "calibrated"
    if (
        result.get("probability_quality") == "estimated" and (
            (result.get("model_source") == "public" and result.get("method") == "public_frequencies")
            or (result.get("model_source") == "private_event" and result.get("method") in (
                "refreshed_pick", "patch_pick_adaptation", "temperature_refreshed_pick",
                "refreshed_opening_ban", "refreshed_second_phase_ban",
            ))
        )
    ):
        return "estimated"
    return None


def _opponent_outlook(plan: dict, pos: Position, forecaster: Any, initial: dict) -> dict | None:
    quality = _probability_quality(initial)
    if pos.team == plan["coach_team"] or quality is None:
        return None

    from fearless.outlook import lookahead

    turns = schedule(pos.first, pos.second)[len(pos.current):len(pos.current) + 2]
    pair_window = len(turns) == 2 and turns[0] == turns[1] and turns[0][1] == "pick"
    path_model = getattr(forecaster, "frozen", forecaster) if pair_window else forecaster

    def forecast(prefix):
        if path_model is forecaster and list(prefix) == pos.current:
            return initial
        return forecast_at(plan, pos, path_model, prefix, limit=3, overrides_only=getattr(forecaster, "accepts_event", False))

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
        return pin_status(
            pool_at(plan, pin["game"], pos.node.get("id")), games, pin["game"], pin["champion"], pin["team"],
            fearless=_is_fearless(plan),
        )

    qualities, sources = set(), set()

    def scored_forecast(prefix):
        result = forecast(prefix)
        actual = _probability_quality(result)
        if actual is None or (result.get("model_source") == "public") != (initial.get("model_source") == "public"):
            return {"status": "unavailable", "choices": []}
        qualities.add(actual)
        sources.add(result.get("model_source"))
        return result

    result = lookahead(
        pool=pool_at(plan, pos.game, pos.node.get("id")), locked=pos.locked, current=pos.current,
        first=pos.first, second=pos.second, coach_team=plan["coach_team"],
        forecast=scored_forecast, evaluate_pin=evaluate if pin else None,
    )
    if result["status"] != "scored":
        return None
    rank_pairs = getattr(forecaster, "rank_pairs", None)
    if result["pairs"] and rank_pairs is not None:
        alternatives = rank_pairs(**_forecast_context(plan, pos, pos.current, forecaster), limit=3)
        if alternatives:
            result["ranked_pair_alternatives"] = alternatives
    path_quality = "estimated" if "estimated" in qualities else "calibrated"
    return {
        **result, "calibrated": path_quality == "calibrated", "probability_quality": path_quality,
        "model_source": "private_event" if "private_event" in sources else next(iter(sources), None),
        **({field: pin[field] for field in ("champion", "game", "player")} if pin else {}),
    }


def node_state(plan: dict, node_id: str, forecaster: Any = None, frequencies: Any = None) -> dict:
    pos = position_at(plan, node_id)
    recorded = plan["replay"].get(str(pos.game), {})
    legal = legal_champions(pool_at(plan, pos.game, node_id), pos.locked, pos.current) if pos.team else []
    suggestions = []
    source = "Forecast unavailable"
    forecast = None
    outlook = None
    if plan["source"] == "illustrative":
        if pos.team:
            suggestions = _illustrative_options(pos, legal)
        source = "Illustrative scenario · authored options"
    elif legal:
        fearless = _is_fearless(plan)
        # The shipped probability policies have no Standard-specific validation.
        result = forecast_at(plan, pos, forecaster, pos.current, limit=3) if forecaster is not None and fearless else {}
        if result.get("as_of") and result["choices"]:
            method = result["method"].replace("_", " ")
            quality = _probability_quality(result)
            source = f"Historical {method} ranking · evidence through {result['as_of']}"
            if quality == "calibrated":
                source += " · calibrated for named champion actions"
            elif result.get("model_source") == "private_event" and quality == "estimated":
                source = f"Tournament {pos.kind} estimate · evidence through {result['as_of']}"
            elif quality == "estimated":
                source = f"Open LCS draft estimate · training through {result['as_of']}"
            forecast = {
                key: result.get(key) for key in (
                    "as_of", "method", "calibration_through", "probability_condition", "model_source",
                )
            }
            forecast["calibrated"] = quality == "calibrated"
            if result.get("event_evidence"):
                forecast["event_evidence"] = result["event_evidence"]
            if result.get("pick_calibration"):
                forecast["pick_calibration"] = result["pick_calibration"]
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
                as_of=recorded.get("date", plan["date"]), region=plan["region"], kind=pos.kind, legal=legal,
                **({"fearless": False} if not fearless else {}),
            )
            if observed["choices"]:
                suggestions = observed["choices"]
                label = "Historical" if fearless else "Standard draft"
                source = f"{label} frequency ranking · evidence through {observed['as_of']}"
                forecast = {
                    "method": "frequency", "calibrated": False,
                    "as_of": observed["as_of"], "sample": observed["sample"],
                }

    return {
        **_public_eligibility(plan, pos.game, node_id),
        "patch": recorded.get("patch", plan["patch"]),
        "draft_mode": plan.get("draft_mode", "fearless"),
        "node_id": node_id, "game": pos.game, "first": pos.first, "second": pos.second,
        "team": pos.team, "kind": pos.kind, "actions": pos.actions, "locked": sorted(pos.locked),
        "legal": legal, "suggestions": suggestions, "source": source, "forecast": forecast, "outlook": outlook,
        "blue": pos.blue, "red": pos.red, "selection_right": pos.selection_right,
        "selection_choice": pos.selection_choice, "progression": pos.progression,
        "series_status": pos.series_status, "game_winner": pos.game_winner, "series_winner": pos.series_winner,
        "score": pos.score, "score_complete": pos.score_complete, "can_rehearse_next": pos.can_rehearse_next,
        "pins": evaluated_pins(plan, pos.games, node_id), "completed": pos.kind == "complete",
        "history": history_at(plan, pos),
    }
