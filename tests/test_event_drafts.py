from collections import Counter

import pytest

from fearless.rules import schedule
from scripts import event_drafts


STAGE = "LCP/2026 Season/Split 3"
CHAMPIONS = [f"Champion {index}" for index in range(1, 51)]


@pytest.fixture
def tables():
    result = {key: [] for key in ("score", "duration", "schedule", "draft", "series")}
    for number, (first, second) in enumerate((("Cloud9", "Ground Zero Gaming"),
                                               ("Ground Zero Gaming", "Cloud9")), 1):
        game = {"GameId": f"game-{number}", "OverviewPage": STAGE}
        result["score"].append({**game, "MatchId": "series", "Team1": second, "Team2": first,
                                "Patch": "26.14", "number": str(number),
                                "start": f"2026-07-25 {number + 8:02}:19:00"})
        result["duration"].append({**game, "duration": "31:07"})
        result["schedule"].append({**game, "Blue": second, "Red": first, "FirstPick": first})
        draft = {**game, "Team1": first, "Team2": second, "IsComplete": "1", "IsFilled": "1"}
        slots = Counter()
        for (team, kind), champion in zip(schedule(first, second), CHAMPIONS[(number - 1) * 20:number * 20]):
            slots[team, kind] += 1
            side = 1 if team == first else 2
            draft[f"Team{side}{kind.title()}{slots[team, kind]}"] = champion
        result["draft"].append(draft)
    result["series"].append({"OverviewPage": STAGE, "MatchId": "series", "BestOf": "3",
                              "Team1Score": "2", "Team2Score": "0", "IsNullified": "0",
                              "DisabledChampions": "Champion 50"})
    return result


def normalize(tables, archive=(), previous=()):
    return event_drafts.normalize(tables, archive, previous, {"16.14": CHAMPIONS})


def test_event_uses_completed_earlier_team_alias_and_stable_source_identity(tables):
    archive = [
        {"date": "2026-07-24", "eligible": True, "finished": True,
         "team_names": {"c9": "Cloud9 Kia"}},
        {"date": "2026-07-25", "eligible": True, "finished": True,
         "team_names": {"later-c9": "Cloud9 Kia"}},
        {"date": "2026-07-24", "finished_at": "2026-07-25T00:05:00Z", "eligible": True, "finished": True,
         "team_names": {"also-later-c9": "Cloud9 Kia"}},
    ]
    (first, second), audit = normalize(tables, archive)

    external = "leaguepedia:Ground Zero Gaming"
    assert first["team_names"] == {"c9": "Cloud9", external: "Ground Zero Gaming"}
    assert second["team_names"] == first["team_names"]
    assert (first["first"], first["second"], second["first"], second["second"]) == ("c9", external, external, "c9")
    assert first["event"] == "LCP Split 3 2026"
    assert first["region"] == "APAC"
    assert first["patch"] == "16.14"
    assert first["disabled"] == ["Champion 50"]
    assert first["finished_at"] == "2026-07-25T09:50:07+00:00"
    assert first["fearless"] and first["eligible"] and first["finished"]
    assert second["series_games"] == 2
    assert len(second["locked"]) == 10
    assert audit["stages"][STAGE]["games"] == 2
    assert audit["stages"][STAGE]["archive_team_ids"] == 1
    assert audit["stages"][STAGE]["external_team_ids"] == 1


def test_source_scope_rejects_unregistered_stages(tables):
    tables["score"][0]["OverviewPage"] = "2026 Season World Championship/Main Event"
    with pytest.raises(ValueError, match="preregistered"):
        normalize(tables)


def test_missing_patch_excludes_the_entire_series(tables):
    tables["score"][1]["Patch"] = ""
    rows, audit = normalize(tables)
    assert rows == []
    assert audit["excluded"] == {"series": "missing competitive patch for game-2"}


def test_ambiguous_earlier_archive_identity_excludes_the_entire_series(tables):
    archive = [{"date": "2026-07-24", "eligible": True, "finished": True,
                "team_names": {"first-c9": "Cloud9 Kia", "second-c9": "Cloud9"}}]
    rows, audit = normalize(tables, archive)
    assert rows == []
    assert "ambiguous team identity" in audit["excluded"]["series"]


@pytest.mark.parametrize("reference", ["archive", "previous"])
def test_overlap_checks_draft_content_even_when_ids_change(tables, reference):
    (_, second), _ = normalize(tables)
    other = {**second, "game_id": "other-game", "series_id": "other-series", "eligible": False,
             "actions": list(reversed(second["actions"]))}
    rows, audit = normalize(tables, **{reference: [other]})
    assert rows == []
    assert "other-game" in audit["excluded"]["series"]
    assert audit["overlap"][reference]["matched_source_games"] == 1


def test_duplicate_source_keys_stop_the_import(tables):
    tables["draft"].append(tables["draft"][0])
    with pytest.raises(ValueError, match="Duplicate GameId"):
        normalize(tables)


@pytest.fixture
def transfer_tables(tables):
    for records in tables.values():
        for row in records:
            row["OverviewPage"] = "CBLOL/2026 Season/Split 2 Playoffs"
    return tables


def test_transfer_uses_recorded_first_pick_for_cblol(transfer_tables):
    rows, audit = event_drafts.normalize(transfer_tables, [], [], {"16.14": CHAMPIONS},
                                         cohort=event_drafts.TRANSFER)
    assert len(rows) == 2
    assert rows[0]["region"] == "LATAM"
    stage = audit["stages"]["CBLOL/2026 Season/Split 2 Playoffs"]
    assert stage["first_pick"] == {"source_recorded": 2, "accepted_recorded": 2}


def test_transfer_excludes_emea_under_preregistered_first_pick_coverage_fallback(transfer_tables):
    stage = "EMEA Masters/2026 Season/Summer Main Event"
    for records in transfer_tables.values():
        for row in records:
            row["OverviewPage"] = stage
    rows, audit = event_drafts.normalize(transfer_tables, [], [], {"16.14": CHAMPIONS},
                                         cohort=event_drafts.TRANSFER)
    assert rows == []
    assert audit["stages"][stage]["source_games"] == 2
    assert "first-pick" in audit["excluded"]["series"]


def test_transfer_excludes_series_when_required_recorded_first_pick_is_missing(transfer_tables):
    transfer_tables["schedule"][1]["FirstPick"] = ""
    rows, audit = event_drafts.normalize(transfer_tables, [], [], {"16.14": CHAMPIONS},
                                         cohort=event_drafts.TRANSFER)
    assert rows == []
    assert "first selection" in audit["excluded"]["series"]


def test_transfer_checks_each_past_external_holdout(transfer_tables):
    rows, _ = event_drafts.normalize(transfer_tables, [], [], {"16.14": CHAMPIONS},
                                     cohort=event_drafts.TRANSFER)
    duplicate = {**rows[0], "game_id": "known-previous-draft", "series_id": "previous-series"}
    rows, audit = event_drafts.normalize(transfer_tables, [], [], {"16.14": CHAMPIONS},
                                         cohort=event_drafts.TRANSFER, additional_previous=([duplicate],))
    assert rows == []
    assert "known-previous-draft" in audit["excluded"]["series"]
    assert audit["overlap"]["previous_2"]["matched_source_games"] == 1


def test_role_pair_holdout_resolves_nongshim_to_its_earlier_archive_team(tables):
    stage = "LCK/2026 Season/Season Playoffs"
    for records in tables.values():
        for row in records:
            row["OverviewPage"] = stage
            for field in ("Team1", "Team2", "Blue", "Red", "FirstPick"):
                if row.get(field) == "Ground Zero Gaming":
                    row[field] = "Nongshim RedForce"
    for row in tables["score"]:
        row["start"] = row["start"].replace("2026-07-25", "2026-08-29")
    archive = [{"date": "2026-08-28", "eligible": True, "finished": True,
                "team_names": {"nongshim": "NONGSHIM RED FORCE"}}]
    (first, second), report = event_drafts.normalize(
        tables, archive, [], {"16.14": CHAMPIONS}, cohort=event_drafts.ROLE_PAIRS,
    )

    assert first["second"] == second["first"] == "nongshim"
    assert first["team_names"]["nongshim"] == "Nongshim RedForce"
    assert first["region"] == "KR"
    assert report["stages"][stage]["first_pick"]["accepted_recorded"] == 2
