import unittest

from fearless.outlook import lookahead
from fearless.rules import pin_status, schedule


def action(team, kind, champion):
    return {"team": team, "kind": kind, "champion": champion}


def prefix(length):
    return [
        action(team, kind, f"Used {index}" if kind == "pick" else None)
        for index, (team, kind) in enumerate(schedule("A", "B")[:length])
    ]


def reply(*choices, method="baseline"):
    return {
        "status": "scored",
        "method": method,
        "choices": [
            {"champion": champion, "probability": probability} for champion, probability in choices
        ],
    }


class OutlookTests(unittest.TestCase):
    def test_two_opponent_picks_sum_unordered_paths_and_preserve_unseen_mass(self):
        current = prefix(7)
        pool = ["Used 6", "X", "Y", "Z"]
        requested = []

        def forecast(actions):
            requested.append(len(actions))
            if len(actions) == 7:
                return reply(("X", 0.6), ("Y", 0.3))
            if actions[-1]["champion"] == "X":
                return reply(("Y", 0.5), ("Z", 0.4))
            return reply(("X", 0.4), ("Z", 0.5))

        result = lookahead(
            pool=pool, locked=(), current=current, first="A", second="B", coach_team="A", forecast=forecast,
            evaluate_pin=lambda actions: pin_status(pool, [actions], 2, "X"),
        )

        self.assertEqual(requested, [7, 8, 8])
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["method"], "baseline")
        self.assertEqual(result["horizon"], 2)
        self.assertEqual(result["conditional"], "opponent_actions_given_named_champion")
        self.assertEqual(result["pairs"][0]["champions"], ["X", "Y"])
        self.assertAlmostEqual(result["pairs"][0]["probability"], 0.42)
        self.assertAlmostEqual(result["observed_mass"], 0.81)
        self.assertAlmostEqual(result["residual_mass"], 0.19)
        probabilities = {row["status"]: row["probability"] for row in result["outcomes"]}
        self.assertAlmostEqual(probabilities["conflict"], 0.66)
        self.assertAlmostEqual(probabilities["still_possible"], 0.15)
        self.assertEqual(len(current), 7)

    def test_stops_before_an_unprovided_coach_turn(self):
        requested = []

        def forecast(actions):
            requested.append(len(actions))
            return reply(("X", 0.8))

        result = lookahead(pool=["X", "Y"], locked=(), current=prefix(1), first="A", second="B",
                           coach_team="A", forecast=forecast)
        self.assertEqual(requested, [1])
        self.assertEqual(result["horizon"], 1)
        self.assertEqual(result["pairs"], [])
        self.assertAlmostEqual(result["residual_mass"], 0.2)

        result = lookahead(pool=["X", "Y"], locked=(), current=prefix(0), first="A", second="B",
                           coach_team="A", forecast=forecast)
        self.assertEqual(requested, [1])
        self.assertEqual(result["status"], "awaiting_coach")
        self.assertEqual(result["horizon"], 0)

    def test_only_explicit_coach_action_is_applied(self):
        received = []

        def forecast(actions):
            received.extend(actions)
            return reply(("Y", 0.8))

        result = lookahead(
            pool=["X", "Y", "Z"], locked=(), current=[], first="A", second="B", coach_team="A",
            forecast=forecast, coach_action=action("A", "ban", "X"),
            evaluate_pin=lambda actions: pin_status(["X", "Y", "Z"], [actions], 2, "X"),
        )

        self.assertEqual(received, [action("A", "ban", "X")])
        self.assertEqual(result["conditional"], "coach_action_and_opponent_actions_given_named_champion")
        self.assertEqual(result["outcomes"], [{"status": "protected", "probability": 0.8, "branches": 1}])
        self.assertEqual(result["pairs"], [])

    def test_unscorable_continuation_remains_residual_mass(self):
        def forecast(actions):
            if len(actions) == 7:
                return reply(("X", 0.6), ("Y", 0.4))
            if actions[-1]["champion"] == "X":
                return {"status": "unavailable", "choices": []}
            return reply(("Z", 0.9))

        result = lookahead(pool=["Used 6", "X", "Y", "Z"], locked=(), current=prefix(7), first="A",
                           second="B", coach_team="A", forecast=forecast)
        self.assertAlmostEqual(result["observed_mass"], 0.36)
        self.assertAlmostEqual(result["residual_mass"], 0.64)

    def test_final_action_can_resolve_the_pin_but_does_not_cross_game_boundary(self):
        current = prefix(19)
        pool = [move["champion"] for move in current if move["champion"] is not None] + ["X", "Y"]
        requested = []

        def forecast(actions):
            requested.append(len(actions))
            return reply(("X", 0.7), ("Y", 0.3))

        result = lookahead(
            pool=pool, locked=(), current=current, first="A", second="B", coach_team="A", forecast=forecast,
            evaluate_pin=lambda actions: pin_status(pool, [actions], 2, "X"),
        )

        self.assertEqual(requested, [19])
        self.assertEqual(result["horizon"], 1)
        self.assertEqual(result["residual_mass"], 0)
        self.assertEqual(result["outcomes"], [
            {"status": "conflict", "probability": 0.7, "branches": 1},
            {"status": "protected", "probability": 0.3, "branches": 1},
        ])

    def test_no_legal_ban_forces_skip_without_model_inference(self):
        def forecast(_actions):
            self.fail("The forecaster should not receive a turn with zero legal champion bans")

        result = lookahead(pool=[], locked=(), current=[], first="A", second="B", coach_team="B",
                           forecast=forecast, evaluate_pin=lambda actions: "conflict" if actions[-1]["champion"] is None else "protected")
        self.assertEqual(result["observed_mass"], 1)
        self.assertEqual(result["residual_mass"], 0)
        self.assertEqual(result["outcomes"][0]["status"], "conflict")

    def test_lookahead_rejects_out_of_scope_or_illegal_actions(self):
        arguments = dict(pool=["X", "Y"], locked={"X"}, current=[], first="A", second="B", coach_team="B")
        with self.assertRaises(ValueError):
            lookahead(**arguments, forecast=lambda _actions: reply(("X", 0.9)))
        with self.assertRaises(ValueError):
            lookahead(**arguments, forecast=lambda _actions: reply(("Y", 1.1)))
        with self.assertRaises(ValueError):
            lookahead(**arguments, forecast=lambda _actions: reply(("Y", 0.9)), coach_action=action("A", "ban", "Y"))
        with self.assertRaises(ValueError):
            lookahead(**arguments, forecast=lambda _actions: reply(("Y", 0.9)), horizon=3)


if __name__ == "__main__":
    unittest.main()
