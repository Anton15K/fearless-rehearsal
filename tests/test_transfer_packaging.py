import json

import pytest

from fearless.event_forecaster import digest
from scripts.promote_transfer import verify_extension


def test_production_evidence_must_equal_the_verified_sources(tmp_path):
    def policy(name, content):
        events = tmp_path / (name + ".jsonl")
        events.write_text(content)
        ref = dict(path=events.name, sha256=digest(events))
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(dict(incumbent="base", parent_sha256="parent", recipe="refresh", files={"events": ref})))
        return path

    prior = policy("previous", '{"game":1}\n')
    cohort = tmp_path / "cohort.jsonl"
    cohort.write_text('{"game":2}\n')
    lock = dict(sha256=digest(cohort), prior_sha256=digest(tmp_path / "previous.jsonl"))
    current = policy("current", '{"game":2}\n{"game":1}\n')
    assert verify_extension(prior, current, cohort, lock)["recipe"] == "refresh"
    wrong = policy("wrong", '{"game":2}\n{"game":3}\n')
    with pytest.raises(ValueError, match="union"):
        verify_extension(prior, wrong, cohort, lock)
