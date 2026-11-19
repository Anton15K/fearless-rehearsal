"""Build a preregistered draft holdout from Leaguepedia and Riot sources."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from time import sleep
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fearless.model_features import ChampionCatalog, evidence_date
from scripts.worlds_holdout import ALIASES, TABLES, keyed, parse_series

CARGO = "https://lol.fandom.com/api.php"
WIKI = "https://lol.fandom.com/wiki/"
EARLIEST = "2026-06-28"
STAGES = {
    "2026 Mid-Season Invitational": ("MSI 2026", "International"),
    "LCP/2026 Season/Split 3": ("LCP Split 3 2026", "APAC"),
    "LPL/2026 Season/Split 3": ("LPL Split 3 2026", "CN"),
    "LPL/2026 Season/Grand Finals": ("LPL Split 3 2026", "CN"),
    "LPL/2026 Season/Regional Finals": ("LPL Split 3 2026", "CN"),
}
RIOT_FIRST_SELECTION = "https://lolesports.com/en-US/lolesports/news/season-start-2026-lol-esports"
FEARLESS_SOURCES = {
    "International": [
        "https://lolesports.com/en-US/news/msi-2026-primer",
        "https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/8/81/MSI_2026_Rulebook.pdf/revision/latest?cb=20260615195447",
    ],
    "APAC": [
        "https://cdn.sanity.io/files/dsfx7636/news_live/3fbf7c285d72ce06f7b10888278eb52fd9f06877.pdf",
        "https://lolesports.com/en-SG/news/lcp-2026-season-",
    ],
    "CN": [
        "https://www.youtube.com/watch?v=ZtDu4MdkeP4",
        "https://weibo.com/lolesports",
    ],
}
EMEA_SUMMER = "EMEA Masters/2026 Season/Summer Main Event"
EMEA_RULEBOOK = "https://cdn.sanity.io/files/dsfx7636/news_live/0a6df7b5c91f3e5dff63371c1f6ba74c2a5f5e79.pdf"
LPL_SPLIT_2 = "LPL/2026 Season/Split 2 Playoffs"
TRANSFER_STAGES = {
    EMEA_SUMMER: ("EMEA Masters Summer 2026", "EMEA"),
    "CBLOL/2026 Season/Split 2 Playoffs": ("CBLOL Split 2 Playoffs 2026", "LATAM"),
    "LCP/2027 Season/Promotion": ("LCP Promotion 2026", "APAC"),
    LPL_SPLIT_2: ("LPL Split 2 Playoffs 2026", "CN"),
}
TRANSFER_RULES = {
    "EMEA": [
        "https://www.emea-competitiveops.com/",
        EMEA_RULEBOOK,
        "https://lolesports.com/en-AU/results/117171782811211421/stage/117171782812259999",
        "https://lolesports.com/en-US/results/117171782811211421/stage/117173038337693166",
    ],
    "LATAM": [
        "https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/7/7b/CBLOL_2026_Rulebook.pdf/revision/latest?cb=20260410173001",
        "https://www.youtube.com/watch?v=BtM8Duut2xU",
    ],
    "APAC": [
        "https://lolesports.com/en-PH/news/lcp-2026-promotion-series-everything-you-need-to-know",
        "https://cdn.sanity.io/files/dsfx7636/news_live/443a65d616d58bbe9189b47e83eb505b61eb6122.pdf",
        "https://cdn.sanity.io/files/dsfx7636/news_live/3fbf7c285d72ce06f7b10888278eb52fd9f06877.pdf",
    ],
    "CN": [
        "https://lolesports.com/en-AU/season/115547545029543948/handbook/115548016979679445/league/98767991314006698",
        "https://weibo.com/ttarticle/p/show?id=2309405305389620265001",
        "https://lol.fandom.com/wiki/LPL/2026_Season/Split_2_Playoffs",
    ],
}
ROLE_PAIR_STAGES = {
    "LCK/2026 Season/Season Play-In": ("LCK 2026 Postseason", "KR"),
    "LCK/2026 Season/Season Playoffs": ("LCK 2026 Postseason", "KR"),
    "LCK CL/2026 Season/Season Play-In": ("LCK CL 2026 Postseason", "KR"),
    "LCK CL/2026 Season/Season Playoffs": ("LCK CL 2026 Postseason", "KR"),
}
ROLE_PAIR_RULES = {
    "KR": [
        "https://lolesports.com/ko-KR/news/2026-lck-rulebook-update",
        "https://cmsassets.rgpub.io/sanity/files/dsfx7636/news_live/b5e4819daa2793425433910ca02b1c479b2f15a3.pdf?accountingTag=lol_esports",
    ],
}


@dataclass(frozen=True)
class Cohort:
    stages: dict[str, tuple[str, str]]
    fearless_sources: dict[str, list[str]]
    earliest: str
    before: str | None = None
    excluded_stages: dict[str, str] = field(default_factory=dict)
    previous_holdouts: int = 1
    query_limit: int = 500


ROUND_ONE = Cohort(STAGES, FEARLESS_SOURCES, EARLIEST)
TRANSFER = Cohort(
    TRANSFER_STAGES, TRANSFER_RULES, "2026-05-23", before="2026-11-19", previous_holdouts=2, query_limit=400,
    excluded_stages={EMEA_SUMMER: "Excluded by the first-pick coverage gate. The event has under 50 verifiable "
                                  "games and its archived draft order conflicts with the published blue-first rule."},
)
ROLE_PAIRS = Cohort(ROLE_PAIR_STAGES, ROLE_PAIR_RULES, "2026-08-24", before="2026-09-16",
                    previous_holdouts=3, query_limit=300)
COHORTS = {"round-one": ROUND_ONE, "transfer": TRANSFER, "role-pairs": ROLE_PAIRS}
TEAM_ALIASES = {name.casefold(): official for name, official in ALIASES.items()} | {
    "nongshim redforce": "NONGSHIM RED FORCE",
}
PATCH_LABEL = re.compile(r"\d+\.\d+")
PASSED_BANS = ("none", "no ban")
EXTERNAL = "leaguepedia:"


def wiki_page(name):
    return WIKI + quote(name.replace(" ", "_"))


def cargo_query(key, cohort=ROUND_ONE):
    table, fields = TABLES[key]
    pages = ",".join(f"'{stage}'" for stage in cohort.stages)
    return dict(action="cargoquery", format="json", tables=table, fields="OverviewPage," + fields,
                where=f"OverviewPage IN ({pages})", limit=cohort.query_limit)


def cached_table(directory, key, cohort=ROUND_ONE):
    path = directory / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    request = Request(CARGO + "?" + urlencode(cargo_query(key, cohort)),
                      headers={"User-Agent": "FearlessRehearsal/1.0 (registered holdout research)"})
    for delay in (2, 7, 15, 30, 60):
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code != 429:
                raise
        else:
            if "cargoquery" in payload:
                records = [entry["title"] for entry in payload["cargoquery"]]
                if not 0 < len(records) < cohort.query_limit:
                    raise ValueError(f"Unexpected row count in {key} Cargo table: {len(records)}")
                path.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
                return records
            if payload.get("error", {}).get("code") != "ratelimited":
                raise ValueError(f"Cargo request failed for {key}: {payload.get('error')}")
        sleep(delay)
    raise RuntimeError(f"Leaguepedia rate limited the {key} query. Cached tables can be reused.")


def team_alias(name):
    return TEAM_ALIASES.get(name.casefold(), name)


def prior_teams(archive):
    history = defaultdict(dict)
    for row in archive:
        if not (row.get("eligible") and row.get("finished")):
            continue
        day = evidence_date(row)
        for team_id, name in row.get("team_names", {}).items():
            first_seen = history[team_alias(name).casefold()]
            first_seen[team_id] = min(day, first_seen.get(team_id, day))
    return history


def team_ids(series, drafts, history):
    first_day = min(game["start"][:10] for game in series)
    names = {drafts[game["GameId"]][key] for game in series for key in ("Team1", "Team2")}
    known = {}
    for name in sorted(names, key=str):
        if not name or name != name.strip() or name.casefold().startswith(("tbd", "unknown")):
            raise ValueError("missing team identity")
        canonical_name = team_alias(name)
        identity = canonical_name.casefold()
        earlier = [team for team, day in history.get(identity, {}).items() if day < first_day]
        if len(earlier) > 1:
            raise ValueError(f"ambiguous team identity for {name}")
        known[identity] = known[name.casefold()] = earlier[0] if earlier else EXTERNAL + canonical_name
    return known


def fingerprint(teams, catalog):
    """Compare both teams' canonical picks and bans regardless of source order or IDs."""
    sides = []
    for picks, bans in teams:
        if len(picks) != 5 or any(not name for name in picks):
            return None, None
        canonical_picks = tuple(sorted(catalog.canonical(name) for name in picks))
        canonical_bans = tuple(sorted(catalog.canonical(name) if name and name.casefold() not in PASSED_BANS
                                      else "" for name in bans))
        sides.append((canonical_picks, canonical_bans))
    pick_signature = tuple(sorted(picks for picks, _ in sides))
    complete = all(len(bans) == 5 for _, bans in sides)
    return tuple(sorted(sides)) if complete else None, pick_signature


def source_fingerprints(row, catalog):
    teams = [([row.get(f"Team{side}Pick{n}") for n in range(1, 6)],
              [row[f"Team{side}Ban{n}"] for n in range(1, 6) if row.get(f"Team{side}Ban{n}")])
             for side in (1, 2)]
    return fingerprint(teams, catalog)


def archive_fingerprints(row, catalog):
    teams = [([a["champion"] for a in row.get("actions", ()) if (a["team"], a["kind"]) == (team, "pick")],
              [a["champion"] for a in row.get("actions", ()) if (a["team"], a["kind"]) == (team, "ban")])
             for team in (row.get("first"), row.get("second"))]
    return fingerprint(teams, catalog)


def overlaps(source, reference, catalog):
    games, series = {}, {}
    full_drafts, partial_picks, all_picks = {}, {}, {}
    for row in reference:
        game_id = row.get("GameId") or row.get("game_id")
        if not game_id:
            continue
        games[game_id] = game_id
        if series_id := row.get("series_id"):
            series[series_id] = game_id
        full, picks = archive_fingerprints(row, catalog)
        if picks:
            all_picks[picks] = game_id
            (full_drafts if full else partial_picks)[full or picks] = game_id
    matched = {}
    for row, full, picks in source:
        duplicate = (games.get(row["GameId"]) or series.get(row["MatchId"]) or full_drafts.get(full)
                     or (partial_picks if full else all_picks).get(picks))
        if duplicate:
            matched[row["GameId"]] = duplicate
    return matched, dict(games=len(reference), game_ids=len(games), complete_draft_fingerprints=len(full_drafts),
                         pick_only_fingerprints=len(partial_picks), matched_source_games=len(matched))


def normalize(tables, archive, previous, patches, *, cohort=ROUND_ONE, additional_previous=()):
    for key, records in tables.items():
        if any(row["OverviewPage"] not in cohort.stages for row in records):
            raise ValueError(f"Unexpected stage outside the preregistered {key} table scope")
    score, durations, drafts, selections = [
        keyed(tables[key], "GameId") for key in ("score", "duration", "draft", "schedule")
    ]
    metadata = keyed(tables["series"], "MatchId")
    games = defaultdict(list)
    for key, row in score.items():
        sid, stage = row["MatchId"], row["OverviewPage"]
        if sid not in metadata or metadata[sid]["OverviewPage"] != stage:
            raise ValueError(f"Scoreboard series is absent from the registered stage schedule: {sid}")
        for table in (durations, drafts, selections):
            if key in table and table[key]["OverviewPage"] != stage:
                raise ValueError(f"Source stage disagrees across tables for {key}")
        games[sid].append({**row, "duration": durations.get(key, {}).get("duration")})
    if set(drafts) - set(score):
        raise ValueError("The draft table contains games absent from the scoreboard")
    catalog = ChampionCatalog.from_json(patches)
    source = [(row, *source_fingerprints(drafts.get(key, {}), catalog)) for key, row in score.items()]
    history = prior_teams(archive)
    matched, audit = {}, {}
    references = {"archive": archive, "previous": previous}
    references.update({f"previous_{index}": rows for index, rows in enumerate(additional_previous, 2)})
    for label, reference in references.items():
        matched[label], audit[label] = overlaps(source, reference, catalog)
    # Every member of an internal duplicate group is suspect.
    by_draft = defaultdict(list)
    for row, full, _ in source:
        if full:
            by_draft[full].append(row["GameId"])
    duplicate_sources = {game: sorted(group) for group in by_draft.values() if len(group) > 1 for game in group}
    audit["source"] = dict(games=len(source), complete_draft_fingerprints=len(by_draft),
                           matched_source_games=len(duplicate_sources))

    rows, excluded = [], {}
    for sid, meta in metadata.items():
        stage = meta["OverviewPage"]
        if stage in cohort.excluded_stages:
            excluded[sid] = cohort.excluded_stages[stage]
            continue
        event, region = cohort.stages[stage]
        series_games = games[sid]
        try:
            if not series_games:
                raise ValueError("missing series games")
            if meta["BestOf"] not in ("3", "5"):
                raise ValueError("unsupported series format")
            for game in series_games:
                key = game["GameId"]
                if key in duplicate_sources:
                    raise ValueError(f"duplicate source draft for {key}: {', '.join(duplicate_sources[key])}")
                for label, matches in matched.items():
                    if key in matches:
                        raise ValueError(f"overlap with {label} game {matches[key]} for {key}")
                if not PATCH_LABEL.fullmatch(game.get("Patch") or ""):
                    raise ValueError(f"missing competitive patch for {key}")
            known = team_ids(series_games, drafts, history)
            records = parse_series(meta, series_games, drafts, selections, patches, known,
                                   earliest=cohort.earliest, region=region)
            if cohort.before and any(evidence_date(game) >= cohort.before for game in records):
                raise ValueError("series completes outside the registered period")
            for game in records:
                draft = drafts[game["game_id"]]
                game.update(event=event, stage=stage,
                            team_names={game["first"]: draft["Team1"], game["second"]: draft["Team2"]})
            if any(prior["finished_at"] > next_game["drafted_at"] for prior, next_game in zip(records, records[1:])):
                raise ValueError("inconsistent chronology within series")
        except (ValueError, KeyError, TypeError) as exc:
            excluded[sid] = str(exc)
        else:
            rows.extend(records)
    rows.sort(key=lambda row: (row["drafted_at"], row["series_id"], row["game_number"]))
    stage_report = {}
    for stage, (event, region) in cohort.stages.items():
        accepted = [game for game in rows if game["stage"] == stage]
        team_names = {team: name for game in accepted for team, name in game["team_names"].items()}
        external_teams = {team: wiki_page(team.removeprefix(EXTERNAL)) for team in team_names if team.startswith(EXTERNAL)}
        stage_patches = sorted({game["patch"] for game in accepted}, key=lambda patch: tuple(map(int, patch.split("."))))
        stage_report[stage] = dict(
            event=event, region=region, source_page=wiki_page(stage), fearless_sources=cohort.fearless_sources[region],
            source_games=sum(row["OverviewPage"] == stage for row in score.values()),
            source_series=sum(row["OverviewPage"] == stage for row in metadata.values()),
            games=len(accepted), series=len({game["series_id"] for game in accepted}),
            patches=stage_patches, patch_range=[stage_patches[0], stage_patches[-1]] if stage_patches else [],
            archive_team_ids=len(team_names) - len(external_teams), external_team_ids=len(external_teams),
            external_team_sources=external_teams,
        )
        if cohort is not ROUND_ONE:
            source_keys = [key for key, row in score.items() if row["OverviewPage"] == stage]
            stage_report[stage]["first_pick"] = dict(
                source_recorded=sum(bool(selections.get(key, {}).get("FirstPick")) for key in source_keys),
                accepted_recorded=len(accepted),
            )
            if stage in cohort.excluded_stages:
                stage_report[stage]["exclusion"] = cohort.excluded_stages[stage]
                stage_report[stage]["draft_team1_disagrees_with_blue"] = sum(
                    drafts[key]["Team1"] != selections[key]["Blue"]
                    for key in source_keys if key in drafts and key in selections
                )
    return rows, dict(
        source_games=len(score), source_series=len(metadata), games=len(rows),
        series=len({game["series_id"] for game in rows}), excluded=excluded, stages=stage_report, overlap=audit,
        dates=[rows[0]["date"], rows[-1]["date"]] if rows else [],
    )


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "catalog", "cache", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous", type=Path, action="append", required=True, help="External reference JSONL, repeatable")
    parser.add_argument("--cohort", choices=COHORTS, default="round-one")
    args = parser.parse_args()
    cohort = COHORTS[args.cohort]
    if len({path.resolve() for path in args.previous}) != len(args.previous):
        parser.error("Each previous holdout must be distinct.")
    if len(args.previous) < cohort.previous_holdouts:
        parser.error(f"The {args.cohort} cohort requires {cohort.previous_holdouts} previous holdouts.")
    input_paths = {"archive": args.archive, "previous": args.previous[0], "catalog": args.catalog}
    input_paths.update({f"previous_{index}": path for index, path in enumerate(args.previous[1:], 2)})
    args.cache.mkdir(parents=True, exist_ok=True)
    tables = {key: cached_table(args.cache, key, cohort) for key in TABLES}
    for key, records in tables.items():
        if {row["OverviewPage"] for row in records} != cohort.stages.keys():
            raise ValueError(f"Incomplete preregistered stage sets in {key} Cargo table")
    patches = json.loads(args.catalog.read_text(encoding="utf-8"))
    previous, *additional_previous = map(read_jsonl, args.previous)
    rows, report = normalize(tables, read_jsonl(args.archive), previous, patches, cohort=cohort,
                             additional_previous=additional_previous)
    if not rows:
        raise ValueError("No complete eligible series")
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    report.update(
        source="Leaguepedia, CC BY-SA 3.0",
        provenance=dict(
            cargo_url=CARGO, cargo_queries={key: cargo_query(key, cohort) for key in TABLES},
            first_selection_rules=RIOT_FIRST_SELECTION,
            fearless_replay="Both teams' prior completed picks are unavailable in subsequent games.",
            timestamps="ScoreboardGames UTC start and game length; draft and completion time estimates.",
            team_ids="Earlier eligible, completed GRID alias matches or unique Leaguepedia team page titles.",
            catalog={patch: f"https://ddragon.leagueoflegends.com/cdn/{patch}.1/data/en_US/champion.json"
                     for patch in patches},
        ),
        hashes={f"{key}.json": sha256((args.cache / f"{key}.json").read_bytes()).hexdigest() for key in TABLES},
        input_hashes={label: sha256(path.read_bytes()).hexdigest() for label, path in input_paths.items()},
        sha256=sha256(data).hexdigest(),
    )
    if args.cohort != "round-one":
        report["cohort"] = args.cohort
    if args.cohort == "transfer":
        report["fallback"] = {
            "coverage_threshold_games": 50,
            "excluded_stage": EMEA_SUMMER,
            "replacement_stage": LPL_SPLIT_2,
            "replacement_start_date": TRANSFER.earliest,
        }
    for path in (args.output, args.audit):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    args.audit.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{report['games']}/{report['source_games']} games, {report['series']}/{report['source_series']} series")


if __name__ == "__main__":
    main()
