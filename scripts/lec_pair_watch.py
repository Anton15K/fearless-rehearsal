"""Check once for the LEC Winter 2027 opener and seal a private Game 1 pilot."""
import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fearless.app_directory import TeamDirectory
from fearless.event_forecaster import digest
from fearless.event_series import read_event_games
from fearless.model_features import ChampionCatalog
from fearless.pair_trial import PairTrial, fingerprint, identities
from scripts.event_drafts import CARGO, team_alias
from scripts.pair_pilot import make_envelope, utc_now, write_exclusive
from scripts.pair_trial import load_freeze, read


STAGE = "LEC/2027 Season/Winter Season"
EVENT = "LEC Winter 2027"
OFFICIAL = "https://lolesports.com/en-GB/lolesports/news/2027-lec-a-look-at-the-year-ahead"
TIMING = "https://www.twitch.tv/lec"
WIKI = "https://lol.fandom.com/wiki/"
DRAGON = "https://ddragon.leagueoflegends.com"
VERSIONS = f"{DRAGON}/api/versions.json"
OPENING = date(2027, 1, 15)
END = datetime(2027, 1, 16, tzinfo=timezone.utc)
LOOKAHEAD = timedelta(days=2)
BUFFER = timedelta(minutes=30)
PATCHES = ((16, 24), (17, 1), (17, 2))
POOL_LIMIT = 2
ROW_LIMIT = 12
STAGE_PATTERN = re.compile(r"LEC/2027 Season/Winter(?: Season| Split)?(?:/Regular Season)?", re.IGNORECASE)


class Pending(Exception):
    def __init__(self, reason, detail, *, status="pending"):
        super().__init__(detail)
        self.reason = reason
        self.status = status


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "FearlessRehearsal/1.0 (LEC opener schedule and patch check)"})
    with urlopen(request, timeout=8) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("A public source exceeded the response-size limit.")
    return json.loads(body)


def _schedule_source(stage):
    # Stage names come only from the canonical constant or the validated Wiki lookup.
    where = (f"OverviewPage='{stage}' AND DateTime_UTC >= '2027-01-15 00:00:00'"
             " AND DateTime_UTC < '2027-01-16 00:00:00'")
    return CARGO + "?" + urlencode({
        "action": "cargoquery", "format": "json", "tables": "MatchSchedule",
        "fields": "OverviewPage,MatchId,Team1,Team2,DateTime_UTC,HasTime,BestOf,Patch,LegacyPatch,DisabledChampions,IsNullified",
        "where": where, "order_by": "DateTime_UTC,MatchId", "limit": ROW_LIMIT,
    })


def _schedule(stage, fetch):
    source = _schedule_source(stage)
    data = fetch(source)
    wrapped = data.get("cargoquery") if isinstance(data, dict) else None
    if not isinstance(wrapped, list) or len(wrapped) >= ROW_LIMIT:
        raise Pending("schedule_feed_incomplete", "Leaguepedia did not return a bounded complete opening-day schedule.")
    rows = [entry.get("title") for entry in wrapped if isinstance(entry, dict)]
    if len(rows) != len(wrapped) or any(not isinstance(row, dict) or row.get("OverviewPage") != stage for row in rows):
        raise Pending("schedule_feed_incomplete", "Leaguepedia returned an unexpected schedule row.")
    return {"stage": stage, "source": source, "rows": rows}


def discover_schedule(fetch):
    canonical = _schedule(STAGE, fetch)
    if canonical["rows"]:
        return canonical
    source = CARGO + "?" + urlencode({
        "action": "query", "format": "json", "titles": STAGE, "redirects": 1,
        "prop": "links", "plnamespace": 0, "pllimit": 100,
    })
    data = fetch(source)
    query = data.get("query") if isinstance(data, dict) else None
    if not isinstance(query, dict) or not isinstance(query.get("pages"), dict):
        raise Pending("schedule_lookup_unavailable", "Leaguepedia did not verify the canonical Winter overview.")
    roots = {STAGE}
    for item in query.get("redirects", ()):
        if item.get("from") in roots and _winter_stage(item.get("to")):
            roots.add(item["to"])
    alternatives = roots - {STAGE}
    for page in query["pages"].values():
        if page.get("title") in roots and "missing" not in page:
            alternatives.update(link["title"] for link in page.get("links", ())
                                if _winter_stage(link.get("title")) and link["title"] != STAGE)
    if len(alternatives) > 2:
        raise Pending("schedule_stage_ambiguous", "The canonical overview links to more than two possible Winter stages.")
    candidates = [_schedule(stage, fetch) for stage in sorted(alternatives)]
    usable = [match for match in candidates if match["rows"]]
    if len(usable) > 1:
        raise Pending("schedule_stage_ambiguous", "Multiple evidenced Winter stages list opening-day fixtures.")
    return {**(usable[0] if usable else canonical), "lookup_source": source, "lookup_sha256": fingerprint(data)}


def _winter_stage(value):
    return isinstance(value, str) and STAGE_PATTERN.fullmatch(value) is not None


def _as_utc(value):
    timestamp = datetime.fromisoformat(value)
    # This adapter reads the explicitly UTC Cargo field.
    return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)


def _known_name(value):
    return (isinstance(value, str) and value.strip() == value and bool(value)
            and not re.match(r"(?i)^(?:tbd\b|tba\b|unknown\b|to be\b|winner of\b|loser of\b|team [12]$|\?)", value))


def opening_match(rows, stage, *, now):
    if now.tzinfo is None:
        raise ValueError("The watcher clock needs a time zone.")
    now = now.astimezone(timezone.utc)
    active = [row for row in rows if str(row.get("IsNullified", "")).lower() not in ("1", "true", "yes")]
    if not active:
        raise Pending("schedule_unpublished", "Leaguepedia lists no confirmed Winter opening-day schedule yet.")
    try:
        starts = [(_as_utc(row["DateTime_UTC"]), row) for row in active]
    except (KeyError, TypeError, ValueError) as error:
        raise Pending("schedule_unconfirmed", "The opening schedule contains an unconfirmed start time.") from error
    starts = [(when, row) for when, row in starts if when.date() == OPENING]
    if not starts:
        raise Pending("schedule_unpublished", "Leaguepedia lists no match on Riot's confirmed Winter opening date.")
    earliest = min(when for when, _ in starts)
    first = [row for when, row in starts if when == earliest]
    if len(first) != 1:
        raise Pending("opening_fixture_ambiguous", "The opening time does not identify a unique first series.")
    row = first[0]
    teams = row.get("Team1"), row.get("Team2")
    match_id = row.get("MatchId")
    if (row.get("OverviewPage") != stage or not isinstance(match_id, str) or not match_id.startswith(stage + "_")
            or str(row.get("HasTime", "")).lower() not in ("1", "true", "yes") or str(row.get("BestOf")) != "3"
            or not all(map(_known_name, teams)) or teams[0].casefold() == teams[1].casefold()):
        raise Pending("opening_fixture_unconfirmed", "The earliest row has an unconfirmed series, UTC time, format or team.")
    deadline = earliest - BUFFER
    if now >= deadline:
        raise Pending("draft_deadline_passed", "The deadline passed for the opening fixture.", status="missed")
    if earliest - now > LOOKAHEAD:
        raise Pending("outside_lookahead", "The confirmed opener is outside the 48-hour lookahead.")
    return {"row": row, "starts_at": earliest, "deadline": deadline}


def _patch_key(major, minor):
    return (major - 10 if 25 <= major <= 29 else major, minor)


def _patch_label(value):
    if value is None or str(value).strip().casefold() in ("", "?", "tbd", "tba", "unknown"):
        return None
    match = re.fullmatch(r"(?:Patch\s+)?(\d+)\.(\d+)(?:\.\d+)?", str(value).strip(), re.IGNORECASE)
    if not match:
        raise Pending("listed_patch_unrecognized", "The listed match patch could not be verified.")
    return _patch_key(*map(int, match.groups()))


def _label(patch):
    return ".".join(map(str, patch))


def _disabled(row):
    raw = row.get("DisabledChampions")
    if raw is None or raw == "":
        return [], "unannounced_assume_none_for_seal"
    if isinstance(raw, str):
        raw = [name.strip() for name in raw.split(",") if name.strip()]
    if not isinstance(raw, list) or any(not _known_name(item) for item in raw):
        raise Pending("disables_unconfirmed", "The opening schedule's champion disables are unconfirmed.")
    if len(raw) == 1 and raw[0].casefold() in ("none", "no disables"):
        return [], "none_listed"
    return raw, "listed" if raw else "unannounced_assume_none_for_seal"


def _dragon_pool(fetch, version):
    url = f"{DRAGON}/cdn/{version}/data/en_US/champion.json"
    payload = fetch(url)
    if (not isinstance(payload, dict) or payload.get("version") != version or payload.get("type") != "champion"
            or not isinstance(payload.get("data"), dict) or len(payload["data"]) < 20):
        raise Pending("patch_feed_incomplete", "Data Dragon did not return an exact, complete champion catalogue.")
    names = [row.get("name") if isinstance(row, dict) else None for row in payload["data"].values()]
    if any(not _known_name(name) for name in names):
        raise Pending("patch_feed_incomplete", "Data Dragon returned an invalid champion identity.")
    return names, {"version": version, "source": url, "sha256": fingerprint(payload)}


def resolve_patches(fetch, row, catalog, base_pools):
    listed = {_patch_label(row.get(field)) for field in ("Patch", "LegacyPatch")} - {None}
    if len(listed) > 1:
        raise Pending("listed_patch_conflict", "The schedule's two patch labels disagree.")
    official = next(iter(listed), None)
    if official and official not in PATCHES:
        raise Pending("listed_patch_outside_range", "The listed patch is outside the frozen LEC opener range.")
    versions = fetch(VERSIONS)
    if not isinstance(versions, list) or not versions:
        raise Pending("patch_index_unavailable", "Data Dragon has no published release index.")
    releases = {}
    for value in versions:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value) if isinstance(value, str) else None
        if match:
            major, minor, build = map(int, match.groups())
            key = _patch_key(major, minor)
            releases[key] = max((build, value), releases.get(key, (0, "")))
    if not releases:
        raise Pending("patch_index_unavailable", "Data Dragon has no recognizable published patch versions.")
    if not official and max(releases) < PATCHES[0]:
        raise Pending("stale_patch_index", "Data Dragon's latest release is stale for the LEC opener.")
    if not official and max(releases) > PATCHES[-1]:
        raise Pending("patch_index_outside_range", "Data Dragon's latest release is outside the frozen candidate range.")
    candidates = [official] if official else sorted(set(PATCHES) & releases.keys(), reverse=True)[:POOL_LIMIT]
    if not candidates:
        raise Pending("patch_index_unavailable", "Data Dragon has no release in the frozen candidate range.")
    raw_disables, disable_status = _disabled(row)
    disabled = sorted({catalog.canonical(name) for name in raw_disables})
    known = set(catalog.names)
    recorded, compatible = {}, {}
    for key in candidates:
        patch = _label(key)
        if key not in releases:
            recorded[patch] = {"abstention": "patch_not_published"}
            continue
        names, details = _dragon_pool(fetch, releases[key][1])
        canonical = {catalog.canonical(name) for name in names}
        unknown = sorted(canonical - known)
        existing = [values for catalogue in (catalog.patches, base_pools)
                    for old_patch, values in catalogue.items() if _patch_label(old_patch) == key]
        if unknown:
            details.update(abstention="unknown_champions", unknown=unknown)
        elif len(canonical) != len(names):
            details.update(abstention="duplicate_champion_aliases")
        elif any(set(map(catalog.canonical, values)) != canonical for values in existing):
            details.update(abstention="conflicting_frozen_pool")
        elif not set(disabled) <= canonical or len(canonical) - len(disabled) < 20:
            details.update(abstention="disabled_pool_incompatible")
        else:
            compatible[patch] = sorted(canonical)
        recorded[patch] = details
    return {"official_patch": _label(official) if official else None, "candidates": recorded,
            "pools": compatible, "disabled": disabled, "disable_status": disable_status,
            "version_source": VERSIONS, "version_sha256": fingerprint(versions)}


def _canonical_team(name):
    return team_alias({"g2": "G2 Esports", "fnc": "Fnatic"}.get(name.casefold(), name))


def _game_identity(game):
    return tuple(str(game.get(key, "")) for key in ("game_id", "series_id", "date", "first", "second"))


def _team_ids(row, trial, evidence_date, name_archive=()):
    archive = {_game_identity(game): (completed, game) for completed, game in trial.archive
               if completed < evidence_date and game.get("game_id")}
    names = {_game_identity(game): game["team_names"] for game in name_archive
             if game.get("team_names") and _game_identity(game) in archive}
    earlier = []
    for key, (completed, game) in archive.items():
        named = names.get(key, game.get("team_names", {}))
        earlier.append((completed, {team: name for team, name in named.items() if str(team) in key[-2:]}))
    earlier.extend((trial.ends[str(series[0]["series_id"])], game.get("team_names", {}))
                   for series in trial.series if trial.ends[str(series[0]["series_id"])] < evidence_date
                   for game in series)
    directory = TeamDirectory()
    directory.add([{"finished_at": day + "T23:59:59+00:00",
                    "team_names": {str(team): _canonical_team(name) for team, name in attached.items()}}
                   for day, attached in earlier], preferred=True)
    teams = {}
    for display in (row["Team1"], row["Team2"]):
        canonical = _canonical_team(display)
        known = directory.resolve(canonical, evidence_date)
        teams[display] = known.id if known else f"leaguepedia:{canonical}"
    if len(set(teams.values())) != 2:
        raise Pending("team_identity_ambiguous", "The opener teams resolve to the same identity.")
    return teams


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _immutable(path, value):
    try:
        write_exclusive(path, value)
    except FileExistsError:
        if read(path) != value:
            raise ValueError("A private immutable watch artifact differs from its existing bytes.")
    _sync_dir(path.parent)


def _status(output, value):
    with NamedTemporaryFile(mode="w", encoding="utf8", dir=output, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(output / "status.json")
        finally:
            temporary.unlink(missing_ok=True)
    _sync_dir(output)
    return value


def _receipt(output):
    path = output / "seal.json"
    envelope = read(path)
    fixture = envelope["fixture"]
    folder = envelope["watch"]["snapshot"]
    if not re.fullmatch(r"inputs-[0-9a-f]{64}", folder):
        raise ValueError("The private seal points outside its frozen snapshot.")
    if (digest(output / folder / "freeze.json") != envelope["freeze_sha256"]
            or digest(output / folder / "provenance.json") != envelope["watch"]["provenance_sha256"]
            or digest(output / folder / "fixture.json") != envelope["watch"]["fixture_sha256"]
            or digest(output / folder / "catalog.json") != envelope["watch"]["catalog_sha256"]):
        raise ValueError("The sealed watch inputs changed.")
    result = {
        "status": "sealed", "publication": "private_only", "fixture_id": fixture["fixture_id"],
        "sealed_at": envelope["sealed_at"], "draft_deadline": fixture["draft_deadline"],
        "sha256": digest(path), "seal": "seal.json", "freeze": f"{folder}/freeze.json",
        "fixture": f"{folder}/fixture.json", "provenance": f"{folder}/provenance.json",
        "patches": fixture["patches"], "official_patch": envelope["watch"]["official_patch"],
        "scenarios": len(envelope["scenarios"]),
        "timing_source": TIMING, "evaluation_requires": "UTC draft-start verification from the official replay",
    }
    _immutable(output / "receipt.json", result)
    return result


def _prepare(freeze, output, fetch, clock, checked_at, team_archive):
    schedule = discover_schedule(fetch)
    match = opening_match(schedule["rows"], schedule["stage"], now=checked_at)
    row, deadline = match["row"], match["deadline"]
    base_hash = digest(freeze)
    manifest, paths = load_freeze(freeze)
    trial = PairTrial.load(**{key: path for key, path in paths.items() if key != "pools"})
    base_pools = read(paths["pools"])
    resolved = resolve_patches(fetch, row, trial.frozen.catalog, base_pools)
    selected = resolved["pools"]
    if not selected:
        return {"status": "pending", "reason": "no_compatible_patch_pool", "official_patch": resolved["official_patch"],
                "candidates": resolved["candidates"], "draft_deadline": deadline.isoformat()}
    archive_hash = digest(team_archive) if team_archive else None
    names = read_event_games(team_archive) if team_archive else ()
    teams = _team_ids(row, trial, min(checked_at.date(), OPENING).isoformat(), names)
    stage = schedule["stage"]
    fixture = {
        "version": 1, "fixture_id": "lec-winter-2027-" + fingerprint(row["MatchId"])[:12], "series_id": row["MatchId"],
        "date": OPENING.isoformat(), "region": "EMEA", "event": EVENT, "teams": list(teams.values()),
        "patches": sorted(selected), "disabled": resolved["disabled"], "fearless": True,
        "schedule_source": schedule["source"], "draft_source": WIKI + quote(stage.replace(" ", "_"), safe="/"),
        "timing_source": TIMING, "draft_deadline": deadline.isoformat(),
    }
    implementation = digest(Path(__file__))
    provenance = {
        "official_announcement": OFFICIAL, "base_freeze_sha256": base_hash, "watcher_sha256": implementation,
        "scheduled_start": match["starts_at"].isoformat(), "schedule": {**schedule, "rows": [row]},
        "team_identities": teams, "team_name_archive_sha256": archive_hash,
        "patch_discovery": {key: value for key, value in resolved.items() if key != "pools"},
        "actual_draft_timing": "official broadcast replay with independently verified UTC draft start",
        "rosters": "earlier_frozen_history_only", "candidate_patch_limit": POOL_LIMIT,
    }
    folder = output / ("inputs-" + fingerprint({"fixture": fixture, "provenance": provenance, "pools": selected}))
    catalog_path = folder / "catalog.json"
    _immutable(catalog_path, {**selected, **base_pools})
    _immutable(folder / "fixture.json", fixture)
    _immutable(folder / "provenance.json", provenance)
    derived = {**paths, "pools": catalog_path}
    value = {
        "version": 1, "protocol_sha256": manifest["protocol_sha256"], "models": identities(**derived),
        "files": {key: os.path.relpath(path.resolve(), folder) for key, path in derived.items()},
    }
    derived_freeze = folder / "freeze.json"
    _immutable(derived_freeze, value)
    _sync_dir(output)

    original_catalog = trial.frozen.catalog
    trial.frozen.catalog = ChampionCatalog(original_catalog.releases, original_catalog.source,
                                           {**selected, **original_catalog.patches})
    if set(trial.frozen.catalog.names) != set(original_catalog.names):
        raise ValueError("An expanded watch pool cannot change the frozen vocabulary.")
    try:
        envelope = make_envelope(fixture, trial, read(catalog_path), digest(derived_freeze), clock=clock)
        if (digest(freeze) != base_hash or identities(**derived) != value["models"]
                or digest(Path(__file__)) != implementation or (team_archive and digest(team_archive) != archive_hash)):
            raise ValueError("Frozen inputs changed while generating the private seal.")
        envelope.update(models=value["models"], watch={
            "snapshot": folder.name, "provenance_sha256": digest(folder / "provenance.json"),
            "fixture_sha256": digest(folder / "fixture.json"), "catalog_sha256": digest(catalog_path),
            "official_patch": resolved["official_patch"],
        })
        write_exclusive(output / "seal.json", envelope, deadline=deadline, clock=clock)
        _sync_dir(output)
    except ValueError as error:
        if clock() >= deadline:
            raise Pending("draft_deadline_passed", "The opener deadline passed before the private seal was written.",
                          status="missed") from error
        raise
    return _receipt(output)


def watch_once(freeze, output, *, team_archive=None, fetch=fetch_json, clock=utc_now):
    """Run one bounded check. The CLI uses the real UTC wall clock."""
    freeze, output = Path(freeze), Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        raise ValueError("Use a private output directory with mode 0700.")
    with (output / ".watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "pending", "reason": "watch_already_running"}
        now = clock()
        try:
            if (output / "seal.json").exists() or (output / "receipt.json").exists():
                result = _receipt(output)
            elif now >= END:
                result = {"status": "closed", "reason": "opening_day_passed"}
            else:
                result = _prepare(freeze, output, fetch, clock, now, team_archive)
        except Pending as error:
            result = {"status": error.status, "reason": error.reason, "detail": str(error)}
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            result = {"status": "pending", "reason": "source_unavailable",
                      "detail": f"Public source check failed ({type(error).__name__}); retry on the next run."}
        return _status(output, {"checked_at": now.isoformat(), "event": EVENT, "official_announcement": OFFICIAL, **result})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True, help="Existing private pair trial freeze.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Private durable LEC watch directory.")
    parser.add_argument("--team-archive", type=Path,
                        help="Optional original GRID rehearsal archive to restore frozen team names.")
    args = parser.parse_args()
    try:
        print(json.dumps(watch_once(args.freeze, args.output_dir, team_archive=args.team_archive), indent=2, sort_keys=True))
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
