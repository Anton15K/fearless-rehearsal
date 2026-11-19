"""Write the private serving manifest for the independently tested transfer components."""
import argparse
from collections import Counter
import json
from os.path import relpath
from pathlib import Path

from fearless.challengers import incumbent_signature
from fearless.event_forecaster import digest
from scripts.compare_challengers import write_json
from scripts.transfer_forecasts import passed


def verified_events(manifest, policy):
    ref = policy["files"]["events"]
    path = manifest.parent / ref["path"]
    if digest(path) != ref["sha256"]:
        raise ValueError("Tournament evidence differs from its declared snapshot.")
    return path


def verify_extension(prior_policy, event_policy, cohort, lock):
    prior = json.loads(prior_policy.read_text())
    current = json.loads(event_policy.read_text())
    for key in ("incumbent", "parent_sha256", "recipe"):
        if prior[key] != current[key]:
            raise ValueError("The tournament policy changed during the evidence refresh.")
    def models(policy):
        return {name: ref["sha256"] for name, ref in policy["files"].items() if name != "events"}

    previous_events = verified_events(prior_policy, prior)
    serving_events = verified_events(event_policy, current)
    if (models(prior) != models(current) or digest(previous_events) != lock["prior_sha256"]
            or digest(cohort) != lock["sha256"]):
        raise ValueError("The serving inputs differ from the confirmed tournament policies.")
    prior_rows = Counter(previous_events.read_bytes().splitlines())
    new_rows = Counter(cohort.read_bytes().splitlines())
    if Counter(serving_events.read_bytes().splitlines()) != prior_rows + new_rows:
        raise ValueError("Expected the union of the prior and newly audited tournament evidence.")
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("bundle", "parent", "prior-policy", "event-policy", "games", "confidence", "result", "lock", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.result.read_text())
    lock = json.loads(args.lock.read_text())
    event = verify_extension(args.prior_policy, args.event_policy, args.games, lock)
    if (report["promoted"] != passed(report["overall"]) or report["illegal_predictions"] != 0
            or report["manifest_sha256"] != digest(args.lock) or report["games_sha256"] != lock["sha256"]
            or any(report[key] != lock[key] for key in ("games", "series", "prior_sha256", "catalog_sha256", "confidence_sha256", "parent_sha256"))
            or report["strata"] != sorted(lock["events"]) or report["games_by_event"] != lock["events"]
            or report["coverage"]["unchanged_pick_rankings"] != 10 * lock["games"]
            or report["confidence_sha256"] != digest(args.confidence) or report["parent_sha256"] != digest(args.parent)
            or report["incumbent"] != incumbent_signature(args.bundle) or event["parent_sha256"] != report["parent_sha256"]
            or event["incumbent"] != report["incumbent"] or event["recipe"] != "refresh"):
        raise ValueError("The evaluation report disagrees with the requested serving artifacts.")
    confidence = {"path": relpath(args.confidence.resolve(), args.output.parent.resolve()), "sha256": digest(args.confidence)}
    write_json(args.output, {"version": 1, "event_policy_sha256": digest(args.event_policy),
                             "evaluation_sha256": digest(args.result), "ban": report["promoted"]["ban"],
                             "confidence": confidence if report["promoted"]["confidence"] else None})


if __name__ == "__main__":
    main()
