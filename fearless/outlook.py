"""Bounded conditional lookahead for the next one or two draft actions."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from math import fsum, isfinite

from fearless.rules import DraftAction, PinStatus, legal_champions, schedule, validate_action


def _replies(prefix, team, kind, pool, locked, forecast, max_choices):
    available = set(legal_champions(pool, locked, prefix))
    if not available:
        return ([({"team": team, "kind": kind, "champion": None}, 1.0)] if kind == "ban" else []), None

    result = forecast(prefix)
    if result.get("status", "scored") != "scored":
        return [], None

    replies = []
    seen = set()
    for row in result["choices"][:max_choices]:
        champion, probability = row["champion"], float(row["probability"])
        if champion not in available or champion in seen:
            raise ValueError("The forecast returned an illegal or repeated champion.")
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Forecast probabilities must be finite and between zero and one.")
        seen.add(champion)
        if probability:
            replies.append(({"team": team, "kind": kind, "champion": champion}, probability))
    if fsum(probability for _, probability in replies) > 1 + 1e-9:
        raise ValueError("Forecast probability mass cannot exceed one.")
    return replies, result.get("method", "forecast") if replies else None


def lookahead(
    *,
    pool: Iterable[str],
    locked: Iterable[str],
    current: Sequence[DraftAction],
    first: str,
    second: str,
    coach_team: str,
    forecast: Callable[[Sequence[DraftAction]], dict],
    evaluate_pin: Callable[[Sequence[DraftAction]], PinStatus] | None = None,
    horizon: int = 2,
    coach_action: DraftAction | None = None,
    max_choices: int = 3,
) -> dict:
    """Explore up to two steps in the current game from a validated draft prefix.

    The callbacks receive the full prefix, including hypothetical actions. The
    forecast supplies ranked legal champion probabilities over the full named
    choice set. Optional bans are absent from that distribution. A ban with no
    legal champions is skipped deterministically. The coach supplies their own
    current action or the search stops before that turn.

    Paths use the opponent's probabilities conditional on each hypothetical
    prefix. Only paths reaching the reported horizon contribute outcome mass.
    Unexpanded mass stays residual. Pair mass sums represented paths in either
    order for two consecutive opponent picks. Pin outcomes describe status at
    the horizon and cannot establish eventual availability in a later draft.
    """
    if type(horizon) is not int or horizon not in (1, 2):
        raise ValueError("Lookahead is limited to one or two actions.")
    if type(max_choices) is not int or not 1 <= max_choices <= 8:
        raise ValueError("Lookahead can retain one to eight choices per forecast.")
    if coach_team not in (first, second):
        raise ValueError("The coach must belong to one of the drafting teams.")

    current = tuple(current)
    available_steps = schedule(first, second)[len(current):len(current) + horizon]
    pool, locked = set(pool), set(locked)
    if coach_action is not None:
        if not available_steps or available_steps[0][0] != coach_team:
            raise ValueError("An explicit coach action requires the current coach turn.")
        validate_action(pool, locked, current, first, second, **coach_action)

    steps = []
    for index, (team, kind) in enumerate(available_steps):
        if team == coach_team and (index != 0 or coach_action is None):
            break
        steps.append((team, kind))

    paths = [(current, 1.0)] if steps else []
    methods = set()
    for index, (team, kind) in enumerate(steps):
        next_paths = []
        for prefix, mass in paths:
            if index == 0 and coach_action is not None:
                replies, method = [(coach_action, 1.0)], None
            else:
                replies, method = _replies(prefix, team, kind, pool, locked, forecast, max_choices)
            if method is not None:
                methods.add(method)
            next_paths.extend((prefix + (action,), mass * probability) for action, probability in replies)
        paths = next_paths

    observed_mass = min(1.0, fsum(mass for _, mass in paths))
    outcome_masses, outcome_counts, pair_masses = defaultdict(list), Counter(), defaultdict(list)
    opponent_pair = len(steps) == 2 and steps[0] == steps[1] and steps[0][1] == "pick"
    opponent_steps = any(team != coach_team for team, _ in steps)
    records = []
    for prefix, mass in paths:
        actions = list(prefix[len(current):])
        outcome = evaluate_pin(prefix) if evaluate_pin is not None else None
        records.append({"actions": actions, "probability": mass, "pin_status": outcome})
        if outcome is not None:
            outcome_masses[outcome].append(mass)
            outcome_counts[outcome] += 1
        if opponent_pair:
            pair = tuple(sorted(action["champion"] for action in actions))
            pair_masses[pair].append(mass)

    if paths:
        status = "scored"
    elif steps:
        status = "unavailable"
    else:
        status = "awaiting_coach" if available_steps else "complete"

    conditional = "opponent_actions_given_named_champion"
    if coach_action is not None:
        conditional = f"coach_action_and_{conditional}" if opponent_steps else "coach_action"
    elif paths and not methods:
        conditional = "rules_only"

    method = "mixed" if len(methods) > 1 else next(iter(methods), None)
    return {
        "status": status,
        "method": method,
        "requested_horizon": horizon,
        "horizon": len(steps),
        "conditional": conditional,
        "observed_mass": observed_mass,
        "residual_mass": 1.0 - observed_mass,
        "outcomes": [
            {"status": outcome, "probability": fsum(masses), "branches": outcome_counts[outcome]}
            for outcome, masses in sorted(outcome_masses.items())
        ],
        "pairs": [
            {"champions": list(pair), "probability": fsum(masses)}
            for pair, masses in sorted(pair_masses.items(), key=lambda item: (-fsum(item[1]), item[0]))
        ],
        "paths": records,
    }
