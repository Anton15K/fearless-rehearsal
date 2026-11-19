import pytest

from fearless.adaptive_pairs import AdaptivePairModel, FEATURES
from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog
from fearless.rules import schedule


CHAMPIONS = [f"Champion {index:02d}" for index in range(45)] + ["Wukong"]
TARGET = ("Champion 44", "Wukong")


def game(day="2026-04-02", *, finished=None, series_id="event-series", event="Cup"):
    actions = [{"team": team, "kind": kind, "champion": CHAMPIONS[index]}
               for index, (team, kind) in enumerate(schedule("one", "two"))]
    for index, champion in zip((7, 8), reversed(TARGET), strict=True):
        actions[index]["champion"] = champion
    return {"series_id": series_id, "game_id": f"{series_id}-game", "game_number": 1, "series_games": 1,
            "event": event, "date": day, "drafted_at": f"{day}T12:00:00Z",
            "finished_at": f"{finished or day}T13:00:00Z", "patch": "16.7", "region": "Cup region",
            "first": "one", "second": "two", "eligible": True, "finished": True, "fearless": True,
            "actions": actions, "locked": [], "rosters": {
                "two": [{"player": "player-a", "champion": TARGET[0], "role": "jungle"},
                        {"player": "player-b", "champion": TARGET[1], "role": "top"}],
            }}


def new_model():
    weights = {name: 0.0 for name in FEATURES}
    weights["event_window"] = 12
    return AdaptivePairModel({"version": 1, "beam_width": 8, "train_through": "2026-03-28",
                              "features": list(FEATURES), "weights": list(weights.values())})


def context(*, day="2026-04-04", index=7, **kwargs):
    return {"forecaster": DraftForecaster(ChampionCatalog(dict.fromkeys(CHAMPIONS, "")), []),
            "date": day, "patch": "16.7", "region": "Cup region", "first": "one", "second": "two",
            "game_number": 1, "current": game()["actions"][:index], "event": "Cup", **kwargs}


def pairs(results):
    return [tuple(row["champions"]) for row in results]


def test_retrieves_orderless_off_beam_pair_from_a_released_series():
    model = new_model()
    before = model.rank_pairs(**context())
    assert TARGET not in pairs(model.rank_pairs(**context(limit=100)))

    updated = model.with_completed_series([game()])
    result = updated.rank_pairs(**context())

    assert pairs(result)[0] == TARGET
    assert any("2026-04-02" in detail and "event" in detail for detail in result[0]["evidence"])
    assert model.rank_pairs(**context()) == before
    assert updated.with_completed_series([game()]).rank_pairs(**context()) == result


def test_waits_until_the_day_after_series_completion_and_excludes_queried_series():
    original = new_model()
    updated = original.with_completed_series([game(finished="2026-04-04")])

    for day in ("2026-04-02", "2026-04-03", "2026-04-04"):
        assert updated.rank_pairs(**context(day=day)) == original.rank_pairs(**context(day=day))
    assert pairs(updated.rank_pairs(**context(day="2026-04-05")))[0] == TARGET
    same_series = context(day="2026-04-05", series_id="event-series")
    assert updated.rank_pairs(**same_series) == original.rank_pairs(**same_series)


def test_retrieved_pairs_respect_aliases_pool_locks_and_previous_actions():
    model = new_model().with_completed_series([game()])
    pool = [*CHAMPIONS[:-1], "MonkeyKing"]
    assert set(pairs(model.rank_pairs(**context(pool=pool)))[0]) == {TARGET[0], "MonkeyKing"}
    for modified in (context(pool=pool[:-1]), context(pool=pool, locked=["Wukong"]),
                     context(pool=pool, current=[{**game()["actions"][0], "champion": "Wukong"},
                                                 *game()["actions"][1:7]])):
        assert all(set(pair).isdisjoint({"Wukong", "MonkeyKing"}) for pair in pairs(model.rank_pairs(**modified)))


def test_refuses_incomplete_updates_and_only_ranks_double_pick_windows():
    model = new_model()
    with pytest.raises(ValueError, match="Incomplete"):
        model.with_completed_series([{**game(), "series_games": 2}])
    for index in (0, 6, 8, 10, 16, 18, 20):
        assert model.rank_pairs(**context(index=index)) == []
    for index in (7, 9, 17):
        assert model.rank_pairs(**context(index=index))
