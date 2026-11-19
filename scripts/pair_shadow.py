"""Operate a private, time-stamped pair trial using observed game and draft inputs."""
import argparse
from datetime import date
import json
from pathlib import Path

from fearless.event_forecaster import digest
from fearless.pair_trial import PairTrial
from scripts.pair_trial import ARTIFACTS, load_freeze, read

CONTEXT = ("date", "patch", "region", "first", "second", "game_number", "locked", "pool", "rosters")


def dispatch(action, ledger, trial_id, game_id, record, freeze_path):
    if action == "score":
        return ledger.score(trial_id)
    if action == "outcome":
        return ledger.record_outcome(trial_id=trial_id, game_id=game_id, **record)
    if action == "cancel":
        return ledger.record_cancelled(trial_id=trial_id, game_id=game_id, **record)
    if action in ("attest-open", "attest-unopened"):
        attest = ledger.attest_open_window if action == "attest-open" else ledger.attest_unopened_draft
        return attest(trial_id=trial_id, game_id=game_id, **record)

    manifest, files = load_freeze(freeze_path)
    binding = dict(trial_id=trial_id, protocol_sha256=manifest["protocol_sha256"], models=manifest["models"])
    if action == "register":
        return ledger.freeze_trial(**binding, **record)

    trial = PairTrial.load(**{key: files[key] for key in ARTIFACTS if key != "pools"})
    available = read(files["pools"])
    if action == "commit-game":
        # The trial's known pool and confirmed fixture exclusions determine champion legality.
        patch = record["patch"]
        disabled = set(record["disabled"])
        if patch not in available or not disabled <= set(available[patch]):
            raise ValueError("Unknown patch or champion disable for this frozen fixture.")
        pool = sorted(set(available[patch]) - disabled)
        if not set(pool) <= set(trial.frozen.catalog.names):
            raise ValueError("The event introduced champions absent from the frozen model.")
        pool_record = {**record, "pool": pool}
        ctx = {key: pool_record[key] for key in CONTEXT}
        date.fromisoformat(ctx["date"])
        if len(set(ctx["locked"])) != len(ctx["locked"]) or not set(ctx["locked"]) <= set(available[patch]):
            raise ValueError("Champion locks differ from the known patch pool.")
        tags = {k: record[k] for k in ("series_id", "event")}
        ex_ante = trial.before_draft(ctx, **tags)
        return ledger.commit_game(**binding, game_id=game_id, evidence_sha256=digest(files["events"]),
                                  pre_draft_rankings=ex_ante, **ctx, **tags)
    saved = ledger.game_context(trial_id, game_id)
    if (saved["models"] != binding["models"] or saved["protocol_sha256"] != binding["protocol_sha256"]
            or saved["evidence_sha256"] != digest(files["events"])):
        raise ValueError("Saved game context and current trial freeze disagree.")
    ctx = {key: saved[key] for key in CONTEXT}
    ranks, _ = trial.predict(ctx, record["prefix"], event=saved["event"], series_id=saved["series_id"])
    return ledger.record_window(**binding, game_id=game_id, rankings=ranks, prefix=record["prefix"],
                                source=record["source"], observed_at=record["observed_at"])


def main():
    from fearless.prospective_pair_log import TrialLedger

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--trial", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    for command in ("register", "commit-game", "window", "attest-open", "attest-unopened", "outcome", "cancel", "score"):
        cmd = actions.add_parser(command)
        if command in ("register", "commit-game", "window"):
            cmd.add_argument("--freeze", type=Path, required=True)
        if command not in ("register", "score"):
            cmd.add_argument("--game", required=True)
        if command != "score":
            cmd.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    opts = vars(args)
    result = dispatch(args.action, TrialLedger(args.ledger), args.trial, opts.get("game"),
                      read(opts["input"]) if "input" in opts else {}, opts.get("freeze"))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
