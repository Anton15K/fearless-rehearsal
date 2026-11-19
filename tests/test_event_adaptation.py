from copy import deepcopy
from types import SimpleNamespace

import pytest

from fearless.challengers import DraftChallengers
from fearless.event_forecaster import EventForecaster
from fearless.event_series import completed_series
from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog
from fearless.rules import schedule, series_locks
from scripts.evaluate_adaptation import intervals


CHAMPIONS = [f"Champion {i:02d}" for i in range(55)]


def game(series, number, size, day, names, locked=()):
    first, second = "home", "away"
    actions = [dict(team=t, kind=k, champion=name) for (t, k), name in zip(schedule(first, second), names)]
    return dict(series_id=series, game_id=f"{series}/{number}", event="Regional split", date=day, patch="16.20",
                region="CN", first=first, second=second, game_number=number, series_games=size, eligible=True,
                fearless=True, finished=True, drafted_at=day+"T19:00:00Z", finished_at=day+"T20:00:00Z",
                disabled=[], actions=actions, locked=list(locked))


@pytest.fixture
def rows():
    one = game("S1", 1, 2, "2026-10-15", CHAMPIONS[:20])
    return [one, game("S1", 2, 2, "2026-10-16", CHAMPIONS[20:40], series_locks([one["actions"]])),
            game("S2", 1, 1, "2026-10-19", CHAMPIONS[:20])]


def test_release_uses_last_game_utc_completion_and_omits_replayed_series(rows):
    two = rows[1]
    two.update(drafted_at="2026-10-16T23:50:00Z", finished_at="2026-10-17T01:12:00Z")
    parent = DraftChallengers(DraftForecaster(ChampionCatalog.from_json({"16.20": CHAMPIONS}), []))
    model = EventForecaster(parent, rows, recipe="refresh")
    assert model._snapshot("2026-10-17", "S2")[-1] == 0
    _, _, updated, last, count = model._snapshot("2026-10-18", "S2")
    assert count == 1 and last == "2026-10-17"
    assert updated._history("2026-10-17").as_of is None
    assert updated._history("2026-10-18").as_of == "2026-10-17"
    assert model._snapshot("2026-10-20", "S1")[-1] == 1
    assert {g["series_id"] for g in model._snapshot("2026-10-20", "S1")[2].games} == {"S2"}


@pytest.mark.parametrize("error", ["truncated", "reused", "wrong size", "swapped teams", "unlocked", "disabled", "offset", "out of order"])
def test_rejects_invalid_evidence_before_exposing_any_game(rows, error):
    bad = deepcopy(rows)
    if error == "truncated":
        bad.pop(1)
    elif error == "reused":
        bad[1]["game_id"] = bad[0]["game_id"]
    elif error == "wrong size":
        bad[0]["series_games"] = 3
    elif error == "swapped teams":
        bad[1]["second"] = "elsewhere"
    elif error == "unlocked":
        bad[1]["locked"] = []
    elif error == "disabled":
        bad[0]["disabled"] = [bad[0]["actions"][0]["champion"]]
    elif error == "offset":
        bad[0]["drafted_at"] = "2026-10-15T19:00:00"
    else:
        bad[0]["finished_at"] = "2026-10-18T20:00:00Z"
    with pytest.raises(ValueError):
        completed_series(bad)


def test_series_bootstrap_resamples_within_each_event_without_changing_mix():
    rows = [(e, sid, {"candidate": (a,), "incumbent": (b,)})
            for e, sid, a, b in [("east", "1", 1, 0), ("west", "2", 0, 1), ("west", "3", 0, 1)]]
    assert intervals(rows, 0) == pytest.approx([-1/3, -1/3])
    assert intervals(rows[::-1], 0) == intervals(rows, 0)


def test_attached_player_evidence_matches_both_revealed_drafts_and_uses_only_earlier_ids(rows):
    from scripts.event_rosters import attach
    from fearless.model_features import ROLES
    g = rows[0]
    catalog = ChampionCatalog.from_json({"16.20": CHAMPIONS})
    players = []
    for team in ("home", "away"):
        champs = [a["champion"] for a in g["actions"] if a["kind"] == "pick" and a["team"] == team]
        for role, champ in zip(ROLES, champs):
            players.append(dict(Team=team.upper(), Role=role.title(), Champion=champ, Link=team+role, Name=team+role))
    known = {("home", "hometop"): {"same-day": g["date"], "previous": "2026-10-13"}}
    enriched = attach(g, players, catalog, known)
    assert enriched["team_names"] == {"home": "HOME", "away": "AWAY"}
    assert enriched["rosters"]["home"][0]["player"] == "previous"
    assert enriched["rosters"]["away"][0]["player"] == "leaguepedia:player:awaytop"
    assert attach(g, players[:-1], catalog, known) is g
    players[0]["Champion"] = players[-1]["Champion"]
    assert attach(g, players, catalog, known) is g
