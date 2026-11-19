import math

import numpy as np

from fearless.model import DraftForecaster
from fearless.model_features import ChampionCatalog
from fearless.pair_model import PairChallenger, PairHistory, _beam, _episodes, _fit, _role_assignments, pair_turns, whole_series
from fearless.rules import schedule


def test_sums_unordered_paths_without_renormalizing_omitted_replies():
    def forecast(prefix, limit):
        rows = {
            None: [("A", .4), ("B", .3)], "A": [("B", .6), ("C", .1)], "B": [("A", .2), ("C", .2)],
        }
        return {"choices": [{"champion": name, "probability": p} for name, p in rows[prefix[-1]["champion"] if prefix else None][:limit]]}

    pairs, shipped = _beam(forecast, (), "two", 8, {"A", "B", "C"})
    assert math.isclose(pairs["A", "B"], .3)
    assert math.isclose(sum(pairs.values()), .4)
    assert pairs == shipped


def test_distinct_role_matching_handles_flex_and_duplicate_lanes():
    top, mid, jungle = 1, 2, 4
    assert _role_assignments([top | mid, mid, jungle])
    assert not _role_assignments([top, mid, mid])
    assert not _role_assignments([top | mid, top | mid, top | mid])


def test_same_day_pair_games_and_future_players_never_enter_history():
    actions = [{"team": team, "kind": kind, "champion": str(index)}
               for index, (team, kind) in enumerate(schedule("one", "two"))]
    row = {"date": "2025-03-02", "eligible": True, "actions": actions,
           "rosters": {"two": [{"player": "known", "champion": "7"}]}}
    history = PairHistory([row, {**row, "date": "2025-03-04"}])

    assert list(pair_turns(actions)) == [(7, "two", ("7", "8")), (9, "one", ("10", "9")), (17, "one", ("17", "18"))]
    assert history.counts(("7", "8"), "two", ["known"], "2025-03-02") == (0, 0, 0)
    assert history.counts(("7", "8"), "two", ["known"], "2025-03-04") == (1, 1, 1)
    assert history.counts(("7", "8"), "one", ["other"], "2025-03-04") == (1, 0, 0)


def test_uses_whole_series_on_both_sides_of_each_boundary():
    games = [dict(series_id="crossing", date=day, eligible=True) for day in ("2025-12-31", "2026-01-01")]
    within = {"series_id": "within", "date": "2025-09-01", "eligible": True}
    assert whole_series([*games, within], "2025-01-01", "2025-12-31") == [within]


def test_activation_rejects_non_pair_positions_without_scoring():
    model = PairChallenger({"version": 1, "beam_width": 8, "train_through": "2025-12-31",
                            "weights": [0.1, 0.1, 0.1, 0.1]})
    assert model.rank_pairs(forecaster=None, date="2026-04-01", patch="16.1", region="NA", first="one", second="two",
                            game_number=1, current=[]) == []


def test_offline_replay_uses_the_same_frozen_beam_and_avoids_early_learned_scores():
    class Learned:
        calls = 0

        def predict(self, values, *, thread_count):
            self.calls += 1
            return np.linspace(-2, 2, len(values))

    champions = [f"Champion {index:02d}" for index in range(40)]
    catalog = ChampionCatalog({champion: "" for champion in champions})
    learned = Learned()
    forecaster = DraftForecaster(catalog, [], models={"pick": learned},
                                 serving={"pick": {"method": "learned", "temperature": 1}})
    actions = [{"team": team, "kind": kind, "champion": champions[index]}
               for index, (team, kind) in enumerate(schedule("one", "two"))]
    early = {"date": "2025-11-04", "eligible": True, "patch": "15.15", "series_id": "earlier",
             "first": "one", "second": "two", "game_number": 1, "region": "NA", "actions": actions, "locked": []}
    assert len(list(_episodes([early], forecaster, 8))) == 3
    assert learned.calls == 0

    held_out = {**early, "date": "2026-04-01", "patch": "16.7", "series_id": "later"}
    wide, shipped = list(_episodes([held_out], forecaster, 8))[0][2:4]
    assert learned.calls > 0
    current = actions[:7]

    def live(prefix, limit):
        return forecaster.forecast(date="2026-04-01", patch="16.7", region="NA", first="one", second="two",
                                   game_number=1, current=prefix, limit=limit)

    assert _beam(live, current, "two", 8, set(champions[7:])) == (wide, shipped)
    model = PairChallenger({"version": 1, "beam_width": 8, "train_through": "2025-12-31",
                            "weights": [0.1, 0.1, 0.1, 0.1]})
    ranked = model.rank_pairs(forecaster=forecaster, date="2026-04-01", patch="16.7", region="NA", first="one",
                              second="two", game_number=1, current=current, pool=champions, locked=[champions[-1]])
    assert len(ranked) == 3
    assert all(set(row) == {"champions"} and len(set(row["champions"])) == 2 for row in ranked)
    assert all(set(row["champions"]) <= set(champions[7:-1]) for row in ranked)


def test_fitter_learns_a_pair_feature_only_from_represented_targets():
    a, b = ("A", "B"), ("C", "D")
    mass = {a: 0.10, b: 0.10}
    features = {a: (0, 1, 0, 0), b: (0, 0, 0, 0)}
    represented = ("series", a, mass, {}, features, True)
    outside_beam = ("other", ("E", "F"), mass, {}, features, True)
    weights, fitted = _fit([represented, outside_beam])
    assert fitted == 1
    assert weights[1] > 0
    assert all(weight == 0 for index, weight in enumerate(weights) if index != 1)
