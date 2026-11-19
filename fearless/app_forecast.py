"""Historical frequency evidence when a trained forecast bundle is unavailable."""

from collections import Counter
from datetime import date, datetime, timezone


def _completed_day(game: dict) -> str:
    timestamp = game.get("finished_at")
    if timestamp:
        completed = datetime.fromisoformat(timestamp)
        if completed.tzinfo is not None:
            completed = completed.astimezone(timezone.utc)
        return completed.date().isoformat()
    return date.fromisoformat(game["date"]).isoformat()


class HistoricalFrequencies:
    def __init__(self, rows):
        self._games = {}
        for game in rows:
            if not game.get("eligible", True) or not game.get("finished", True):
                continue
            try:
                self._add(game)
            except (KeyError, TypeError, ValueError):
                continue

    def _add(self, game: dict) -> None:
        day = _completed_day(game)
        key = game.get("game_id") or (game["series_id"], game["game_number"])
        actions = {
            (action["kind"], action["champion"])
            for action in game["actions"] if action["champion"] is not None
        }
        roles = {
            (player["champion"], player["role"])
            for players in game.get("rosters", {}).values() for player in players
            if player.get("champion") and player.get("role") and player.get("role_source")
        }
        self._games[key] = (day, game.get("region", ""), actions, roles)

    def estimate(self, *, as_of: str, region: str, kind: str, legal: list[str]) -> dict:
        known = [game for game in self._games.values() if game[0] < as_of]
        regional = [game for game in known if region and game[1] == region]
        games = regional or known
        if not games:
            return {"choices": []}

        counts = Counter()
        roles = Counter()
        for _, _, actions, labels in games:
            counts.update(champion for action, champion in actions if action == kind)
            roles.update(labels)

        leaders = sorted(
            (champion for champion in legal if counts[champion]),
            key=lambda name: (-counts[name], name.casefold()),
        )[:3]
        return {
            "as_of": max(game[0] for game in games),
            "sample": len(games),
            "choices": [{
                "champion": champion,
                "score": None,
                "games": counts[champion],
                "roles": [role for (name, role), _ in roles.most_common() if name == champion],
                "reason": (
                    f"{'Picked' if kind == 'pick' else 'Banned'} in {counts[champion]} of "
                    f"{len(games)} earlier {'regional ' if regional else ''}professional games."
                ),
            } for champion in leaders],
        }
