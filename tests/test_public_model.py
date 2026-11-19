from itertools import cycle, islice
import json
from math import fsum
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from fearless import api
from fearless.public_model import PublicForecaster, SHOWCASE_DATE, SHOWCASE_SERIES, SOURCE, fit_public_model
from fearless.rules import schedule


POOL = [f"Champion {index:02d}" for index in range(50)]
CATALOG = {"16.15": POOL}


def game(series, day, *, number=1, offset=0):
    champions = islice(cycle(POOL), offset, offset + 20)
    return {
        "source": SOURCE, "series_id": series, "game_number": number, "date": day,
        "region": "NA", "patch": "16.15", "first": "coach", "second": "enemy", "fearless": True,
        "actions": [
            {"team": team, "kind": kind, "champion": champion}
            for (team, kind), champion in zip(schedule("coach", "enemy"), champions, strict=True)
        ],
    }


def corpus():
    return [
        game("train", "2026-04-01"),
        game("validation", "2026-04-25", offset=4),
        game("holdout", "2026-05-23", offset=7),
    ]


class PublicModelTests(unittest.TestCase):
    def test_whole_series_and_showcase_rows_do_not_cross_partitions(self):
        eligible = corpus()
        additions = [
            game("crossing", "2026-04-24"),
            game("crossing", "2026-04-25", number=2, offset=20),
            game(SHOWCASE_SERIES, "2026-04-01"),
            game("same-day", SHOWCASE_DATE),
        ]
        expected = fit_public_model(eligible, CATALOG)
        actual = fit_public_model(eligible + additions, CATALOG)

        self.assertEqual(actual["statistics"], expected["statistics"])
        self.assertEqual(actual["temperature"], expected["temperature"])
        self.assertEqual(actual["metrics"], expected["metrics"])
        self.assertEqual(actual["exclusions"]["cross_boundary"], 1)
        self.assertEqual(actual["exclusions"]["showcase"], 1)
        self.assertEqual(actual["exclusions"]["on_or_after_showcase"], 1)

    def test_holdout_scores_do_not_change_statistics_or_temperatures(self):
        original = fit_public_model(corpus(), CATALOG)
        changed = fit_public_model([*corpus()[:2], game("holdout", "2026-05-23", offset=24)], CATALOG)

        self.assertEqual(original["statistics"], changed["statistics"])
        self.assertEqual(original["temperature"], changed["temperature"])
        self.assertNotEqual(original["metrics"]["holdout"], changed["metrics"]["holdout"])

    def test_fitting_rejects_a_non_public_input(self):
        rows = corpus()
        rows[0]["source"] = "GRID"
        with self.assertRaisesRegex(ValueError, "public"):
            fit_public_model(rows, CATALOG)

    def test_probability_normalizes_over_every_legal_champion(self):
        model = PublicForecaster(fit_public_model(corpus(), CATALOG))
        used = POOL[20]
        locked = POOL[21]
        disabled = POOL[22]
        current = [{"team": "coach", "kind": "ban", "champion": used}]

        supplied = [champion for champion in POOL if champion != disabled] + ["Not released"]
        result = model.forecast(
            date=SHOWCASE_DATE, patch="16.15", region="NA", first="coach", second="enemy",
            game_number=2, current=current, locked=[locked], pool=supplied, limit=len(POOL),
        )

        self.assertEqual(result["team"], "enemy")
        self.assertEqual(result["method"], "public_frequencies")
        self.assertEqual(result["probability_condition"], "named_champion_action")
        self.assertEqual(result["probability_quality"], "estimated")
        self.assertFalse(result["calibrated"])
        self.assertEqual({choice["champion"] for choice in result["choices"]}, set(POOL) - {used, locked, disabled})
        self.assertTrue(all(choice["probability"] > 0 for choice in result["choices"]))
        self.assertAlmostEqual(fsum(choice["probability"] for choice in result["choices"]), 1)

        top = model.forecast(
            date=SHOWCASE_DATE, patch="16.15", region="NA", first="coach", second="enemy",
            game_number=2, current=current, locked=[locked], pool=supplied, limit=3,
        )
        self.assertEqual(top["choices"], result["choices"][:3])
        self.assertLess(fsum(choice["probability"] for choice in top["choices"]), 1)

    def test_catalog_patch_is_required_and_fitted_model_cannot_forecast_past(self):
        model = PublicForecaster(fit_public_model(corpus(), CATALOG))
        context = dict(region="NA", first="coach", second="enemy", game_number=1, current=[], pool=POOL)

        self.assertEqual(model.forecast(date="2026-04-25", patch="16.15", **context)["status"], "unavailable")
        self.assertEqual(model.forecast(date=SHOWCASE_DATE, patch="16.16", **context)["status"], "no_candidates")

    def test_published_artifact_has_distinct_validation_and_holdout(self):
        model = PublicForecaster.load(Path(__file__).parents[1] / "fearless/assets/public_forecast.json")
        partitions = model.artifact["partitions"]

        self.assertLess(partitions["train"]["through"], partitions["validation"]["from"])
        self.assertLess(partitions["validation"]["through"], partitions["holdout"]["from"])
        self.assertLess(partitions["holdout"]["through"], SHOWCASE_DATE)
        for kind in ("pick", "ban"):
            for method in ("public_frequencies", "baseline"):
                metrics = model.artifact["metrics"]["holdout"][kind][method]
                self.assertGreater(metrics["turns"], 0)
                self.assertTrue(0 <= metrics["hit_at_3"] <= 1)
                self.assertTrue(0 <= metrics["top1_ece"] <= 1)
                self.assertGreater(metrics["log_loss"], 0)

    def test_public_replay_serves_estimated_paths_without_the_private_model(self):
        first = game(SHOWCASE_SERIES, SHOWCASE_DATE)
        second = game(SHOWCASE_SERIES, SHOWCASE_DATE, number=2, offset=20)
        first["team_names"] = second["team_names"] = {"coach": "Cloud9", "enemy": "Dignitas"}
        second["first"], second["second"] = second["second"], second["first"]
        for action, (team, _kind) in zip(second["actions"], schedule("enemy", "coach"), strict=True):
            action["team"] = team

        class PrivateForecaster:
            def forecast(self, **_context):
                raise AssertionError("The public replay must use its public estimate")

        with TemporaryDirectory() as directory:
            assets = Path(directory)
            (assets / "public_forecast.json").write_text(json.dumps(fit_public_model(corpus(), CATALOG)))
            (assets / "public_games.jsonl").write_text("\n".join(map(json.dumps, [first, second])))
            (assets / "champions.json").write_text(json.dumps(CATALOG))

            with patch.object(api, "ASSETS", assets), TestClient(api.create_app(
                database=assets / "plans.sqlite3", forecaster=PrivateForecaster(),
            )) as client:
                created = client.post("/api/plans", json={"match_id": SHOWCASE_SERIES})
                self.assertEqual(created.status_code, 201, created.text)
                plan = created.json()
                response = client.get(f"/api/plans/{plan['id']}/nodes/{plan['selected_node']}")

        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(state["forecast"]["probability_quality"], "estimated")
        self.assertFalse(state["forecast"]["calibrated"])
        self.assertGreater(state["suggestions"][0]["score"], 0)
        self.assertEqual(state["outlook"]["probability_quality"], "estimated")
        self.assertEqual(state["outlook"]["horizon"], 1)
        self.assertAlmostEqual(state["outlook"]["observed_mass"] + state["outlook"]["residual_mass"], 1)
