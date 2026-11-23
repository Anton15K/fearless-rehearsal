import csv
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fearless.data import import_grid, load_role_evidence, turn_rows


DRAFT_ORDER = (
    "A ban", "B ban", "A ban", "B ban", "A ban", "B ban",
    "A pick", "B pick", "B pick", "A pick", "A pick", "B pick",
    "B ban", "A ban", "B ban", "A ban", "B pick", "A pick", "A pick", "B pick",
)


def actions(prefix, first="A"):
    for slot, item in enumerate(DRAFT_ORDER, 1):
        team, kind = item.split()
        yield {
            "sequenceNumber": str(slot),
            "type": kind,
            "drafter": {"id": team if first == "A" else ("B" if team == "A" else "A")},
            "draftable": {"id": f"{prefix}{slot}", "name": f"{prefix} {slot}"},
        }


def game(number, draft, game_id=None):
    return {
        "id": game_id or f"g{number}", "sequenceNumber": number, "finished": True,
        "startedAt": f"2026-04-04T1{number}:30:00Z", "patch": "16.7",
        "teams": [{"id": "A", "name": "A Team", "side": "blue"},
                  {"id": "B", "name": "B Team", "side": "red"}],
        "draftActions": list(draft),
    }


def envelope(sequence, *events, hour=10, minute=0):
    return {
        "seriesId": "s1", "id": f"env-{sequence}", "sequenceNumber": sequence,
        "occurredAt": f"2026-04-04T{hour:02d}:{minute:02d}:00Z", "events": events,
    }


def event(event_type, game_id, *, state=None, **payload):
    value = {"type": event_type, "target": {"type": "game", "id": game_id}, **payload}
    if state is not None:
        value["seriesState"] = {"games": state}
    return value


def draft_event(raw_action, game_id, event_id, undo=False):
    verb = {"ban": "banned", "pick": "picked"}[raw_action["type"]]
    return event(
        f"team-{'!' if undo else ''}{verb}-character", game_id, id=event_id,
        gameId=game_id, actor={"type": "team", **raw_action["drafter"]},
        target={"type": "character", **raw_action["draftable"]},
    )


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class GridImportTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        (self.root / "lol").mkdir()
        (self.root / "meta").mkdir()

    def write_feed(self, envelopes, *, compressed=False, event_name="LEC - Spring 2026"):
        suffix = ".zst" if compressed else ""
        filename = f"lol/events_s1_grid.jsonl{suffix}"
        write_csv(self.root / "lol/series.csv", [{
            "series_id": "s1", "event": event_name, "stage": "Regular Season",
            "scheduled_start": "2026-04-04T10:00:00Z", "file": filename,
        }])
        content = "\n".join(json.dumps(row) for row in envelopes) + "\n"
        path = self.root / filename
        if compressed:
            subprocess.run(["zstd", "-q", "-o", str(path)], input=content.encode(), check=True)
        else:
            path.write_text(content, encoding="utf-8")

    @unittest.skipUnless(shutil.which("zstd"), "zstd CLI needed to create fixture")
    def test_streams_zstd_and_uses_draft_order_independent_of_map_side(self):
        final = game(1, reversed(list(actions("One", first="B"))))
        self.write_feed([envelope(100, event("grid-ended-feed", "", state=[final]))], compressed=True)
        games, audit = import_grid(self.root)

        self.assertEqual(len(games), 1)
        record = games[0]
        self.assertTrue(record["eligible"])
        self.assertEqual((record["series_id"], record["game_number"], record["date"]), ("s1", 1, "2026-04-04"))
        self.assertEqual((record["first"], record["second"], record["sides"]["B"]), ("B", "A", "red"))
        self.assertEqual(record["actions"][6], {"team": "B", "kind": "pick", "champion": "One 7"})
        self.assertEqual((record["patch"], record["region"], record["ruleset"]), ("16.7", "EMEA", "fearless"))
        self.assertEqual(audit["eligible_games"], 1)

    def test_finished_game_winner_and_best_of_use_recorded_evidence(self):
        final = game(1, actions("One"))
        final["teams"][1]["won"] = True
        ended = event("grid-ended-feed", "", state=[final])
        ended["seriesState"]["format"] = "best-of-3"
        self.write_feed([envelope(1, ended)])
        records, _ = import_grid(self.root)
        self.assertEqual((records[0]["winner"], records[0]["best_of"]), ("B", 3))

        write_csv(self.root / "meta/games.csv", [{
            "title": "lol", "series_id": "s1", "game_id": "g1", "winner_team_id": "A",
        }])
        records, _ = import_grid(self.root)
        self.assertIsNone(records[0]["winner"])
        self.assertIn("conflicting_game_winner", records[0]["issues"])

    def test_events_deduplicate_undo_and_discard_rolled_back_game(self):
        first, second = list(actions("One")), list(actions("Two", first="B"))
        temporary = {**second[0], "draftable": {"id": "temp", "name": "Temporary"}}
        feeds = [envelope(1, event("series-started-game", "old", state=[game(1, first, "old")]))]
        feeds.append(envelope(2, event("series-!started-game", "old", state=[])))
        feeds.append(envelope(3, draft_event(temporary, "g1", "temp")))
        feeds.append(envelope(4, draft_event(temporary, "g1", "undo", undo=True)))
        feeds.append(envelope(5, *(draft_event(action, "g1", f"d-{i}") for i, action in enumerate(second))))
        feeds.append(feeds[-1])
        feeds.append(envelope(6, draft_event(second[0], "g1", "d-0")))
        played = game(1, [], "g1")
        played.pop("draftActions")
        feeds.append(envelope(7, event("series-ended-game", "g1", state=[played]), hour=12))
        self.write_feed(feeds)

        games, audit = import_grid(self.root)

        self.assertEqual(len(games), 1)
        self.assertTrue(games[0]["eligible"])
        self.assertEqual(games[0]["draft_source"], "events")
        self.assertEqual(games[0]["actions"][0]["champion"], "Two 1")
        self.assertEqual(games[0]["drafted_at"], "2026-04-04T10:00:00Z")
        self.assertEqual(games[0]["finished_at"], "2026-04-04T12:00:00Z")
        self.assertEqual(audit["rolled_back_games"], 1)
        self.assertEqual((audit["duplicate_envelopes"], audit["duplicate_draft_events"]), (1, 1))

    def test_missing_history_and_locked_champion_are_ineligible(self):
        one = list(actions("One"))
        two = list(actions("Two"))
        two[6]["draftable"] = one[6]["draftable"]
        self.write_feed([envelope(1, event("grid-ended-feed", "", state=[game(1, one), game(2, two), game(4, actions("Four"))]))])

        games, audit = import_grid(self.root)

        self.assertEqual([row["eligible"] for row in games], [True, False, False])
        self.assertIn("illegal_action", games[1]["issues"])
        self.assertIn("missing_prior_draft", games[2]["issues"])
        self.assertIn("One 7", games[1]["locked"])
        self.assertNotIn("One 1", games[1]["locked"])
        self.assertEqual(audit["missing_game_numbers"], 1)

    def test_finished_roster_corroborates_prior_picks_when_one_ban_is_missing(self):
        first = list(actions("One"))
        state = game(1, first[:5] + first[6:])
        for team in state["teams"]:
            team["players"] = [
                {"id": f"{team['id']}-{i}", "character": action["draftable"]}
                for i, action in enumerate(first)
                if action["type"] == "pick" and action["drafter"]["id"] == team["id"]
            ]
        self.write_feed([envelope(1, event("grid-ended-feed", "", state=[state, game(2, actions("Two"))]))],
                        event_name="LCS - Promotion 2026")

        games, _ = import_grid(self.root)

        self.assertFalse(games[0]["eligible"])
        self.assertTrue(games[1]["eligible"])
        self.assertEqual(len(games[1]["locked"]), 10)
        self.assertIn("One 7", games[1]["locked"])
        self.assertNotIn("One 1", games[1]["locked"])

    def test_unsourced_competition_requires_override_for_later_games(self):
        self.write_feed([envelope(1, event("grid-ended-feed", "", state=[game(1, actions("One")), game(2, actions("Two"))]))],
                        event_name="Unofficial Series 2026")

        games, _ = import_grid(self.root)
        self.assertTrue(games[0]["eligible"])
        self.assertEqual(games[1]["issues"], ["unknown_series_rules"])
        games, _ = import_grid(self.root, rule_overrides={"s1": ("standard", "organizer rulebook")})
        self.assertTrue(games[1]["eligible"])
        self.assertEqual(games[1]["rules_source"], "organizer rulebook")
        self.assertEqual(games[1]["locked"], [])

    def test_explicit_empty_ban_keeps_its_slot(self):
        draft = list(actions("One"))
        draft[0]["draftable"] = None
        self.write_feed([envelope(1, event("grid-ended-feed", "", state=[game(1, draft)]))])

        games, _ = import_grid(self.root)

        self.assertTrue(games[0]["eligible"])
        self.assertEqual(games[0]["actions"][0], {"team": "A", "kind": "ban", "champion": None})
        self.assertEqual(games[0]["actions"][6]["champion"], "One 7")

    def test_unverified_and_future_roles_are_unknown_and_absent_from_turn_features(self):
        first = list(actions("One"))
        self.write_feed([envelope(1, event("grid-ended-feed", "", state=[game(1, first)]))])
        write_csv(self.root / "meta/rosters.csv", [
            {"title": "lol", "series_id": "s1", "game_id": "g1", "team_id": "A", "player_id": "p1",
             "player_name": "First", "character_id": "One7", "character_name": "One 7", "role": "support"},
            {"title": "lol", "series_id": "s1", "game_id": "g1", "team_id": "A", "player_id": "p2",
             "player_name": "Second", "character_id": "One10", "character_name": "One 10", "role": "mid"},
            {"title": "val", "series_id": "s1", "game_id": "g1", "team_id": "A", "player_id": "p3",
             "player_name": "Wrong title", "character_id": "other", "character_name": "Other", "role": "top"},
        ])
        roles = self.root / "roles.csv"
        write_csv(roles, [
            {"team_id": "A", "player_id": "p1", "valid_from": "2026-01-01", "valid_to": "2026-05-01",
             "known_from": "2026-03-25", "role": "ADC", "source": "dated team roster"},
            {"team_id": "A", "player_id": "p2", "valid_from": "2026-01-01", "valid_to": "2026-05-01",
             "known_from": "2026-04-05", "role": "mid", "source": "later announcement"},
        ])

        games, audit = import_grid(self.root, roles=load_role_evidence(roles))
        roster = games[0]["rosters"]["A"]
        self.assertEqual([(row["player"], row["role"]) for row in roster], [("p1", "bot"), ("p2", None)])
        self.assertEqual((audit["roster_rows"], audit["verified_role_rows"]), (2, 1))

        rows = list(turn_rows(games))
        self.assertEqual(rows[0]["context"]["actions"], [])
        self.assertEqual(len(rows[6]["context"]["actions"]), 6)
        self.assertEqual(rows[6]["label"], "One 7")
        self.assertNotIn("rosters", rows[6]["context"])
        self.assertNotIn("One 7", json.dumps(rows[6]["context"]))


if __name__ == "__main__":
    unittest.main()
