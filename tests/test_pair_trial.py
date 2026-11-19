from collections import Counter
from types import SimpleNamespace

import pytest

from fearless.adaptive_pairs import PAIR_WINDOWS
from fearless.pair_trial import METHODS, PairTrial, checked, validate_prefix
from fearless.rules import legal_champions, schedule
from scripts.pair_trial import summarize


def test_pre_draft_ranking_uses_identical_simulated_prefixes_and_is_reproducible():
    pool = tuple(f"C{i:02}" for i in range(24))
    ctx = dict(first="A", second="B", locked=[pool[-1]], pool=pool, date="2026-11-19", patch="16.22",
               game_number=2, region="EMEA")
    seen = Counter()

    def predict(context, prefix, *, event, series_id):
        validate_prefix(context, prefix)
        seen[len(prefix)] += 1
        available = legal_champions(pool, context["locked"], prefix)
        return {k: [tuple(available[:2])] for k in METHODS}, []

    def forecast(*, current, locked, pool, **_):
        return {"choices": [{"champion": c, "probability": 1} for c in legal_champions(pool, locked, current)]}

    fake = SimpleNamespace(frozen=SimpleNamespace(forecast=forecast), predict=predict)
    first = PairTrial.before_draft(fake, ctx, event="Trial", series_id="s")
    assert seen == {i: 32 for i in PAIR_WINDOWS}
    assert first == PairTrial.before_draft(fake, ctx, event="Trial", series_id="s")
    assert len(first) == 3
    for ranks in first.values():
        assert ranks["adaptive"] == ranks["frequency"] == ranks["role_first"]
        assert all(len(set(pair)) == 2 and pool[-1] not in pair for pair in ranks["role_first"])


def test_pair_legality_and_phase_checks_reject_invalid_inputs():
    with pytest.raises(ValueError):
        checked([("A", "B"), ("B", "A")], {"A", "B"})
    ctx = dict(pool=["A", "B"], first="T1", second="T2", locked=["A"])
    team, kind = schedule("T1", "T2")[0]
    with pytest.raises(ValueError):
        validate_prefix(ctx, [{"champion": "A", "team": team, "kind": kind}])


def test_empty_shortlist_and_missing_roles_stay_in_their_correct_denominators():
    rows = [dict(event="E", series="S", role_hit=None, ranks=dict(role_first=0, frequency=0, adaptive=1)),
            dict(event="E", series="S", role_hit=True, ranks=dict(role_first=2, frequency=3, adaptive=0))]
    summary = summarize(rows)
    assert summary["scores"]["role_first"] == dict(turns=2, hit1=0, hits=1, hit3=0.5, mrr3=0.25)
    assert summary["role_top3"] == {"hits": 1, "labeled": 1}
    assert summary["delta_ci95"]["adaptive"] == [0.0, 0.0]
