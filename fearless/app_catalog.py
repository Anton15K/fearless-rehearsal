"""Local match settings and a deliberately small, entirely authored demo."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from fearless.app_directory import TeamDirectory, team_key
from fearless.app_forecast import HistoricalFrequencies
from fearless.app_store import RehearsalError
from fearless.rules import DRAFT_LENGTH, DraftAction, schedule, series_locks, validate_action


DEMO_ID = "poppy-game-three"
PROSPECTIVE_ID = "prospective"
HISTORICAL_SERIES = {"3000378": "Bo5", "994442840205359781": "Bo3"}
DEMO_PATCH = "16.17"
DEMO_TEAMS = ("Cloud9 Kia", "LYON")
DEMO_POOL = tuple(sorted({
    "Aatrox", "Ahri", "Akali", "Akshan", "Alistar", "Ambessa", "Amumu", "Anivia", "Annie",
    "Aphelios", "Ashe", "Aurelion Sol", "Aurora", "Azir", "Bard", "Bel'Veth", "Blitzcrank",
    "Brand", "Braum", "Briar", "Caitlyn", "Camille", "Cassiopeia", "Cho'Gath", "Corki",
    "Darius", "Diana", "Dr. Mundo", "Draven", "Ekko", "Elise", "Evelynn", "Ezreal",
    "Fiddlesticks", "Fiora", "Fizz", "Galio", "Gangplank", "Garen", "Gnar", "Gragas",
    "Graves", "Gwen", "Hecarim", "Heimerdinger", "Hwei", "Illaoi", "Irelia", "Ivern",
    "Janna", "Jarvan IV", "Jax", "Jayce", "Jhin", "Jinx", "K'Sante", "Kai'Sa",
    "Kalista", "Karma", "Karthus", "Kassadin", "Katarina", "Kayle", "Kayn", "Kennen",
    "Kha'Zix", "Kindred", "Kled", "Kog'Maw", "LeBlanc", "Lee Sin", "Leona", "Lillia",
    "Lissandra", "Locke", "Lucian", "Lulu", "Lux", "Malphite", "Malzahar", "Maokai", "Master Yi",
    "Mel", "Milio", "Miss Fortune", "Mordekaiser", "Morgana", "Naafiri", "Nami", "Nasus",
    "Nautilus", "Neeko", "Nidalee", "Nilah", "Nocturne", "Nunu & Willump", "Olaf",
    "Orianna", "Ornn", "Pantheon", "Poppy", "Pyke", "Qiyana", "Quinn", "Rakan", "Rammus",
    "Rek'Sai", "Rell", "Renata Glasc", "Renekton", "Rengar", "Riven", "Rumble", "Ryze",
    "Samira", "Sejuani", "Senna", "Seraphine", "Sett", "Shaco", "Shen", "Shyvana",
    "Singed", "Sion", "Sivir", "Skarner", "Smolder", "Sona", "Soraka", "Swain",
    "Sylas", "Syndra", "Tahm Kench", "Taliyah", "Talon", "Taric", "Teemo", "Thresh",
    "Tristana", "Trundle", "Tryndamere", "Twisted Fate", "Twitch", "Udyr", "Urgot",
    "Varus", "Vayne", "Veigar", "Vel'Koz", "Vex", "Vi", "Viego", "Viktor", "Vladimir",
    "Volibear", "Warwick", "Wukong", "Xayah", "Xerath", "Xin Zhao", "Yasuo", "Yone",
    "Yorick", "Yunara", "Yuumi", "Zaahen", "Zac", "Zed", "Zeri", "Ziggs", "Zilean",
    "Zoe", "Zyra",
}))


@dataclass(frozen=True)
class Match:
    metadata: dict
    region: str
    team_ids: dict[str, str]
    player_ids: dict[str, dict[str, str]]
    previous: list[list[DraftAction]]
    historical: dict[int, dict]
    opening: list[DraftAction]
    coach_team: str


def _sequence(first: str, second: str, champions: list[str | None]) -> list[DraftAction]:
    return [
        {"team": team, "kind": kind, "champion": champion}
        for (team, kind), champion in zip(schedule(first, second), champions)
    ]


def _demo() -> Match:
    cloud9, lyon = DEMO_TEAMS
    rosters = {
        cloud9: ["Thanatos", "Blaber", "Loki", "Tactical", "Vulcan"],
        lyon: ["Dhokla", "Inspired", "Saint", "Berserker", "Isles"],
    }
    return Match(
        metadata={
            "id": DEMO_ID, "title": f"{cloud9} vs {lyon}", "teams": list(DEMO_TEAMS),
            "source": "illustrative", "date": "2026-10-03", "patch": DEMO_PATCH,
            "format": "Bo5", "rosters": rosters, "first": lyon,
        },
        region="NA",
        team_ids={team: team for team in DEMO_TEAMS},
        player_ids={},
        previous=[_sequence(cloud9, lyon, [
            "Vi", "Jax", "Rumble", "Ashe", "Kalista", "Azir", "Xin Zhao", "Gwen", "Lucian",
            "K'Sante", "Orianna", "Syndra", "Jarvan IV", "Varus", "Renekton", "Ahri", "Milio",
            "Ezreal", "Karma", "Nidalee",
        ])],
        historical={},
        opening=_sequence(lyon, cloud9, ["Aurora", "Yone", "Tristana", "Rumble", "Skarner"]),
        coach_team=cloud9,
    )


def _series_matches(games: list[dict], catalog: dict[str, tuple[str, ...]]) -> list[Match]:
    grouped = defaultdict(list)
    for row in games:
        series = str(row.get("series_id", ""))
        if (
            series in HISTORICAL_SERIES and row.get("eligible", True)
            and row.get("finished", True) and row.get("fearless", True)
        ):
            grouped[series].append(row)

    matches = []
    for series_id, rows in grouped.items():
        try:
            match = _series_match(series_id, rows, catalog)
        except (KeyError, TypeError, ValueError):
            continue
        if match is not None:
            matches.append(match)
    return matches


def _series_match(series_id: str, rows: list[dict], catalog: dict[str, tuple[str, ...]]) -> Match | None:
    rows.sort(key=lambda row: row["game_number"])
    if len(rows) < 2 or [row["game_number"] for row in rows] != list(range(1, len(rows) + 1)):
        return None
    earlier, opening = rows[:2]
    team_ids = {earlier["team_names"][team]: team for team in (earlier["first"], earlier["second"])}
    if len(team_ids) != 2:
        return None
    team_names = {team: name for name, team in team_ids.items()}
    replay = {}
    completed = []
    for row in rows:
        first, second = team_names[row["first"]], team_names[row["second"]]
        pool = catalog.get(row["patch"], DEMO_POOL)
        locked = series_locks(completed)
        actions = []
        for action in row["actions"]:
            team, kind, champion = team_names[action["team"]], action["kind"], action["champion"]
            validate_action(pool, locked, actions, first, second, team, kind, champion)
            actions.append({"team": team, "kind": kind, "champion": champion})
        if len(actions) != DRAFT_LENGTH or row["patch"] != opening["patch"]:
            return None
        replay[row["game_number"]] = {"first": first, "actions": actions}
        completed.append(actions)

    # Game 1 is already complete. Its lineups are available at this starting position.
    players = {}
    for team, roster in earlier.get("rosters", {}).items():
        if team in team_names:
            players[team_names[team]] = {
                player["player_name"].strip(): str(player["player"])
                for player in roster if player.get("player_name") and player.get("player")
            }
    game_format = earlier.get("format", HISTORICAL_SERIES[series_id])
    if game_format not in {"Bo3", "Bo5"} or len(completed) > int(game_format[-1]):
        return None
    coach = next((name for name in team_ids if "cloud9" in name.lower()), next(iter(team_ids)))
    teams = [coach, next(name for name in team_ids if name != coach)]
    return Match(
        metadata={
            "id": series_id, "title": " vs ".join(teams), "teams": teams,
            "source": "historical", "date": date.fromisoformat(opening["date"]).isoformat(),
            "patch": opening["patch"], "format": game_format, "first": replay[2]["first"],
            "rosters": {team: list(roster) for team, roster in players.items()},
            "provenance": opening.get("source"),
        },
        region=opening.get("region", ""),
        team_ids=team_ids,
        player_ids=players,
        previous=[replay.pop(1)["actions"]],
        historical=replay,
        opening=[],
        coach_team=coach,
    )


class MatchCatalog:
    def __init__(self, games: Path | None, champions: Path | None, public_games: Path | None = None):
        raw = json.loads(champions.read_text(encoding="utf8")) if champions else {}
        self.patches = {
            patch: tuple(sorted(set(names), key=str.casefold))
            for patch, names in raw.items()
        }
        self.matches = {DEMO_ID: _demo()}
        history = []
        self.teams = TeamDirectory()
        for path in dict.fromkeys((games, public_games)):
            if path is None:
                continue
            with path.open(encoding="utf8") as source:
                rows = [json.loads(line) for line in source if line.strip()]
            self.matches.update({
                match.metadata["id"]: match for match in _series_matches(rows, self.patches)
            })
            self.teams.add(rows, preferred=path == games)
            history.extend(rows)
        self.frequencies = HistoricalFrequencies(history)

    def pool(self, match: Match, patch: str) -> tuple[str, ...] | None:
        if match.metadata["source"] == PROSPECTIVE_ID:
            return self.patches.get(patch) or (DEMO_POOL if patch == DEMO_PATCH else None)
        if patch == DEMO_PATCH and match.metadata["source"] == "illustrative":
            return DEMO_POOL
        pool = self.patches.get(patch)
        if pool is None and patch == match.metadata["patch"]:
            return DEMO_POOL
        return pool

    def prospective(self, settings: dict) -> Match:
        submitted = settings.get("teams")
        if not isinstance(submitted, list) or len(submitted) != 2 or any(
            not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 for name in submitted
        ):
            raise RehearsalError(422, "Enter two team names.")
        teams = [name.strip() for name in submitted]
        if team_key(teams[0]) == team_key(teams[1]):
            raise RehearsalError(422, "Use a different name for each team.")
        coach, first = settings.get("coach_team"), settings.get("first")
        if coach not in teams or first not in teams:
            raise RehearsalError(422, "Choose a coach side and first pick from the matchup.")

        game_format = settings.get("format")
        if game_format not in ("Bo3", "Bo5"):
            raise RehearsalError(422, "Choose a best-of-three or best-of-five series.")
        patch = settings.get("patch")
        if not isinstance(patch, str) or patch not in {*self.patches, DEMO_PATCH}:
            raise RehearsalError(422, "Choose a patch from the champion catalog.")
        try:
            as_of = date.fromisoformat(settings.get("date", "")).isoformat()
        except (TypeError, ValueError) as error:
            raise RehearsalError(422, "Choose a valid as-of date.") from error

        resolved = {team: self.teams.resolve(team, as_of) for team in teams}
        ids = {team: history.id if history else f"prospective:{team_key(team)}" for team, history in resolved.items()}
        if len(set(ids.values())) != 2:
            raise RehearsalError(422, "Both names refer to the same team in earlier games.")
        regions = [resolved[team].region for team in teams if resolved[team] is not None and resolved[team].region]
        region = regions[0] if regions and len(set(regions)) == 1 else ""
        return Match(
            metadata={
                "id": PROSPECTIVE_ID, "source": PROSPECTIVE_ID, "title": " vs ".join(teams),
                "teams": teams, "first": first, "date": as_of, "format": game_format, "patch": patch,
                "rosters": {team: history.roster for team, history in resolved.items() if history and history.roster},
                "team_history": {team: {"as_of": history.as_of} if history else None for team, history in resolved.items()},
                "roster_dates": {team: history.roster_as_of for team, history in resolved.items() if history},
            },
            region=region, team_ids=ids,
            player_ids={team: history.player_ids for team, history in resolved.items() if history},
            previous=[], historical={}, opening=[], coach_team=coach,
        )

    def bootstrap(self) -> dict:
        matches = [match.metadata for match in self.matches.values()]
        champions = set(DEMO_POOL)
        for pool in self.patches.values():
            champions.update(pool)
        return {
            "champions": sorted(champions, key=str.casefold),
            "matches": matches,
            "default_match": DEMO_ID,
            "patches": sorted(
                {*self.patches, DEMO_PATCH},
                key=lambda patch: tuple(int(part) if part.isdigit() else -1 for part in patch.split(".")),
                reverse=True,
            ),
        }
