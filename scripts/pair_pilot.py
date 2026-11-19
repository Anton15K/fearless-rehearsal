"""Seal and verify a pre-draft Game 1 pilot without publishing its forecasts."""

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from fearless.adaptive_pairs import PAIR_WINDOWS
from fearless.event_forecaster import digest
from fearless.event_series import utc
from fearless.model_features import ChampionCatalog
from fearless.pair_trial import METHODS, SEED, SIMULATIONS, PairTrial, checked, identities, validate_prefix
from fearless.rules import DRAFT_LENGTH
from scripts.pair_trial import load_freeze, read


FIXTURE_FIELDS = {"version", "fixture_id", "series_id", "date", "region", "event", "teams", "patches",
                  "disabled", "fearless", "schedule_source", "draft_source", "timing_source", "draft_deadline"}


def utc_now():
    return datetime.now(timezone.utc)


def _url(value):
    parsed = urlsplit(value) if isinstance(value, str) else None
    if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source evidence requires a direct HTTPS URL.")
    return value


def _names(values, field, *, minimum=0):
    if (not isinstance(values, list) or len(values) < minimum
            or any(not isinstance(value, str) or not value or value != value.strip() for value in values)
            or len(set(values)) != len(values)):
        raise ValueError(f"The fixture requires distinct canonical {field} names.")
    return values


def _fixture(value):
    if not isinstance(value, dict) or set(value) != FIXTURE_FIELDS or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Expected the fixed version 1 fixture schema.")
    for field in ("fixture_id", "series_id", "event", "region"):
        if not isinstance(value[field], str) or not value[field] or value[field] != value[field].strip():
            raise ValueError(f"An explicit fixture {field} is required.")
    if value["date"] != date.fromisoformat(value["date"]).isoformat():
        raise ValueError("The fixture date must be a UTC calendar day.")
    teams = _names(value["teams"], "team", minimum=2)
    if len(teams) != 2 or type(value["fearless"]) is not bool:
        raise ValueError("Exactly two fixture teams and an explicit draft format are required.")
    _names(value["patches"], "patch", minimum=1)
    _names(value["disabled"], "disabled champion")
    for field in ("schedule_source", "draft_source", "timing_source"):
        _url(value[field])
    return utc(value["draft_deadline"])


def _pool(fixture, pools, catalog, patch):
    if patch not in pools:
        raise ValueError("Every declared scenario needs a frozen patch pool.")
    names = pools[patch]
    canonical = {catalog.canonical(name): name for name in names}
    disables = {catalog.canonical(champion) for champion in fixture["disabled"]}
    if len(canonical) != len(names) or len(disables) != len(fixture["disabled"]) or not disables <= canonical.keys():
        raise ValueError("The scenario contains unresolved disables or duplicate champion aliases.")
    enabled = [name for key, name in canonical.items() if key not in disables]
    if len(enabled) < DRAFT_LENGTH:
        raise ValueError("The scenario pool cannot support an entire Game 1 draft.")
    return enabled


def _orders(fixture):
    return [(patch, first, second) for patch in sorted(fixture["patches"])
            for first, second in (tuple(fixture["teams"]), tuple(reversed(fixture["teams"])))]


def _panels(matrix, pool):
    if set(matrix) != {str(slot) for slot in PAIR_WINDOWS} or any(set(panel) != set(METHODS) for panel in matrix.values()):
        raise ValueError("A scenario must include all three windows and methods.")
    enabled = set(pool)
    return {slot: {name: [list(pair) for pair in checked(panel[name], enabled)] for name in METHODS}
            for slot, panel in matrix.items()}


def make_envelope(fixture, trial, pools, freeze_sha256, *, clock=utc_now):
    deadline = _fixture(fixture)
    code_hash = digest(Path(__file__))
    started = clock()
    if started >= deadline or started.date().isoformat() > fixture["date"]:
        raise ValueError("The pilot must start before the registered draft deadline.")
    if fixture["series_id"] in trial.ends:
        raise ValueError("The target fixture is already present in the frozen event evidence.")
    evidence_date = min(started.date().isoformat(), fixture["date"])
    scenarios = []
    for patch, first, second in _orders(fixture):
        pool = _pool(fixture, pools, trial.frozen.catalog, patch)
        context = {"date": evidence_date, "patch": patch, "region": fixture["region"], "first": first, "second": second,
                   "game_number": 1, "locked": [], "pool": pool, "fearless": fixture["fearless"]}
        ranks = trial.before_draft(context, event=fixture["event"], series_id=fixture["series_id"])
        scenarios.append({"patch": patch, "first": first, "second": second, "windows": _panels(ranks, pool)})
    sealed = clock()
    if not started <= sealed < deadline:
        raise ValueError("Scenario generation exceeded the registered draft deadline.")
    if digest(Path(__file__)) != code_hash:
        raise ValueError("The pilot wrapper changed during forecast generation.")
    return {"version": 1, "scope": "pilot", "fixture": fixture, "freeze_sha256": freeze_sha256,
            "pilot_code_sha256": code_hash, "started_at": started.isoformat(), "sealed_at": sealed.isoformat(), "evidence_date": evidence_date,
            "simulations_per_scenario": SIMULATIONS, "seed": SEED, "scenarios": scenarios}


def write_exclusive(path, value, *, deadline=None, clock=utc_now):
    """Publish complete bytes at a new path without replacing an existing seal."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        if deadline is not None and clock() >= deadline:
            raise ValueError("The registered deadline passed before the seal could be written.")
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_verified_seal(path, public_sha256, fixture_id):
    if digest(path) != public_sha256:
        raise ValueError("The seal does not match the public SHA-256.")
    envelope = read(path)
    if envelope.get("version") != 1 or envelope.get("scope") != "pilot" or envelope.get("fixture", {}).get("fixture_id") != fixture_id:
        raise ValueError("The public fixture ID does not match a version 1 pilot seal.")
    if envelope.get("pilot_code_sha256") != digest(Path(__file__)):
        raise ValueError("The pilot wrapper differs from the sealed implementation.")
    return envelope


def _validate_actual(fixture, actual, catalog):
    if actual.get("version") != 1 or actual.get("verified") is not True:
        raise ValueError("A verified official Game 1 record is required.")
    if any(actual.get(field) != fixture[field] for field in ("fixture_id", "series_id", "date", "event", "region")):
        raise ValueError("The recorded game differs from the registered fixture identity or UTC date.")
    if type(actual.get("game_number")) is not int or actual["game_number"] != 1 or actual.get("locked", []) != []:
        raise ValueError("The pilot is for Game 1 with no earlier-game locks.")
    if actual.get("fearless") is not fixture["fearless"]:
        raise ValueError("The recorded game format differs from the registered fixture.")
    for field, label in (("draft_source", "draft source"), ("timing_source", "timing source")):
        if actual.get(field) != fixture[field]:
            raise ValueError(f"Verified {label} differs from the fixture's pinned authority.")
    disabled = _names(actual.get("disabled"), "recorded disable")
    if len(disabled) != len(fixture["disabled"]) or set(map(catalog.canonical, disabled)) != set(map(catalog.canonical, fixture["disabled"])):
        raise ValueError("The recorded game includes unexpected champion disables.")

    start = utc(actual.get("drafted_at"))
    finish = utc(actual.get("finished_at"))
    timing = actual.get("timing")
    if not isinstance(timing, dict) or timing.get("kind") != "official_draft_start":
        raise ValueError("Scoring requires a verified official draft start, with its raw timing status.")
    if utc(timing.get("timestamp")) != start or start.date().isoformat() != fixture["date"] or finish <= start:
        raise ValueError("The official draft-start timestamp conflicts with the recorded draft timing.")
    return start


def score(envelope, actual, pools, catalog, published_at, commitment_source):
    fixture = envelope["fixture"]
    deadline = _fixture(fixture)
    start = _validate_actual(fixture, actual, catalog)
    started, sealed, published = utc(envelope["started_at"]), utc(envelope["sealed_at"]), utc(published_at)
    if not started <= sealed <= published < min(deadline, start):
        raise ValueError("The public commitment needs verified publication before the deadline and official draft start.")
    _url(commitment_source)
    if (envelope.get("evidence_date") != min(started.date().isoformat(), fixture["date"])
            or (envelope.get("simulations_per_scenario"), envelope.get("seed")) != (SIMULATIONS, SEED)):
        raise ValueError("The seal does not match the frozen pilot simulation protocol.")

    fields = "patch", "first", "second"
    scenarios = envelope["scenarios"]
    registered = [tuple(scenario[field] for field in fields) for scenario in scenarios]
    observed = tuple(actual.get(field) for field in fields)
    if len(registered) != len(set(registered)) or set(registered) != set(_orders(fixture)) or observed not in registered:
        raise ValueError("The official patch and canonical team order must select exactly one registered scenario.")
    chosen = scenarios[registered.index(observed)]
    pool = _pool(fixture, pools, catalog, observed[0])
    display = {catalog.canonical(name): name for name in pool}
    actions = actual.get("actions")
    if not isinstance(actions, list) or len(actions) != DRAFT_LENGTH:
        raise ValueError("Scoring requires every official Game 1 draft action.")
    actions = [{**action, "champion": display.get(catalog.canonical(action["champion"]), action["champion"])} for action in actions]
    validate_prefix({"first": observed[1], "second": observed[2], "pool": pool, "locked": []}, actions)

    marks = {}
    for slot, methods in _panels(chosen["windows"], pool).items():
        index = int(slot)
        target = sorted(action["champion"] for action in actions[index:index + 2])
        marks[slot] = {name: shortlist.index(target) + 1 if target in shortlist else 0 for name, shortlist in methods.items()}
    scores = {}
    for name in METHODS:
        ranks = [rank[name] for rank in marks.values()]
        scores[name] = {"windows": len(ranks), "hits_at_1": sum(i == 1 for i in ranks), "hits_at_3": sum(i > 0 for i in ranks)}
    return {"version": 1, "scope": "pilot", "evaluation": "sealed pre-draft unconditioned shortlists",
            "operating_mode": "Fearless Game 1" if fixture["fearless"] else "standard Game 1",
            "fixture_id": fixture["fixture_id"], "scenario": dict(zip(fields, observed, strict=True)),
            "published_at": published.isoformat(), "commitment_source": commitment_source,
            "drafted_at": start.isoformat(), "timing_source": fixture["timing_source"],
            "timing_status": "official_draft_start", "draft_source": fixture["draft_source"], "methods": scores, "by_window": marks}


def seal(args):
    if args.output.exists():
        raise FileExistsError("A pilot seal already exists at this path.")
    freeze_hash = digest(args.freeze)
    manifest, paths = load_freeze(args.freeze)
    fixture = read(args.fixture)
    deadline = _fixture(fixture)
    if utc_now() >= deadline:
        raise ValueError("The registered draft deadline has passed.")
    trial = PairTrial.load(**{key: path for key, path in paths.items() if key != "pools"})
    envelope = make_envelope(fixture, trial, read(paths["pools"]), freeze_hash)
    if digest(args.freeze) != freeze_hash or identities(**paths) != manifest["models"]:
        raise ValueError("The frozen models or files changed during forecast generation.")
    envelope["models"] = manifest["models"]
    write_exclusive(args.output, envelope, deadline=deadline)
    public = {key: fixture[key] for key in ("fixture_id", "date", "event", "region", "teams", "patches", "fearless",
                                           "schedule_source", "draft_deadline")}
    print(json.dumps({**public, "scope": "pilot", "sha256": digest(args.output), "sealed_at": envelope["sealed_at"],
                      "scenarios": len(envelope["scenarios"])}, indent=2))


def verify(args):
    envelope = read_verified_seal(args.seal, args.sha256, args.fixture_id)
    manifest, paths = load_freeze(args.freeze)
    if envelope.get("freeze_sha256") != digest(args.freeze) or envelope.get("models") != manifest["models"]:
        raise ValueError("The sealed forecast does not use the verified frozen policy.")
    pools = read(paths["pools"])
    result = score(envelope, read(args.actual), pools, ChampionCatalog.from_json(pools), args.published_at, args.commitment_source)
    result.update(seal_sha256=args.sha256, freeze_sha256=envelope["freeze_sha256"])
    if args.output:
        write_exclusive(args.output, result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commit = commands.add_parser("seal")
    for name in ("freeze", "fixture", "output"):
        commit.add_argument("--" + name, type=Path, required=True)
    commit.set_defaults(run=seal)
    check = commands.add_parser("verify")
    for name in ("freeze", "seal", "actual"):
        check.add_argument("--" + name, type=Path, required=True)
    for name in ("sha256", "fixture-id", "published-at", "commitment-source"):
        check.add_argument("--" + name, required=True)
    check.add_argument("--output", type=Path)
    check.set_defaults(run=verify)
    args = parser.parse_args()
    try:
        args.run(args)
    except (KeyError, TypeError, OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
