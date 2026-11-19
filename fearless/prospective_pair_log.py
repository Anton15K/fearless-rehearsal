"""Private, append-only ledger for a prospective shadow pair-ranking trial.

The caller verifies the source and identity of every supplied observation.
Only the ledger's clock stamps new entries. A scheduled game start is a
commitment deadline and cannot establish when the draft actually began.
"""
from collections import Counter
from collections.abc import Mapping
from contextlib import closing, contextmanager
from datetime import date as CalendarDate, datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

from fearless.event_series import utc
from fearless.rules import DRAFT_LENGTH, legal_champions, validate_action


PAIR_WINDOWS = (7, 9, 17)
BEFORE_DRAFT = -1
_KEYS = {
    "trials": ("trial_id",),
    "commitments": ("trial_id", "game_id"),
    "forecasts": ("trial_id", "game_id", "slot"),
    "attestations": ("trial_id", "game_id", "slot"),
    "verified_times": ("trial_id", "game_id", "slot"),
    "verified_contexts": ("trial_id", "game_id"),
    "outcomes": ("trial_id", "game_id"),
}


def _schema():
    for name, keys in _KEYS.items():
        fields = ", ".join(f"{key} {'INTEGER' if key == 'slot' else 'TEXT'} NOT NULL" for key in keys)
        parent = ", FOREIGN KEY (trial_id) REFERENCES trials(trial_id)" if name != "trials" else ""
        match = " AND ".join(f"{key} = NEW.{key}" for key in keys)
        yield f"""
            CREATE TABLE IF NOT EXISTS {name} (
                {fields}, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY ({', '.join(keys)}){parent}
            );
        """
        for verb in ("UPDATE", "DELETE", "INSERT"):
            when = f"WHEN EXISTS (SELECT 1 FROM {name} WHERE {match})" if verb == "INSERT" else ""
            yield f"""
                CREATE TRIGGER IF NOT EXISTS {name}_immutable_{verb.lower()}
                BEFORE {verb} ON {name} {when}
                BEGIN
                    SELECT RAISE(ABORT, 'Shadow trial rows are append-only');
                END;
            """


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stamp(value):
    return utc(value).isoformat(timespec="microseconds")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string.")
    return value


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _binding(protocol_sha256, models):
    if not isinstance(models, Mapping) or len(models) != 3:
        raise ValueError("Exactly three model hashes are required for the trial binding.")
    return {
        "protocol_sha256": _hash(protocol_sha256, "Protocol"),
        "models": {_text(name, "Model name"): _hash(digest, "Model") for name, digest in models.items()},
    }


def _read(db, table, key):
    where = " AND ".join(f"{name}=?" for name in _KEYS[table])
    row = db.execute(f"SELECT recorded_at, payload FROM {table} WHERE {where}", key).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def _append(db, table, key, value, now, *, deadline=None):
    previous = _read(db, table, key)
    if previous:
        if previous[1] != value:
            raise ValueError("The first ledger entry is immutable.")
        return previous[0]
    if deadline is not None and now >= deadline:
        raise ValueError("The ledger deadline has already passed.")
    placeholders = ", ".join("?" for _ in range(len(key) + 2))
    db.execute(f"INSERT INTO {table} VALUES ({placeholders})", (*key, now, _json(value)))
    return now


def _trial(db, trial_id, *, binding=None):
    row = _read(db, "trials", (trial_id,))
    if row is None:
        raise ValueError("Trial is not registered.")
    trial = row[1]
    if binding is not None and binding != trial["binding"]:
        raise ValueError("The artifacts do not match the frozen trial binding.")
    return trial


def _game(trial, game_id):
    game = trial["games"].get(game_id)
    if game is None:
        raise ValueError("Game is not registered in this trial.")
    if not game["eligible"]:
        raise ValueError("Game is not eligible in the frozen trial registry.")
    return game


def _source(trial, source):
    if source not in trial["timing_sources"]:
        raise ValueError("The trial does not recognize this timing source.")
    return source


def _slot(value):
    if isinstance(value, (int, str)) and str(value) in {"7", "9", "17"}:
        return int(value)
    raise ValueError("A pair forecast requires the exact 7, 9, or 17 action prefix.")


def _window_fields(values):
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("Supply pair window fields as a mapping.")
    normalized = {_slot(slot): value for slot, value in values.items()}
    if len(normalized) != len(values):
        raise ValueError("The same pair window was supplied twice.")
    return normalized


def _champions(values, label):
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{label} must be an ordered sequence of champions.")
    names = [_text(value, label) for value in values]
    if len(names) != len(set(names)):
        raise ValueError(f"{label} contains duplicate champions.")
    return names


def _actions(actions, *, first, second, pool, locked):
    prefix = []
    for row in actions:
        action = {key: row[key] for key in ("team", "kind", "champion")}
        validate_action(pool, locked, prefix, first, second, **action)
        prefix.append(action)
    return prefix


def _ranking(rankings, model_names, legal):
    if not isinstance(rankings, Mapping) or set(rankings) != set(model_names):
        raise ValueError("Supply rankings for all three frozen models.")
    result = {}
    for model, rows in rankings.items():
        if not isinstance(rows, (list, tuple)) or len(rows) > 3:
            raise ValueError("A model can rank at most three unordered pairs.")
        pairs = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("Ranked pairs require exactly two legal champions.")
            names = _champions(row, "Ranked pair")
            if not set(names) <= legal:
                raise ValueError("Ranked pair contains an illegal champion.")
            names.sort()
            if names in pairs:
                raise ValueError("A model cannot rank the same unordered pair twice.")
            pairs.append(names)
        result[model] = pairs
    return result


def _observation(value, source, trial, now):
    observed = _stamp(value)
    if observed > now:
        raise ValueError("An observation cannot be in the future.")
    return {"observed_at": observed, "source": _source(trial, source)}


def _verified_times(db, key):
    rows = db.execute("SELECT slot, payload FROM verified_times WHERE trial_id=? AND game_id=?", key)
    return {slot: json.loads(payload)["observed_at"] for slot, payload in rows}


def _write_timing(db, key, trial, now, source, draft_started_at, target_times):
    facts = _window_fields(target_times)
    if draft_started_at is not None:
        facts[BEFORE_DRAFT] = draft_started_at
    if not facts:
        raise ValueError("Supply at least one verified draft or target time.")
    outcome = _read(db, "outcomes", key)
    if outcome and outcome[1]["status"] == "cancelled":
        raise ValueError("A cancelled game cannot have draft timing.")
    known = _verified_times(db, key)
    for slot, observed_at in facts.items():
        observation = _observation(observed_at, source, trial, now)
        _append(db, "verified_times", (*key, slot), observation, now)
        known[slot] = observation["observed_at"]
    timeline = [known[slot] for slot in sorted(known)]
    if any(a >= b for a, b in zip(timeline, timeline[1:])):
        raise ValueError("Verified draft times must be chronological.")
    return now


def _verified_context(value):
    required = {"game_id", "patch", "first", "second", "game_number", "locked", "pool"}
    optional = {"date", "region", "event", "series_id", "fearless"}
    if not isinstance(value, Mapping) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("Supply the full independently verified game context.")
    context = dict(value)
    for field in ("game_id", "patch", "first", "second", "region", "event", "series_id"):
        if field in context:
            _text(context[field], field)
    if (context["first"] == context["second"] or type(context["game_number"]) is not int
            or not 1 <= context["game_number"] <= 5 or type(context.get("fearless", True)) is not bool):
        raise ValueError("Invalid verified game number, teams or format.")
    if "date" in context:
        context["date"] = CalendarDate.fromisoformat(context["date"]).isoformat()
    for field in ("pool", "locked"):
        context[field] = sorted(_champions(context[field], field))
    return context


def _validate_outcome(actions, actual_context=None):
    if actual_context is None:
        return _actions(actions, first=actions[0]["team"], second=actions[1]["team"], locked=(),
                        pool=[action["champion"] for action in actions if action["champion"] is not None])
    locked = actual_context["locked"] if actual_context.get("fearless", True) else ()
    return _actions(actions, first=actual_context["first"], second=actual_context["second"],
                    pool=actual_context["pool"], locked=locked)


def _write_context(db, key, trial, now, source, actual_context):
    context = _verified_context(actual_context)
    source = _source(trial, source)
    outcome = _read(db, "outcomes", key)
    if outcome:
        if outcome[1]["status"] == "cancelled":
            raise ValueError("A cancelled game cannot have a verified draft context.")
        _validate_outcome(outcome[1]["actions"], context)
    return _append(db, "verified_contexts", key, {"context": context, "source": source}, now)


class TrialLedger:
    """Store private commitments, forecasts and verified outcomes separately."""

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("Shadow trial database permissions must be private.")
        with self._database(write=True) as db:
            for statement in _schema():
                db.execute(statement)

    @contextmanager
    def _database(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _now(self, db):
        instant = self.clock()
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError("The ledger clock requires an explicit time zone.")
        now = _stamp(instant.isoformat())
        bounds = " UNION ALL ".join(f"SELECT max(recorded_at) AS last FROM {name}" for name in _KEYS)
        most_recent = db.execute(f"SELECT max(last) FROM ({bounds})").fetchone()[0]
        if most_recent is not None and now < most_recent:
            raise ValueError("The ledger clock moved backward.")
        return now

    def freeze_trial(self, *, trial_id, protocol_sha256, models, enrollment_deadline, timing_sources, games):
        """Freeze every known fixture, eligibility decision and trusted timing source."""
        _text(trial_id, "Trial identifier")
        deadline = _stamp(enrollment_deadline)
        if isinstance(timing_sources, str):
            raise ValueError("Supply timing sources as a sequence.")
        sources = sorted({_text(source, "Timing source") for source in timing_sources})
        registered = {}
        for game in games:
            game_id = _text(game["game_id"], "Game identifier")
            starts_at = _stamp(game["starts_at"])
            eligible = game["eligible"]
            if game_id in registered or type(eligible) is not bool or starts_at < deadline:
                raise ValueError("Registered games need unique identifiers, frozen eligibility and future starts.")
            registered[game_id] = {
                "starts_at": starts_at, "eligible": eligible,
                "exclusion_reason": game.get("exclusion_reason"),
            }
        if not registered or not sources:
            raise ValueError("Freeze a nonempty game registry and trusted timing sources.")
        payload = {
            "binding": _binding(protocol_sha256, models), "enrollment_deadline": deadline,
            "timing_sources": sources, "games": registered,
        }
        with self._database(write=True) as db:
            return _append(db, "trials", (trial_id,), payload, self._now(db), deadline=deadline)

    def commit_game(self, *, trial_id, game_id, protocol_sha256, models, evidence_sha256,
                    date, region, event, series_id, patch, first, second, game_number, locked, pool,
                    rosters=None, pre_draft_rankings=None):
        """Seal the game inputs and optional blind pair ranks before the scheduled deadline."""
        key = (trial_id, game_id)
        enabled = _champions(pool, "Enabled pool")
        locked = _champions(locked, "Fearless locks")
        if first == second or type(game_number) is not int or not 1 <= game_number <= 5:
            raise ValueError("A game needs distinct drafting teams and a valid series game number.")
        for label, value in (("team", first), ("team", second), ("patch", patch), ("region", region),
                             ("event", event), ("series", series_id)):
            _text(value, label)
        legal = set(legal_champions(enabled, locked, ()))
        if len(legal) < 10:
            raise ValueError("A committed game needs enough enabled champions for ten picks.")
        rankings = {
            str(slot): _ranking(value, models, legal)
            for slot, value in _window_fields(pre_draft_rankings).items()
        }
        context = {
            "date": CalendarDate.fromisoformat(date).isoformat(), "region": region, "event": event,
            "series_id": series_id, "patch": patch, "first": first, "second": second,
            "game_number": game_number, "locked": sorted(locked), "pool": enabled,
            "rosters": rosters, "evidence_sha256": _hash(evidence_sha256, "Evidence"),
        }
        payload = json.loads(_json({"context": context, "rankings": rankings}))
        with self._database(write=True) as db:
            trial = _trial(db, trial_id, binding=_binding(protocol_sha256, models))
            game = _game(trial, game_id)
            if context["date"] > game["starts_at"][:10]:
                raise ValueError("The forecast date cannot follow the registered game date.")
            if not _read(db, "commitments", key):
                if _read(db, "outcomes", key):
                    raise ValueError("A game with a recorded outcome cannot receive a commitment.")
                if _verified_times(db, key):
                    raise ValueError("A game with verified draft activity cannot receive a pre-draft commitment.")
            return _append(db, "commitments", key, payload, self._now(db), deadline=game["starts_at"])

    def game_context(self, trial_id, game_id):
        """Read the original game inputs and artifact hashes without revealing either ranking or outcome."""
        with self._database() as db:
            trial = _trial(db, trial_id)
            game = _game(trial, game_id)
            stored = _read(db, "commitments", (trial_id, game_id))
            if not stored:
                raise ValueError("Game has no frozen commitment.")
            recorded_at, value = stored
            return {**value["context"], **trial["binding"], "recorded_at": recorded_at,
                    "starts_at": game["starts_at"]}

    def record_window(self, *, trial_id, game_id, protocol_sha256, models, prefix, rankings, observed_at, source):
        """Record one live conditional shortlist against a source-observed draft prefix."""
        game_key = (trial_id, game_id)
        slot = _slot(len(prefix))
        key = (*game_key, slot)
        with self._database(write=True) as db:
            trial = _trial(db, trial_id, binding=_binding(protocol_sha256, models))
            _game(trial, game_id)
            if not _read(db, "forecasts", key):
                if _read(db, "outcomes", game_key):
                    raise ValueError("A game with a recorded outcome cannot receive a forecast.")
                if any(known >= slot for known in _verified_times(db, game_key)):
                    raise ValueError("A verified target action has already been recorded.")
            previous = _read(db, "commitments", game_key)
            if not previous:
                raise ValueError("A live forecast needs a frozen game commitment.")
            recorded_at, value = previous
            context = value["context"]
            prefix = _actions(prefix, first=context["first"], second=context["second"],
                              pool=context["pool"], locked=context["locked"])
            legal = set(legal_champions(context["pool"], context["locked"], prefix))
            ranks = _ranking(rankings, models, legal)
            now = self._now(db)
            observed = _observation(observed_at, source, trial, now)
            if observed["observed_at"] <= recorded_at:
                raise ValueError("A live prefix must have been observed after the game commitment.")
            if context["date"] > observed["observed_at"][:10]:
                raise ValueError("The frozen forecast date cannot follow the live prefix date.")
            payload = {**observed, "prefix": prefix, "rankings": ranks}
            return _append(db, "forecasts", key, payload, now)

    def _attest(self, trial_id, game_id, slot, observed_at, source, prefix=None):
        key = (trial_id, game_id)
        with self._database(write=True) as db:
            trial = _trial(db, trial_id)
            _game(trial, game_id)
            now = self._now(db)
            observed = _observation(observed_at, source, trial, now)
            if slot == BEFORE_DRAFT:
                original = _read(db, "commitments", key)
            else:
                original = _read(db, "forecasts", (*key, slot))
            if not original or observed["observed_at"] <= original[0]:
                raise ValueError("Record an attestation from after the entry it verifies.")
            if prefix is not None and original[1]["prefix"] != [
                {key: item[key] for key in ("team", "kind", "champion")} for item in prefix
            ]:
                raise ValueError("The observed open prefix differs from the recorded forecast prefix.")
            return _append(db, "attestations", (*key, slot), observed, now)

    def attest_unopened_draft(self, *, trial_id, game_id, observed_at, source):
        """Attest from a trusted snapshot that a committed game's draft had yet to open."""
        return self._attest(trial_id, game_id, BEFORE_DRAFT, observed_at, source)

    def attest_open_window(self, *, trial_id, game_id, prefix, observed_at, source):
        """Attest that a game was still at the exact recorded prefix after the write."""
        return self._attest(trial_id, game_id, _slot(len(prefix)), observed_at, source, prefix)

    def record_verified_timing(self, *, trial_id, game_id, source, target_times=None, draft_started_at=None):
        """Append verified draft clocks before or after the separate outcome record."""
        key = (trial_id, game_id)
        with self._database(write=True) as db:
            trial = _trial(db, trial_id)
            _game(trial, game_id)
            return _write_timing(db, key, trial, self._now(db), source, draft_started_at, target_times)

    def record_verified_context(self, *, trial_id, game_id, actual_context, source):
        """Append independently verified game inputs without changing the recorded draft."""
        key = (trial_id, game_id)
        with self._database(write=True) as db:
            trial = _trial(db, trial_id)
            _game(trial, game_id)
            return _write_context(db, key, trial, self._now(db), source, actual_context)

    def record_outcome(self, *, trial_id, game_id, actions, actual_context=None, target_times=None,
                       draft_started_at=None, source=None):
        """Append the verified full draft and any independently verified timing."""
        key = (trial_id, game_id)
        if len(actions) != DRAFT_LENGTH:
            raise ValueError("An outcome requires a complete legal draft.")
        with self._database(write=True) as db:
            trial = _trial(db, trial_id)
            game = _game(trial, game_id)
            now = self._now(db)
            if source is not None:
                _source(trial, source)
            if target_times or draft_started_at is not None:
                _write_timing(db, key, trial, now, source, draft_started_at, target_times)
            if not _verified_times(db, key) and now < game["starts_at"]:
                raise ValueError("An untimed outcome cannot precede the game deadline.")
            if actual_context is not None:
                _write_context(db, key, trial, now, source, actual_context)
            verified = _read(db, "verified_contexts", key)
            complete = _validate_outcome(actions, verified[1]["context"] if verified else None)
            return _append(db, "outcomes", key, {
                "status": "completed", "actions": complete, "source": source,
            }, now)

    def record_cancelled(self, *, trial_id, game_id, reason, source):
        """Account for a scheduled but unplayed game without editing registered eligibility."""
        key = (trial_id, game_id)
        with self._database(write=True) as db:
            trial = _trial(db, trial_id)
            _game(trial, game_id)
            value = {"status": "cancelled", "reason": _text(reason, "Cancellation reason"),
                     "source": _source(trial, source)}
            return _append(db, "outcomes", key, value, self._now(db))

    def score(self, trial_id):
        """Join only exact, timed predictions to outcomes and report all eligible windows."""
        games = Counter(registered=0, eligible=0, committed=0, completed=0, cancelled=0, awaiting_outcome=0)
        blind, conditional = [], []
        with self._database() as db:
            trial = _trial(db, trial_id)
            for game_id, game in sorted(trial["games"].items()):
                games["registered"] += 1
                if not game["eligible"]:
                    continue
                games["eligible"] += 1
                key = (trial_id, game_id)
                committed = _read(db, "commitments", key)
                outcome = _read(db, "outcomes", key)
                actual = outcome[1] if outcome else None
                timing = _verified_times(db, key)
                verified = _read(db, "verified_contexts", key)
                actual_context = verified[1]["context"] if verified else None
                games["committed"] += committed is not None
                games[actual["status"] if actual else "awaiting_outcome"] += 1
                unopened = _read(db, "attestations", (*key, BEFORE_DRAFT))
                for slot in PAIR_WINDOWS:
                    rankings = committed[1]["rankings"].get(str(slot)) if committed else None
                    forecast = _read(db, "forecasts", (*key, slot))
                    open_window = _read(db, "attestations", (*key, slot))
                    blind.append(_assess(game_id, slot, rankings, committed, actual, actual_context, timing, unopened))
                    conditional.append(_assess(game_id, slot, forecast[1]["rankings"] if forecast else None,
                                               committed, actual, actual_context, timing, open_window, forecast))
        return {
            "trial_id": trial_id, "games": dict(games),
            "pre_draft": _summary(blind, trial["binding"]["models"]),
            "conditional": _summary(conditional, trial["binding"]["models"]),
        }


def _assess(game_id, slot, rankings, committed, actual, actual_context, timing, attested, forecast=None):
    row = {"game_id": game_id, "slot": slot}
    if actual and actual["status"] == "cancelled":
        return {**row, "status": "cancelled"}
    if rankings is None:
        return {**row, "status": "missed"}
    if actual is None:
        return {**row, "status": "awaiting_outcome"}
    if actual_context is None:
        return {**row, "status": "context_unknown"}
    expected = {**committed[1]["context"], "game_id": game_id, "fearless": True}
    expected["pool"] = sorted(expected["pool"])
    if any(expected[field] != value for field, value in actual_context.items()):
        return {**row, "status": "context_mismatch"}
    draft_started_at = timing.get(BEFORE_DRAFT)
    first_known = min(timing.values(), default=None)
    if first_known and committed[0] >= first_known:
        return {**row, "status": "retrospective_late"}
    if ((draft_started_at and expected["date"] != draft_started_at[:10])
            or (first_known and expected["date"] > first_known[:10])):
        return {**row, "status": "context_mismatch"}

    if forecast:
        if forecast[1]["prefix"] != actual["actions"][:slot]:
            return {**row, "status": "prefix_mismatch"}
        recorded_at, target_time = forecast[0], timing.get(slot)
        if any(known >= slot and recorded_at >= value for known, value in timing.items()):
            return {**row, "status": "retrospective_late"}
    else:
        recorded_at, target_time = committed[0], draft_started_at
    if target_time and recorded_at >= target_time:
        return {**row, "status": "retrospective_late"}
    if target_time is None and attested is None:
        return {**row, "status": "retrospective_unknown"}
    target = sorted(action["champion"] for action in actual["actions"][slot:slot + 2])
    return {**row, "status": "scored", "hits": {model: target in ranks for model, ranks in rankings.items()},
            "abstained": [model for model, ranks in rankings.items() if not ranks]}


def _summary(rows, names):
    scored = [row for row in rows if row["status"] == "scored"]
    return {
        "eligible_windows": len(rows), "statuses": dict(Counter(row["status"] for row in rows)),
        "models": {name: {"scored": len(scored), "hits": sum(row["hits"][name] for row in scored),
                          "abstained": sum(name in row["abstained"] for row in scored)} for name in names},
        "windows": rows,
    }
