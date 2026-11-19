import unittest

from fearless import rules


def action(team, kind, champion):
    return {"team": team, "kind": kind, "champion": champion}


TOURNAMENT_ORDER = [
    ("A", "ban"), ("B", "ban"), ("A", "ban"), ("B", "ban"), ("A", "ban"), ("B", "ban"),
    ("A", "pick"), ("B", "pick"), ("B", "pick"), ("A", "pick"), ("A", "pick"), ("B", "pick"),
    ("B", "ban"), ("A", "ban"), ("B", "ban"), ("A", "ban"),
    ("B", "pick"), ("A", "pick"), ("A", "pick"), ("B", "pick"),
]


def recorded_game(prefix):
    return [action(team, kind, f"{prefix}-{index}") for index, (team, kind) in enumerate(TOURNAMENT_ORDER)]


class DraftRulesTests(unittest.TestCase):
    def test_twenty_steps_follow_draft_order_for_either_team(self):
        self.assertEqual(rules.schedule("A", "B"), TOURNAMENT_ORDER)
        swapped = [("B" if team == "A" else "A", kind) for team, kind in TOURNAMENT_ORDER]
        self.assertEqual(rules.schedule("B", "A"), swapped)

    def test_a_team_cannot_fill_both_draft_positions(self):
        with self.assertRaises(ValueError):
            rules.schedule("A", "A")

    def test_legal_pool_excludes_previous_picks_and_current_selections(self):
        pool = ["thresh", "ahri", "rakan", "xayah", "orianna", "lee-sin"]
        current = [action("A", "ban", "rakan"), action("B", "ban", None), action("A", "pick", "xayah")]

        self.assertEqual(rules.legal_champions(pool, {"ahri"}, current), ["thresh", "orianna", "lee-sin"])

    def test_illegal_actions_raise_value_error(self):
        pool = ["thresh", "ahri", "rakan"]
        current = [action("A", "ban", "rakan")]

        for team, kind, champion in [
            ("A", "ban", "thresh"),
            ("B", "pick", "thresh"),
            ("B", "ban", "rakan"),
            ("B", "ban", "ahri"),
            ("B", "ban", "disabled-champion"),
            ("B", "ban", ["thresh"]),
        ]:
            with self.subTest(team=team, kind=kind, champion=champion), self.assertRaises(ValueError):
                rules.validate_action(pool, {"ahri"}, current, "A", "B", team, kind, champion)

    def test_a_ban_can_be_skipped_but_a_pick_requires_a_champion(self):
        rules.validate_action([], set(), [], "A", "B", "A", "ban", None)
        current = [action(team, kind, None) for team, kind in TOURNAMENT_ORDER[:6]]
        with self.assertRaises(ValueError):
            rules.validate_action(["thresh"], set(), current, "A", "B", "A", "pick", None)
        rules.validate_action(["thresh"], set(), current, "A", "B", "A", "pick", "thresh")

    def test_a_completed_draft_rejects_another_action(self):
        with self.assertRaises(ValueError):
            rules.validate_action(["thresh"], set(), recorded_game("g1"), "A", "B", "A", "ban", "thresh")

    def test_picks_from_both_teams_carry_across_completed_games(self):
        completed = [recorded_game("g1"), recorded_game("g2")]
        picks = {f"{game}-{index}" for game in ("g1", "g2") for index in (6, 7, 8, 9, 10, 11, 16, 17, 18, 19)}

        self.assertEqual(rules.series_locks(completed), picks)
        self.assertEqual(rules.series_locks(completed, fearless=False), set())

    def test_rules_replay_multiple_games_deterministically(self):
        first_game = recorded_game("g1")
        second_game = [
            action("B" if entry["team"] == "A" else "A", entry["kind"], entry["champion"])
            for entry in recorded_game("g2")
        ]
        second_game[6]["champion"] = "g1-0"
        games = [first_game, second_game]
        pool = [entry["champion"] for game in games for entry in game if entry["champion"] != "g2-6"]
        expected = {"g1-0", "g2-7", "g2-8", "g2-9", "g2-10", "g2-11", "g2-16", "g2-17", "g2-18", "g2-19"}
        expected.update({"g1-6", "g1-7", "g1-8", "g1-9", "g1-10", "g1-11", "g1-16", "g1-17", "g1-18", "g1-19"})

        for _ in range(2):
            completed = []
            for game, (first, second) in zip(games, [("A", "B"), ("B", "A")]):
                locks = rules.series_locks(completed)
                for index, entry in enumerate(game):
                    rules.validate_action(pool, locks, game[:index], first, second, **entry)
                completed.append(game)
            self.assertEqual(rules.series_locks(completed), expected)


class PinStatusTests(unittest.TestCase):
    def test_ban_protects_the_next_game_only(self):
        current = [action("A", "ban", "thresh")]
        self.assertEqual(rules.pin_status(["thresh"], [current], 2, "thresh"), "protected")
        self.assertEqual(rules.pin_status(["thresh"], [current], 3, "thresh"), "still_possible")

    def test_future_pin_is_still_possible_until_before_target(self):
        first_game = recorded_game("g1")
        self.assertEqual(rules.pin_status(["thresh"], [[]], 2, "thresh"), "still_possible")
        self.assertEqual(rules.pin_status(["thresh"], [first_game], 2, "thresh"), "protected")
        self.assertEqual(rules.pin_status(["thresh"], [first_game], 3, "thresh"), "still_possible")

    def test_intervening_game_can_consume_a_pin(self):
        first_game = recorded_game("g1")
        second_game = recorded_game("g2")[:7]
        second_game[6]["champion"] = "thresh"
        self.assertEqual(rules.pin_status(["thresh"], [first_game, second_game], 3, "thresh"), "conflict")

    def test_standard_mode_protects_future_pin_even_after_prior_pick(self):
        current = recorded_game("g1")[:7]
        self.assertEqual(rules.pin_status(["g1-6"], [current], 2, "g1-6", fearless=False), "protected")

    def test_disabled_champion_conflicts(self):
        self.assertEqual(rules.pin_status([], [], 1, "thresh"), "conflict")

    def test_target_pick_must_satisfy_its_team_and_cannot_be_banned(self):
        current = recorded_game("g1")[:7]
        self.assertEqual(rules.pin_status(["g1-6"], [current], 1, "g1-6", team="A"), "satisfied")
        self.assertEqual(rules.pin_status(["g1-6"], [current], 1, "g1-6"), "satisfied")
        self.assertEqual(rules.pin_status(["g1-6"], [current], 1, "g1-6", team="B"), "conflict")
        self.assertEqual(rules.pin_status(["g1-0"], [current], 1, "g1-0", team="A"), "conflict")

    def test_pin_conflicts_after_team_passes_its_last_pick(self):
        target = recorded_game("g2")[:-1]
        self.assertEqual(rules.pin_status(["thresh"], [target], 1, "thresh", team="A"), "conflict")
        self.assertEqual(rules.pin_status(["thresh"], [target], 1, "thresh", team="B"), "protected")

    def test_end_of_target_game_does_not_erase_satisfied_pin(self):
        games = [recorded_game("g1"), recorded_game("g2")]
        self.assertEqual(rules.pin_status(["g1-6"], games, 1, "g1-6", team="A"), "satisfied")
        self.assertEqual(rules.pin_status(["thresh"], games, 1, "thresh"), "conflict")

    def test_target_game_numbers_start_at_one(self):
        with self.assertRaises(ValueError):
            rules.pin_status(["thresh"], [], 0, "thresh")


if __name__ == "__main__":
    unittest.main()
