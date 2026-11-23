"""Normalize a local GRID LoL archive and materialize pre-turn draft context."""

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import rules
from .grid import FeedGame, champion_name, identity, objects, ordinal, read_feed, snapshot_actions, spelling, timestamp


_SPLIT_1_2025 = "https://lolesports.com/en-US/news/lol-esports-in-2025"
_LATER_2025 = "https://lolesports.com/en-SG/news/fearless-draft-takes-over-2025"
_LEC_2026 = "https://cdn.sanity.io/files/dsfx7636/news_live/c9b1ec234a036d08d794c71fd3ab6710c8f46a78.pdf"
_LCS_2026 = "https://competitiveops.riotgames.com/en-US/library?GAME=lol"
_TIER_1_EVENTS = {
    **dict.fromkeys(("LCK - LCK Cup 2025", "LEC - Winter 2025", "LTA South - Split 1 2025",
                     "LTA North - Split 1 2025", "LTA Cross-Conference - Split 1 2025"), _SPLIT_1_2025),
    **dict.fromkeys(("LEC - Spring 2025", "LCK - Split 2 2025", "LTA South - Split 2 2025",
                     "LTA North - Split 2 2025", "LCK - Split 3 2025", "LTA South - Split 3 2025",
                     "LTA North - Split 3 2025", "LEC - Summer 2025",
                     "LTA Cross-Conference - Regional Championship 2025"), _LATER_2025),
    **dict.fromkeys(("LEC - LEC Versus 2026", "LEC - Spring 2026", "LEC - Summer 2026"), _LEC_2026),
    **dict.fromkeys(("LCS - Split 1 2026", "LCS - Split 2 2026", "LCS - Split 3 2026"), _LCS_2026),
    "LCS - Promotion 2026": "https://lolesports.com/en-US/news/lcs-promotion-tournament-wsci",
}
_REGIONS = {"LCK": "KR", "LEC": "EMEA", "LTA North": "NA", "LTA South": "LATAM",
            "LTA Cross-Conference": "Americas", "LCS": "NA", "LPL": "CN", "LCP": "APAC", "CBLOL": "BR"}
_ROLES = {"top": "top", "jungle": "jungle", "jgl": "jungle", "mid": "mid", "middle": "mid",
          "adc": "bot", "bot": "bot", "bottom": "bot", "support": "support", "sup": "support"}


def csv_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def lol_metadata(root, name):
    path = root / "meta" / (name + ".csv")
    if path.is_file():
        yield from (row for row in csv_rows(path) if row.get("title") == "lol")


@dataclass(frozen=True)
class RoleEvidence:
    role: str
    valid_from: date
    valid_to: date | None
    known_from: date
    source: str


def load_role_evidence(path):
    """Load dated evidence keyed by stable GRID team and player IDs."""
    evidence = defaultdict(list)
    for line, row in enumerate(csv_rows(Path(path)), 2):
        try:
            team, player = row["team_id"].strip(), row["player_id"].strip()
            role, source = _ROLES[spelling(row["role"])], row["source"].strip()
            valid_from = date.fromisoformat(row["valid_from"])
            valid_to = date.fromisoformat(row["valid_to"]) if row.get("valid_to") else None
            known_from = date.fromisoformat(row.get("known_from") or row["valid_from"])
            if not team or not player or not source or (valid_to and valid_to <= valid_from):
                raise ValueError
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid role evidence at {path}:{line}") from error
        evidence[team, player].append(RoleEvidence(role, valid_from, valid_to, known_from, source))
    return evidence


def effective_role(evidence, team, player, game_date, raw_roles):
    identifiers = [item.get("name") or item.get("id") if isinstance(item, dict) else item for item in raw_roles]
    explicit = {_ROLES[key] for identifier in identifiers if (key := spelling(identifier)) in _ROLES}
    if explicit:
        return (next(iter(explicit)), "GRID game snapshot") if len(explicit) == 1 else (None, None)
    if not game_date:
        return None, None
    when = date.fromisoformat(game_date)
    matching = [row for row in evidence.get((team, player), ())
                if row.valid_from <= when and row.known_from <= when and (row.valid_to is None or when < row.valid_to)]
    if len({row.role for row in matching}) != 1:
        return None, None
    return matching[0].role, sorted({row.source for row in matching})[0]


def rule_name(value):
    identifier = spelling(value)
    if identifier in {"fearless", "hardfearless", "fullfearless", "fearlessdraft", "hardfearlessdraft", "fullfearlessdraft", "true"}:
        return "fearless"
    if identifier in {"standard", "standarddraft", "classic", "false"}:
        return "standard"
    return "unknown"


def competition_rules(series, feed, overrides):
    if series["series_id"] in overrides:
        return overrides[series["series_id"]]
    explicit = feed.ruleset or series.get("draft_format") or series.get("fearless")
    if explicit:
        return rule_name(explicit), "GRID explicit draft format"
    event = series.get("event", "")
    if event in _TIER_1_EVENTS:
        return "fearless", _TIER_1_EVENTS[event]
    return "unknown", None


def load_rule_overrides(path):
    overrides = {}
    for line, row in enumerate(csv_rows(Path(path)), 2):
        series_id = identity(row.get("series_id"))
        ruleset = rule_name(row.get("ruleset"))
        source = (row.get("source") or "").strip()
        if not series_id or not source or ruleset == "unknown" or series_id in overrides:
            raise ValueError(f"Invalid series rules override at {path}:{line}")
        overrides[series_id] = ruleset, source
    return overrides


def read_metadata(root):
    games, rosters, characters = defaultdict(dict), defaultdict(list), {}
    for row in lol_metadata(root, "games"):
        games[row["series_id"]][row["game_id"]] = row
    for row in lol_metadata(root, "rosters"):
        rosters[row["series_id"], row["game_id"]].append(row)
    for row in lol_metadata(root, "characters"):
        name = row.get("name")
        if name:
            characters[spelling(name)] = name
            if row.get("character_id"):
                characters[spelling(row["character_id"])] = name
    return games, rosters, characters


def participants(game, metadata, series_teams):
    teams = {identity(team): {"name": team.get("name")} for team in series_teams if identity(team)}
    for index in (1, 2):
        prefix = f"team{index}_"
        team_id = identity(metadata.get(prefix + "id"))
        if team_id:
            teams[team_id] = {"name": metadata.get(prefix + "name"), "side": metadata.get(prefix + "side")}
    for team in objects(game.get("teams")):
        team_id = identity(team)
        if team_id:
            known = teams.setdefault(team_id, {})
            known.update({key: team[key] for key in ("name", "side") if team.get(key)})
    return teams


def game_rosters(raw, metadata, teams, characters, evidence, game_date):
    players = defaultdict(dict)
    for team in objects(raw.get("teams")):
        team_id = identity(team)
        for player in objects(team.get("players")):
            champion = champion_name(player.get("character"), characters)
            if team_id in teams and identity(player) and champion:
                players[team_id][identity(player)] = [player.get("name"), champion, player.get("roles") or []]
    for item in metadata:
        team, player = identity(item.get("team_id")), identity(item.get("player_id"))
        champion = champion_name({"id": item.get("character_id"), "name": item.get("character_name")}, characters)
        if team in teams and player and champion:
            entry = players[team].setdefault(player, [None, None, []])
            entry[0] = item.get("player_name") or entry[0]
            entry[1] = champion
    rosters = {team: [] for team in teams}
    for team, players_on_team in players.items():
        for player, (name, champion, raw_roles) in sorted(players_on_team.items()):
            role, source = effective_role(evidence, team, player, game_date, raw_roles)
            rosters[team].append({"player": player, "player_name": name, "champion": champion,
                                  "role": role, "role_source": source})
    return rosters


def patch_name(game, metadata):
    value = game.get("titleVersion") or game.get("patch") or metadata.get("patch")
    if isinstance(value, dict):
        value = value.get("name") or value.get("version")
    match = re.search(r"(?<!\d)(\d+\.\d+)", str(value or ""))
    return match[1] if match else None


def draft_evidence(game, characters):
    raw_snapshot = game.state.get("draftActions")
    snapshot, issues = snapshot_actions(raw_snapshot, characters) if isinstance(raw_snapshot, list) else ([], set())
    event_actions = [action for action, _ in game.events]
    full_snapshot = len(snapshot) == rules.DRAFT_LENGTH and not issues
    full_events = len(event_actions) == rules.DRAFT_LENGTH and not game.issues
    drafted_at = game.events[0][1] if event_actions and (not snapshot or event_actions[0] == snapshot[0]) else None
    if full_snapshot or snapshot and not full_events:
        if full_snapshot and full_events and snapshot != event_actions:
            issues.add("draft_sources_disagree")
        return snapshot, "snapshot", issues, drafted_at
    if event_actions:
        return event_actions, "events", set(game.issues), drafted_at
    return snapshot, "none", issues | game.issues, None


def corroborated_picks(actions, rosters):
    picks = [action for action in actions if action["kind"] == "pick"]
    picked = {(action["team"], action["champion"]) for action in picks}
    played = {(team, player["champion"]) for team, roster in rosters.items() for player in roster}
    if len(picks) != 10 or len({champion for _, champion in picked if champion}) != 10 or picked != played:
        return None
    if len(rosters) != 2 or any(len(roster) != 5 for roster in rosters.values()):
        return None
    return picks


def game_sort_time(game):
    value = game["drafted_at"] or game["started_at"] or game["date"]
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def normalize_series(series, feed, rolled_back, game_metadata, roster_metadata, characters, evidence, overrides, audit):
    series_id = series["series_id"]
    event = series.get("event", "")
    ruleset, rules_source = competition_rules(series, feed, overrides)
    fearless = ruleset == "fearless"
    region = series.get("region") or _REGIONS.get(event.partition(" - ")[0])
    games = {game_id: FeedGame() for game_id in game_metadata if game_id not in rolled_back}
    games.update(feed.games)
    ordered = []
    for game_id, game in games.items():
        metadata = game_metadata.get(game_id, {})
        if game_metadata and not metadata and game.state.get("finished") is not True and not game.finished_at:
            audit["unconfirmed_game_attempts"] += 1
            continue
        number = ordinal(game.state.get("sequenceNumber")) or ordinal(metadata.get("game_number"))
        if number is None or not 1 <= number <= 99:
            audit["games_without_number"] += 1
            continue
        ordered.append((number, game_id, game, metadata))
    numbers = Counter(number for number, *_ in ordered)
    audit["missing_game_numbers"] += len(set(range(1, max(numbers, default=0))) - numbers.keys())
    completed = {}
    for number, game_id, game, metadata in sorted(ordered):
        actions, draft_source, issues, drafted_at = draft_evidence(game, characters)
        teams = participants(game.state, metadata, feed.series_teams)
        action_teams = {action["team"] for action in actions if action["team"]}
        if len(teams) < 2:
            for team in action_teams:
                teams.setdefault(team, {})
        first = actions[0]["team"] if actions and actions[0]["kind"] == "ban" else None
        second = next((team for team in teams if team != first), None) if len(teams) == 2 and first in teams else None
        if not first or not second:
            issues.add("unknown_draft_positions")
        if numbers[number] > 1:
            issues.add("ambiguous_game_number")
        if draft_source == "events" and any(feed.counts[key] for key in (
                "unordered_envelopes", "malformed_envelopes", "unassigned_draft_events")):
            issues.add("incomplete_event_order")
        if len(actions) != rules.DRAFT_LENGTH:
            issues.add("incomplete_draft")
        finished = game.state.get("finished") is True or game.finished_at is not None or str(metadata.get("finished")).lower() == "true"
        if not finished:
            issues.add("unfinished_game")
        if game.state.get("forfeited") is True:
            issues.add("forfeited_game")
        prior = [completed[previous] for previous in range(1, number) if previous in completed]
        locked = rules.series_locks(prior, fearless=fearless)
        if first and second:
            pool = {action["champion"] for action in actions if action["champion"] is not None}
            for index, action in enumerate(actions):
                try:
                    rules.validate_action(pool, locked, actions[:index], first, second, **action)
                except ValueError:
                    issues.add("illegal_action")
                    break
        replay_verified = not issues
        if number > 1 and fearless and len(prior) != number - 1:
            issues.add("missing_prior_draft")
        if number > 1 and ruleset == "unknown":
            issues.add("unknown_series_rules")
        started_at = timestamp(game.state.get("startedAt")) or timestamp(metadata.get("started_at")) or game.started_at
        # Feed times describe the evidence. Post-start backfills cannot establish
        # an as-of draft boundary for historical roster features.
        if drafted_at and started_at and datetime.fromisoformat(drafted_at) > datetime.fromisoformat(started_at):
            drafted_at = None
        scheduled = timestamp(series.get("scheduled_start"))
        game_date = (drafted_at or started_at or scheduled or "")[:10] or None
        patch = patch_name(game.state, metadata)
        if not game_date:
            issues.add("unknown_game_date")
        if not patch:
            issues.add("unknown_patch")
        rosters = game_rosters(game.state, roster_metadata.get((series_id, game_id), ()), teams, characters, evidence, game_date)
        if replay_verified:
            completed[number] = actions
        elif fearless and finished and numbers[number] == 1 and not game.state.get("forfeited"):
            if played_picks := corroborated_picks(actions, rosters):
                completed[number] = played_picks
                audit["corroborated_prior_drafts"] += 1
        winners = {identity(t) for t in objects(game.state.get("teams")) if t.get("won") is True}
        if value := identity(metadata.get("winner_team_id")):
            winners.add(value)
        if len(winners) > 1 or winners - teams.keys():
            issues.add("conflicting_game_winner")
        winner = next(iter(winners)) if len(winners) == 1 and winners <= teams.keys() and finished else None
        yield {
            "series_id": series_id, "game_id": game_id, "game_number": number, "date": game_date,
            "patch": patch, "region": region, "event": event, "stage": series.get("stage"),
            "first": first, "second": second, "actions": actions, "rosters": rosters,
            "fearless": fearless, "ruleset": ruleset, "rules_source": rules_source,
            "winner": winner, "best_of": feed.best_of,
            "team_names": {team: fields.get("name") or team for team, fields in teams.items()},
            "sides": {team: fields.get("side") for team, fields in teams.items()},
            "drafted_at": drafted_at, "started_at": started_at, "finished_at": game.finished_at,
            "finished": finished, "draft_source": draft_source, "locked": sorted(locked),
            "eligible": not issues, "issues": sorted(issues),
        }


def import_grid(root, *, series_ids=None, roles=None, rule_overrides=None):
    """Return chronological normalized games and aggregate coverage counts."""
    root = Path(root).resolve()
    game_meta, roster_meta, characters = read_metadata(root)
    index = {}
    for series in csv_rows(root / "lol/series.csv"):
        key = series["series_id"]
        if key in index and series != index[key]:
            raise ValueError(f"Conflicting series index rows for {key}")
        index[key] = series
    selected = index.keys() if series_ids is None else set(series_ids)
    if missing := selected - index.keys():
        raise ValueError(f"Series ID absent from LoL index: {', '.join(sorted(missing))}")
    counts, issues, sources, regions, rulesets = (Counter() for _ in range(5))
    games = []
    for series_id in sorted(selected):
        series = index[series_id]
        feed_path = (root / series["file"]).resolve()
        if not feed_path.is_relative_to(root) or not feed_path.is_file():
            counts["missing_feeds"] += 1
            continue
        try:
            feed, rolled_back = read_feed(feed_path, series_id, characters)
        except (OSError, UnicodeError):
            counts["unreadable_feeds"] += 1
            continue
        counts["readable_feeds"] += 1
        counts.update(feed.counts)
        extracted = list(normalize_series(series, feed, rolled_back, game_meta.get(series_id, {}), roster_meta,
                                          characters, roles or {}, rule_overrides or {}, counts))
        if not extracted:
            counts["series_without_games"] += 1
        games.extend(extracted)
    games.sort(key=lambda game: (game_sort_time(game),
                                 game["series_id"], game["game_number"], game["game_id"]))
    for game in games:
        issues.update(game["issues"])
        sources[game["draft_source"]] += 1
        regions[game["region"] or "unknown"] += 1
        rulesets[game["ruleset"]] += 1
        counts["eligible_games"] += game["eligible"]
        counts["games_with_draft_timestamp"] += game["drafted_at"] is not None
        counts["games_with_finish_timestamp"] += game["finished_at"] is not None
        for roster in game["rosters"].values():
            counts["roster_rows"] += len(roster)
            counts["verified_role_rows"] += sum(row["role"] is not None for row in roster)
    audit = {
        **counts, "selected_series": len(selected), "normalized_games": len(games),
        "eligible_games": counts["eligible_games"], "ineligible_games": len(games) - counts["eligible_games"],
        "roster_rows": counts["roster_rows"], "verified_role_rows": counts["verified_role_rows"],
        "issue_counts": dict(issues), "draft_sources": dict(sources), "regions": dict(regions), "rulesets": dict(rulesets),
    }
    return games, audit


def turn_rows(games):
    """Yield legal training turns with context restricted to the pre-action state."""
    for game in games:
        if not game["eligible"]:
            continue
        base = {key: game[key] for key in ("series_id", "game_number", "date", "drafted_at", "patch", "region",
                                             "first", "second", "fearless", "locked")}
        for turn, action in enumerate(game["actions"]):
            context = {**base, "turn": turn, "team": action["team"], "kind": action["kind"],
                       "actions": game["actions"][:turn]}
            yield {"context": context, "label": action["champion"]}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        work = Path(stream.name)
        try:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.close()
            work.replace(path)
        finally:
            work.unlink(missing_ok=True)


def export_parquet(jsonl, parquet):
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError("Install duckdb to export Parquet") from error
    parquet.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as connection:
        connection.read_json(str(jsonl), format="newline_delimited").write_parquet(str(parquet))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-dir", type=Path, default=Path(os.environ.get("STL_DATA_DIR", "../stl-hackathon-2026-grid-starter")))
    parser.add_argument("--output", type=Path, default=Path(".local/training/games.jsonl"))
    parser.add_argument("--audit", type=Path, default=Path(".local/training/audit.json"))
    parser.add_argument("--series", action="append", help="GRID series ID. Repeat to select multiple series.")
    parser.add_argument("--roles", type=Path, help="Dated external role evidence CSV")
    parser.add_argument("--rules", type=Path, help="Series ID, ruleset and source override CSV")
    parser.add_argument("--turns", type=Path, help="Optional JSONL export of pre-turn contexts and labels")
    parser.add_argument("--parquet", type=Path, help="Optional DuckDB-backed Parquet export")
    args = parser.parse_args(argv)
    roles = load_role_evidence(args.roles) if args.roles else None
    overrides = load_rule_overrides(args.rules) if args.rules else None
    games, audit = import_grid(args.grid_dir, series_ids=args.series, roles=roles, rule_overrides=overrides)
    write_rows(args.output, games)
    write_rows(args.audit, [audit])
    if args.turns:
        write_rows(args.turns, turn_rows(games))
    if args.parquet:
        export_parquet(args.output, args.parquet)
    print(f"Normalized {audit['normalized_games']} games, with {audit['eligible_games']} eligible drafts. Audit written to {args.audit}")


if __name__ == "__main__":
    main()
