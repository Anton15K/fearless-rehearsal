"""Pure rules for replaying standard and Fearless tournament drafts."""

from collections.abc import Iterable, Sequence
from typing import Literal, TypedDict


ActionKind = Literal["ban", "pick"]
PinStatus = Literal["protected", "still_possible", "conflict", "satisfied"]


class DraftAction(TypedDict):
    team: str
    kind: ActionKind
    champion: str | None


_TOURNAMENT_ORDER: tuple[tuple[int, ActionKind], ...] = (
    (0, "ban"), (1, "ban"), (0, "ban"), (1, "ban"), (0, "ban"), (1, "ban"),
    (0, "pick"), (1, "pick"), (1, "pick"), (0, "pick"), (0, "pick"), (1, "pick"),
    (1, "ban"), (0, "ban"), (1, "ban"), (0, "ban"),
    (1, "pick"), (0, "pick"), (0, "pick"), (1, "pick"),
)
DRAFT_LENGTH = len(_TOURNAMENT_ORDER)


def schedule(first: str, second: str) -> list[tuple[str, ActionKind]]:
    """Return the 20 draft steps using draft position independently of map side."""
    if first == second:
        raise ValueError("Draft positions must belong to different teams.")
    teams = (first, second)
    return [(teams[position], kind) for position, kind in _TOURNAMENT_ORDER]


def legal_champions(
    pool: Iterable[str], locked: Iterable[str], current: Sequence[DraftAction]
) -> list[str]:
    """Return available champion IDs in the order of the enabled pool."""
    used = {action["champion"] for action in current if action["champion"] is not None}
    unavailable = used.union(locked)
    return [champion for champion in pool if champion not in unavailable]


def validate_action(
    pool: Iterable[str],
    locked: Iterable[str],
    current: Sequence[DraftAction],
    first: str,
    second: str,
    team: str,
    kind: str,
    champion: str | None,
) -> None:
    """Raise ValueError if a proposed action is illegal for the next draft step."""
    if len(current) >= DRAFT_LENGTH:
        raise ValueError("This draft is complete.")

    expected_team, expected_kind = schedule(first, second)[len(current)]
    if (team, kind) != (expected_team, expected_kind):
        raise ValueError(f"{expected_team} must {expected_kind} next.")

    if champion is None:
        if kind == "pick":
            raise ValueError("A pick requires a champion.")
        return

    if not isinstance(champion, str) or champion not in pool:
        raise ValueError("Choose an enabled champion from the pool.")
    if champion in locked:
        raise ValueError("This champion was picked in a previous game.")
    if any(action["champion"] == champion for action in current):
        raise ValueError("This champion was already picked or banned in this game.")


def series_locks(
    completed_games: Iterable[Iterable[DraftAction]], fearless: bool = True
) -> set[str]:
    """Derive the series locks from both teams' picks in completed games."""
    if not fearless:
        return set()
    return {
        action["champion"]
        for game in completed_games
        for action in game
        if action["kind"] == "pick" and action["champion"] is not None
    }


def pin_status(
    pool: Iterable[str],
    games: Sequence[Sequence[DraftAction]],
    target_game: int,
    champion: str,
    team: str | None = None,
    fearless: bool = True,
) -> PinStatus:
    """Evaluate a champion pin for a one-based target game and optional picking team.

    Pass recorded action lists in chronological order, including the active
    partial draft. Unstarted games may be omitted or represented by empty lists.
    A pin is satisfied by the required pick and conflicts when that pick has
    become impossible. An open pin is protected when this branch guarantees
    availability at the target game's start. It is still possible while a prior
    draft could pick the champion. A pin never restricts a draft action.
    """
    if target_game < 1:
        raise ValueError("Game numbers start at one.")
    if champion not in pool:
        return "conflict"

    target_index = target_game - 1
    earlier_games = games[:target_index]
    if champion in series_locks(earlier_games, fearless=fearless):
        return "conflict"

    if target_index < len(games):
        target = games[target_index]
        for action in target:
            if action["champion"] == champion:
                if action["kind"] == "pick" and (team is None or action["team"] == team):
                    return "satisfied"
                return "conflict"
        if len(target) >= DRAFT_LENGTH:
            return "conflict"
        if team is not None and len(target) >= 2:
            # The first two bans identify the teams' draft positions.
            remaining = schedule(target[0]["team"], target[1]["team"])[len(target):]
            if (team, "pick") not in remaining:
                return "conflict"

    if not fearless or target_index == 0:
        return "protected"
    if len(earlier_games) == target_index:
        preceding_game = earlier_games[-1]
        if len(preceding_game) == DRAFT_LENGTH or any(
            action["kind"] == "ban" and action["champion"] == champion for action in preceding_game
        ):
            return "protected"
    return "still_possible"
