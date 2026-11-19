import pytest

from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog, ROLES
from fearless.role_first_pairs import RoleFirstPairModel
from fearless.rules import schedule


CHAMPIONS = tuple(f"C{i:02d}" for i in range(45)) + ("Wukong",)


def game(series, day, *, flip=False, finished=None):
    actions = [{"team": team, "kind": kind, "champion": CHAMPIONS[i]}
               for i, (team, kind) in enumerate(schedule("blue", "red"))]
    roles = {6: "mid" if flip else "top", 9: "jungle", 10: "bot",
             17: "top" if flip else "mid", 18: "support"}
    rosters = {"blue": [{"champion": actions[slot]["champion"], "player": f"p-{role}", "role": role}
                         for slot, role in roles.items()],
               "red": [{"champion": actions[slot]["champion"], "player": f"r-{role}", "role": role}
                       for slot, role in zip((7, 8, 11, 16, 19), ROLES, strict=True)]}
    return {"series_id": series, "game_id": series + "/1", "series_games": 1, "game_number": 1,
            "date": day, "drafted_at": day+"T19:00:00Z", "finished_at": (finished or day)+"T20:00:00Z",
            "event": "Practice", "patch": "16.20", "region": "CN", "rosters": rosters,
            "first": "blue", "second": "red", "available": CHAMPIONS, "disabled": [], "locked": [],
            "eligible": True, "finished": True, "fearless": True, "actions": actions}


def model():
    return RoleFirstPairModel({"version": 1, "fit_through": "2026-04-30", "patch_strength": 12.0,
                               "pair_strength": 20.0, "flex_power": 1.0})


def context(day="2026-05-10", index=17, **options):
    base = {"date": day, "patch": "16.20", "region": "CN", "first": "blue", "second": "red", "event": "Practice",
            "game_number": 1, "current": game("prefix", day)["actions"][:index], "pool": CHAMPIONS,
            "forecaster": DraftForecaster(ChampionCatalog({}, patches={"16.20": CHAMPIONS}), [])}
    return {**base, **options}


def test_flex_champion_leaves_two_plausible_role_pairs():
    rows = [game("1", "2026-05-01"), game("2", "2026-05-02", flip=True),
            game("3", "2026-05-03"), game("4", "2026-05-04", flip=True)]
    current = model().with_completed_series(rows)
    output = current.rank_roles(**context(), limit=10)
    assert sum(row["probability"] for row in output) == pytest.approx(1)
    scores = {tuple(row["roles"]): row["probability"] for row in output}
    assert scores["top", "support"] > 0.25
    assert scores["mid", "support"] > 0.25
    assert scores["jungle", "bot"] < 0.05


def test_never_observes_a_same_day_or_future_roster_or_replayed_series():
    cold = model()
    row = game("not-yet", "2026-05-01", finished="2026-05-02")
    updated = cold.with_completed_series([row])
    for day in ("2026-05-01", "2026-05-02"):
        args = context(day, rosters={"blue": ["p-top", "p-mid"]})
        assert updated.rank_pairs(**args) == cold.rank_pairs(**args)
        assert updated.rank_roles(**args) == cold.rank_roles(**args)
        assert cold.rank_pairs(**args) == cold.rank_pairs(**context(day))
    omitted = context("2026-05-03", series_id="not-yet")
    assert updated.rank_pairs(**omitted) == cold.rank_pairs(**omitted)
    assert updated.rank_pairs(**context("2026-05-03")) != cold.rank_pairs(**context("2026-05-03"))


def test_returns_distinct_deterministic_legal_pairs_in_the_three_windows():
    fitted = model().with_completed_series([game("released", "2026-05-02")])
    enabled = (*CHAMPIONS[:-1], "MonkeyKing")
    for window in (7, 9, 17):
        args = context(index=window, pool=enabled, locked=["Wukong", "C42"], limit=15)
        result = fitted.rank_pairs(**args)
        assert result == fitted.rank_pairs(**{**args, "pool": reversed(enabled)})
        assert len(result) == 15
        unavailable = {"MonkeyKing", "Wukong", "C42"} | {a["champion"] for a in args["current"]}
        assert len({tuple(row["champions"]) for row in result}) == 15
        for row in result:
            assert len(set(row["champions"])) == 2
            assert set(row["champions"]).isdisjoint(unavailable)
            assert all(isinstance(line, str) for line in row["evidence"])
    for window in (0, 6, 8, 10, 12, 16, 18, 20):
        assert fitted.rank_pairs(**context(index=window)) == []
