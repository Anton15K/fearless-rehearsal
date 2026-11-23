import json

import pytest

from fearless.app_catalog import DEMO_POOL, MatchCatalog
from fearless.rehearsal import advance_history, create_catalog_plan, node_state
from fearless.rules import schedule


SERIES = "994442840205359781"
PATCH = "16.17"


def historical_catalog(tmp_path, changes, patches=None):
    rows = []
    for number, change in enumerate(changes, 1):
        first, second = ("alpha", "beta") if number % 2 else ("beta", "alpha")
        rows.append({
            "series_id": SERIES, "game_number": number, "team_names": {"alpha": "Alpha", "beta": "Beta"},
            "date": "2026-08-01", "patch": PATCH, "first": first, "second": second,
            "actions": [
                {"team": team, "kind": kind, "champion": champion}
                for (team, kind), champion in zip(
                    schedule(first, second), DEMO_POOL[(number - 1) * 20:number * 20], strict=True,
                )
            ],
            **change,
        })
    games = tmp_path / "games.jsonl"
    games.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")
    path = None
    if patches is not None:
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps(patches), encoding="utf8")
    return MatchCatalog(games, path)


@pytest.mark.parametrize("changes", [
    [{"winner": "alpha"}, {"winner": "alpha"}, {"winner": "beta"}],
    [{"winner": "alpha"}, {"winner": "outsider"}, {}],
    [{"winner": "alpha"}, {"winner": 0}, {}],
])
def test_rejects_invalid_historical_winner_sequences(tmp_path, changes):
    catalog = historical_catalog(tmp_path, changes, {PATCH: DEMO_POOL})
    assert SERIES not in catalog.matches


@pytest.mark.parametrize("changes", [
    [{"format": "Bo3"}, {"best_of": 5}, {}],
    [{"format": "Bo3", "best_of": 5}, {}, {}],
    [{"best_of": 3}, {"format": "Bo1"}, {}],
    [{}, {"best_of": True}, {}],
])
def test_rejects_inconsistent_historical_format_declarations(tmp_path, changes):
    catalog = historical_catalog(tmp_path, changes, {PATCH: DEMO_POOL})
    assert SERIES not in catalog.matches


@pytest.mark.parametrize("changes", [
    [{"date": "2026-08-02"}, {}, {}],
    [{"drafted_at": "2026-08-01T15:00:00Z", "finished_at": "2026-08-01T14:00:00Z"}, {}, {}],
    [{"finished_at": "2026-08-01T15:00:00Z"}, {}, {"drafted_at": "2026-08-01T14:55:00Z"}],
    [{"started_at": "2026-08-01T15:00:00Z"}, {"drafted_at": "2026-08-01T14:55:00Z"}, {}],
    [
        {"drafted_at": "2026-08-01T14:40:00Z", "started_at": "2026-08-01T15:00:00Z"},
        {"drafted_at": "2026-08-01T14:55:00Z"},
        {},
    ],
    [{"drafted_at": "2026-08-01T15:00:00Z", "started_at": "2026-08-01T14:55:00Z"}, {}, {}],
    [{"finished_at": "2026-08-01T15:00:00Z"}, {"finished_at": "2026-08-01T15:00:00Z"}, {}],
])
def test_rejects_impossible_historical_game_timing(tmp_path, changes):
    catalog = historical_catalog(tmp_path, changes, {PATCH: DEMO_POOL})
    assert SERIES not in catalog.matches


def test_catalog_keeps_unknown_winners_and_partial_timestamps(tmp_path):
    catalog = historical_catalog(tmp_path, [
        {"winner": None, "finished_at": "2026-08-01T14:00:00Z"},
        {"best_of": 3, "drafted_at": "2026-08-01T10:00:00-04:00"},
        {"winner": "beta", "format": "Bo3", "finished_at": "2026-08-01T15:00:00Z"},
    ], {PATCH: DEMO_POOL})
    match = catalog.matches[SERIES]
    assert match.metadata["format"] == "Bo3"
    assert match.metadata["prior_winners"] == [None]
    assert match.historical[2]["winner"] is None
    assert match.historical[3]["winner"] == "Beta"

    plan = create_catalog_plan(catalog, {"match_id": SERIES})
    opening = node_state(plan, plan["root_id"])
    assert opening["score"] == {"Alpha": 0, "Beta": 0}
    assert opening["score_complete"] is False
    for _ in range(20):
        advance_history(plan, plan["selected_node"])
    terminal = node_state(plan, plan["selected_node"])
    assert terminal["game_winner"] is None
    assert terminal["history"]["kind"] == "next_game"


def test_known_format_may_first_appear_on_a_later_readable_row(tmp_path):
    catalog = historical_catalog(tmp_path, [{}, {}, {"best_of": 5}], {PATCH: DEMO_POOL})
    assert catalog.matches[SERIES].metadata["format"] == "Bo5"


@pytest.mark.parametrize("patches", [{PATCH: DEMO_POOL}, {}])
def test_historical_replay_requires_every_patch_in_a_supplied_catalog(tmp_path, patches):
    catalog = historical_catalog(tmp_path, [{}, {}, {"patch": "15.1"}], patches)
    assert SERIES not in catalog.matches


def test_catalog_free_legacy_replay_can_still_use_the_demo_pool(tmp_path):
    catalog = historical_catalog(tmp_path, [{}, {}, {"patch": "15.1"}])
    plan = create_catalog_plan(catalog, {"match_id": SERIES})
    assert catalog.matches[SERIES].historical[3]["patch"] == "15.1"
    assert node_state(plan, plan["root_id"])["history"] is not None
