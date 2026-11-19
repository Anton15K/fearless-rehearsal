"""Resolve matchup teams and rosters using completed games before a cutoff."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from fearless.app_forecast import _completed_day


def team_key(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class TeamObservation:
    id: str
    day: str
    region: str
    preferred: bool
    roster: dict[str, str]


@dataclass(frozen=True)
class TeamHistory:
    id: str
    as_of: str
    region: str
    roster: list[str]
    roster_as_of: str | None
    player_ids: dict[str, str]


class TeamDirectory:
    def __init__(self):
        self._names = defaultdict(list)
        self._teams = defaultdict(list)
        self._players = defaultdict(dict)

    def add(self, games: list[dict], *, preferred: bool) -> None:
        for game in games:
            if not game.get("eligible", True) or not game.get("finished", True):
                continue
            try:
                day = _completed_day(game)
                names = game["team_names"]
                rosters = game.get("rosters", {})
                for team, name in names.items():
                    roster = {
                        player["player_name"].strip(): str(player["player"])
                        for player in rosters.get(team, ())
                        if player.get("player_name") and player.get("player")
                    }
                    sighting = TeamObservation(str(team), day, game.get("region", ""), preferred, roster)
                    self._names[team_key(name)].append(sighting)
                    self._teams[sighting.id].append(sighting)
                    for player_name, player_id in roster.items():
                        first_seen = self._players[player_name.casefold()]
                        first_seen[player_id] = min(day, first_seen.get(player_id, day))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue

    def resolve(self, name: str, cutoff: str) -> TeamHistory | None:
        candidates = self._names.get(team_key(name), ())
        identity = max(
            (sighting for sighting in candidates if sighting.day < cutoff),
            key=lambda sighting: (sighting.preferred, sighting.day),
            default=None,
        )
        if identity is None:
            return None

        earlier = sorted(
            (sighting for sighting in self._teams[identity.id] if sighting.day < cutoff),
            key=lambda sighting: sighting.day,
        )
        player_ids = {name: player for sighting in earlier for name, player in sighting.roster.items()}
        roster = next((sighting for sighting in reversed(earlier) if sighting.roster), None)
        latest = earlier[-1]
        return TeamHistory(
            identity.id, latest.day, latest.region, list(roster.roster) if roster else [],
            roster.day if roster else None, player_ids,
        )


    def lineup_ids(self, roster: Iterable[str], same_team: dict[str, str], cutoff: str) -> dict[str, str]:
        players = {name.casefold(): player for name, player in same_team.items()}
        for name in roster:
            key = name.casefold()
            if key in players:
                continue
            matches = [
                player for player, first_seen in self._players.get(key, {}).items()
                if first_seen < cutoff
            ]
            if len(matches) == 1:
                players[key] = matches[0]
        return players
