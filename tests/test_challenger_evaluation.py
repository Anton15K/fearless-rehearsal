"""Regression coverage for the out-of-archive replay and paired promotion gates."""

from collections import Counter
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import numpy as np
import pytest

from fearless.model_features import ChampionCatalog
from fearless.rules import schedule
from scripts import compare_challengers, worlds_holdout


CHAMPIONS = [f"Champion {i}" for i in range(1, 61)]
PATCHES = {"16.20": CHAMPIONS}
TEAM_IDS = {"cloud9 kia": "c9", "dignitas": "dig", "flyquest": "fly"}


def _draft_fields(game_id, first, second, champions):
    fields = {"GameId": game_id, "Team1": first, "Team2": second, "IsFilled": "1", "IsComplete": "1"}
    counts = Counter()
    for (team, kind), champion in zip(schedule(first, second), champions, strict=True):
        counts[team, kind] += 1
        team_index = 1 if team == first else 2
        fields[f"Team{team_index}{kind.title()}{counts[team, kind]}"] = champion
    return fields


@pytest.fixture
def series():
    games, drafts, selections = [], {}, {}
    for number, (first, second) in enumerate((("Cloud9", "Dignitas"), ("Dignitas", "Cloud9")), 1):
        game = f"game-{number}"
        first_index = (number - 1) * 20
        names = CHAMPIONS[first_index:first_index + 20]
        if number == 2:
            # The first game's opening ban may be used again; a passing ban consumes its slot.
            names[0], names[1] = CHAMPIONS[0], "No ban"
        games.append({
            "GameId": game, "MatchId": "series", "number": str(number),
            "Team1": "Dignitas", "Team2": "Cloud9", "start": f"2026-10-25 20:{number}0:00",
            "Patch": "26.20", "duration": "35:23",
        })
        drafts[game] = _draft_fields(game, first, second, names)
        selections[game] = {"Blue": second, "Red": first, "FirstPick": first}
    metadata = {"Team1Score": "2", "Team2Score": "0", "BestOf": "3", "IsNullified": "0",
                "DisabledChampions": ""}
    return SimpleNamespace(metadata=metadata, games=games, drafts=drafts, selections=selections)


def _parse(series):
    return worlds_holdout.parse_series(
        series.metadata, series.games, series.drafts, series.selections, PATCHES, TEAM_IDS,
    )


def test_importer_uses_first_pick_for_alternation_and_preserves_only_pick_locks(series):
    first, second = _parse(series)
    assert first["patch"] == second["patch"] == "16.20"
    assert first["series_games"] == second["series_games"] == 2
    assert (first["first"], first["second"]) == ("c9", "dig")
    assert (second["first"], second["second"]) == ("dig", "c9")
    assert [(action["team"], action["kind"]) for action in first["actions"][:7]] == [
        ("c9", "ban"), ("dig", "ban"), ("c9", "ban"), ("dig", "ban"),
        ("c9", "ban"), ("dig", "ban"), ("c9", "pick"),
    ]
    assert len(second["locked"]) == 10
    assert set(second["locked"]) == {action["champion"] for action in first["actions"] if action["kind"] == "pick"}
    assert second["actions"][0]["champion"] == first["actions"][0]["champion"]
    assert second["actions"][1]["champion"] is None


def test_importer_rejects_first_pick_disagreement_and_prior_series_pick(series):
    original = series.selections["game-1"]["FirstPick"]
    series.selections["game-1"]["FirstPick"] = "Dignitas"
    with pytest.raises(ValueError, match="first selection"):
        _parse(series)

    series.selections["game-1"]["FirstPick"] = original
    series.drafts["game-2"]["Team1Pick1"] = series.drafts["game-1"]["Team1Pick1"]
    with pytest.raises(ValueError, match="previous game"):
        _parse(series)


def test_importer_rejects_a_series_with_different_participants_in_later_game(series):
    series.games[1]["Team1"] = "FlyQuest"
    series.drafts["game-2"]["Team1"] = "FlyQuest"
    series.selections["game-2"]["Red"] = "FlyQuest"
    series.selections["game-2"]["FirstPick"] = "FlyQuest"
    with pytest.raises(ValueError, match="participants"):
        _parse(series)


def test_importer_rejects_an_invalid_utc_date(series):
    series.games[0]["start"] = "2026-15-01 20:00:00"
    with pytest.raises(ValueError):
        _parse(series)


def test_importer_records_utc_completion_even_when_game_crosses_midnight(series):
    series.games[-1]["start"] = "2026-10-25 23:50:00"
    _, late_game = _parse(series)
    assert late_game["date"] == "2026-10-25"
    assert datetime.fromisoformat(late_game["finished_at"]) == datetime(2026, 10, 26, 0, 25, 23, tzinfo=timezone.utc)


def _read_holdout(rows, tmp_path):
    holdout = tmp_path / "holdout.jsonl"
    holdout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")
    frozen = SimpleNamespace(
        catalog=ChampionCatalog.from_json(PATCHES),
        games=[{"date": "2026-10-13"}],
    )
    return compare_challengers.whole_games(holdout, frozen)


def test_refresh_partition_rejects_a_series_that_finishes_after_cutoff(series, tmp_path):
    rows = _parse(series)
    rows[-1]["finished_at"] = "2026-10-26T00:20:00Z"
    with pytest.raises(ValueError, match="cutoff"):
        _read_holdout(rows, tmp_path)


def test_importer_excludes_a_series_with_null_game_duration(series):
    series.games[-1]["duration"] = None
    with pytest.raises(ValueError):
        _parse(series)


@pytest.mark.parametrize("removed", [0, 1])
def test_holdout_refuses_a_truncated_series_even_if_remaining_games_are_valid(series, tmp_path, removed):
    rows = _parse(series)
    rows.pop(removed)
    with pytest.raises(ValueError, match="series"):
        _read_holdout(rows, tmp_path)


def test_holdout_refuses_a_game_marked_ineligible(series, tmp_path):
    rows = _parse(series)
    rows[-1]["eligible"] = False
    with pytest.raises(ValueError, match="complete|eligible|validated"):
        _read_holdout(rows, tmp_path)


def test_pair_gate_fallback_uses_the_frozen_picker(monkeypatch):
    prefix = [
        {"team": team, "kind": kind, "champion": champion}
        for (team, kind), champion in zip(schedule("c9", "dig")[:7], CHAMPIONS[:7], strict=True)
    ]

    def frozen_forecast(**context):
        assert context["current"] == prefix
        return {"source": "frozen"}

    def calibrated_forecast(**context):
        pytest.fail("The calibration refresh must not influence the pair gate")

    challenger = SimpleNamespace(
        frozen=SimpleNamespace(forecast=frozen_forecast),
        rank_pairs=lambda **context: [],
        forecast=calibrated_forecast,
    )

    def lookahead(**kwargs):
        assert kwargs["forecast"](prefix)["source"] == "frozen"
        return {"pairs": [{"champions": ["Ahri", "Poppy"]}]}

    monkeypatch.setattr(compare_challengers, "lookahead", lookahead)
    context = {"pool": ["Ahri", "Poppy", *CHAMPIONS[:7]], "locked": [], "first": "c9", "second": "dig"}
    assert compare_challengers.pick_pairs(challenger, context, prefix) == [("Ahri", "Poppy")]


def test_paired_confidence_interval_resamples_whole_series_and_weights_turns():
    paired = {
        "large": [1] * 17, "medium": [-1] * 6, "small": [0, 1],
        "fourth": [0, 0, 0], "fifth": [-1], "sixth": [0, 0, 1, 1],
    }
    rows = [(sid, (int(delta < 0),), (int(delta > 0),))
            for sid, turns in paired.items() for delta in turns]
    rng = np.random.default_rng(10429)
    weights_and_deltas = np.array([(len(paired[sid]), sum(paired[sid])) for sid in sorted(paired)])
    selected = weights_and_deltas[rng.integers(0, len(paired), size=(5000, len(paired)))].sum(axis=1)
    expected = np.quantile(selected[:, 1] / selected[:, 0], (0.025, 0.975)).tolist()
    ranking = compare_challengers.compare(rows, is_pair=True)
    reverse = compare_challengers.compare(rows[::-1], is_pair=True)

    assert ranking["turns"] == sum(map(len, paired.values()))
    assert ranking["series"] == len(paired)
    assert ranking["hit3_delta_ci95"] == pytest.approx(expected)
    assert ranking == reverse


def test_full_distribution_rejects_shortlist_even_if_it_sums_to_one():
    prefix = [{"team": "c9", "kind": "ban", "champion": "Vi"}]
    context = {"pool": ["Ahri", "Poppy", "Sejuani", "Vi"], "locked": ["Sejuani"]}

    class Forecast:
        def forecast(self, *, current, limit, **_):
            assert current == prefix
            assert limit == 2
            return {"choices": [{"champion": "Poppy", "probability": 1.0}]}

    with pytest.raises(ValueError, match="full legal distribution"):
        compare_challengers.distribution(Forecast(), context, prefix)


def test_quality_reports_use_top_choice_correctness_and_full_target_log_loss():
    outcomes = [
        ("s1", (1, 0.8, 0.62, 1), (1, 0.9, 0.58, 1)),
        ("s1", (1, 1.6, 0.65, 0), (1, 1.2, 0.58, 0)),
        ("s2", (0, 2.0, 0.28, 0), (0, 1.9, 0.18, 0)),
    ]
    metrics = compare_challengers.compare(outcomes)
    assert metrics["incumbent"] == pytest.approx({
        "hit3": 2 / 3, "nll": (0.8 + 1.6 + 2.0) / 3, "ece": (0.27 + 0.28) / 3,
    })
    assert metrics["candidate"] == pytest.approx({
        "hit3": 2 / 3, "nll": (0.9 + 1.2 + 1.9) / 3, "ece": (0.16 + 0.18) / 3,
    })
    assert metrics["hit3_delta_ci95"] == [0, 0]


def test_opening_ban_invariance_includes_observed_passes(monkeypatch):
    frozen = object()
    candidate = SimpleNamespace(frozen=frozen)
    monkeypatch.setattr(compare_challengers.DraftChallengers, "load", lambda *args: candidate)
    games = [{"series_id": "s", "actions": [{"team": "c9", "kind": "ban", "champion": None}]}]
    monkeypatch.setattr(compare_challengers, "whole_games", lambda *args: games)
    monkeypatch.setattr(compare_challengers, "dated_games", lambda *_: iter(games))
    monkeypatch.setattr(compare_challengers, "json_file", lambda _: PATCHES)
    monkeypatch.setattr(compare_challengers, "context", lambda *_: {})

    def distribution(forecaster, *_):
        probabilities = {"Ahri": 0.9, "Vi": 0.1} if forecaster is frozen else {"Vi": 0.9, "Ahri": 0.1}
        return probabilities, {}

    monkeypatch.setattr(compare_challengers, "distribution", distribution)
    args = SimpleNamespace(bundle=None, policy=None, games=None, catalog=None)
    with pytest.raises(ValueError, match="Opening ban"):
        compare_challengers.score(args)
