import gzip
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from fearless.adaptive_pairs import AdaptivePairModel, FEATURES
from fearless.api import create_app
from fearless.calibration import bundle_fingerprint
from fearless.challengers import DraftChallengers, incumbent_signature
from fearless.event_forecaster import EventForecaster, digest
from fearless.model import DraftForecaster
from fearless.model_features import FEATURES as DRAFT_FEATURES, ChampionCatalog
from fearless.pair_model import PairChallenger
from fearless.patch_picks import PatchPickModel
from fearless.rules import schedule
from scripts.evaluate_adaptation import package, top_pairs


CHAMPIONS = tuple(f"Champion {index:02d}" for index in range(45))
PATCH = "16.20"


def game(series_id, day, player_id, *, names=CHAMPIONS):
    actions = [
        {"team": team, "kind": kind, "champion": champion}
        for (team, kind), champion in zip(schedule("home", "away"), names)
    ]
    return {
        "series_id": series_id, "game_id": f"{series_id}/1", "series_games": 1, "game_number": 1,
        "date": day, "patch": PATCH, "region": "CN", "event": "Regional split",
        "drafted_at": f"{day}T15:00:00Z", "finished_at": f"{day}T16:00:00Z",
        "eligible": True, "finished": True, "fearless": True, "locked": [], "disabled": [],
        "first": "home", "second": "away", "team_names": {"home": "Home", "away": "Away"},
        "actions": actions, "rosters": {"away": [
            {"player": player_id, "player_name": player_id, "champion": actions[7]["champion"]},
        ]},
    }


def forecaster(games=(), **kwargs):
    return DraftChallengers(DraftForecaster(ChampionCatalog(patches={PATCH: CHAMPIONS}, releases={}), games),
                            **kwargs)


def test_empty_adaptive_pair_shortlist_serves_what_evaluation_counts():
    row = game("reply", "2026-10-20", "Starter")
    context = {key: row[key] for key in ("first", "second", "date", "patch", "region", "game_number", "locked")}
    context["pool"] = CHAMPIONS
    parent = forecaster(pair=PairChallenger({
        "version": 1, "beam_width": 8, "train_through": "2025-12-31", "weights": [1, 0, 0, 0],
    }))
    pair = AdaptivePairModel({
        "version": 1, "beam_width": 8, "train_through": row["date"],
        "features": list(FEATURES), "weights": [0] * len(FEATURES),
    })
    model = EventForecaster(parent, (), pair=pair)
    current = row["actions"][:7]
    incumbent = parent.rank_pairs(current=current, **context)
    original = [tuple(sorted(row["champions"])) for row in incumbent]

    assert len(original) == 3
    assert top_pairs(model, context, current, row["event"], row["series_id"], original) == original
    assert model.rank_pairs(current=current, event=row["event"], series_id=row["series_id"], **context) == incumbent


def test_event_board_does_not_override_the_parents_inferred_lineup(tmp_path):
    first = game("frozen", "2026-10-15", "Frozen Starter")
    later = game("released", "2026-10-19", "Released Starter")
    parent = forecaster([first])
    model = EventForecaster(parent, [later], recipe="fixed")
    champions = tmp_path / "champions.json"
    champions.write_text(json.dumps({PATCH: CHAMPIONS}))
    app = create_app(database=tmp_path / "plans.sqlite3", catalog=champions, forecaster=model)
    matchup = {
        "match_id": "prospective", "teams": ["Home", "Away"], "coach_team": "Away", "first": "Home",
        "date": "2026-10-20", "patch": PATCH, "format": "Bo3", "event": later["event"],
    }

    with TestClient(app) as client:
        same_day = client.post("/api/plans", json={**matchup, "date": later["date"]})
        assert same_day.status_code == 201, same_day.text
        assert not same_day.json()["rosters"]

        response = client.post("/api/plans", json=matchup)
        assert response.status_code == 201, response.text
        plan = response.json()
        assert plan["rosters"]["Away"] == ["Released Starter"]
        path = f"/api/plans/{plan['id']}/nodes/{plan['root_id']}"
        forecast = client.get(path).json()["forecast"]
        assert forecast["roster_evidence"]["Away"] == {
            "source": "previous_observed_lineup", "as_of": "2026-10-15", "players": 1,
        }
        override = client.post("/api/plans", json={**matchup, "rosters": {"Away": ["Released Starter"]}})
        assert override.status_code == 201, override.text
        override_plan = override.json()
        override_path = f"/api/plans/{override_plan['id']}/nodes/{override_plan['root_id']}"
        assert client.get(override_path).json()["forecast"]["roster_evidence"]["Away"]["source"] == "supplied"


def test_rejects_frozen_game_repeated_under_a_different_series():
    frozen = game("original", "2026-10-15", "Starter")
    duplicate = {**frozen, "series_id": "imported-copy"}

    with pytest.raises(ValueError, match="overlap"):
        EventForecaster(forecaster([frozen]), [duplicate], recipe="refresh")


def write_archive(tmp_path, name, games):
    path, audit = tmp_path / f"{name}.jsonl", tmp_path / f"{name}-audit.json"
    path.write_text("".join(json.dumps(row) + "\n" for row in games))
    audit.write_text(json.dumps({"sha256": digest(path), "games": len(games), "series": len(games)}))
    return path, audit


def test_packaged_evidence_can_extend_a_verified_release_without_changing_recipe(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"models": {}}))
    (bundle / "history.jsonl.gz").write_bytes(b"test archive")
    parent_policy, selection, result = [tmp_path / f"{name}.json" for name in ("parent", "selection", "result")]
    parent_policy.write_text("{}")
    first = game("released", "2026-10-16", "Later Starter")
    second = game("new-release", "2026-10-19", "Newest Starter", names=CHAMPIONS[20:])
    initial, initial_audit = write_archive(tmp_path, "initial", [first])
    extended, extended_audit = write_archive(tmp_path, "extended", [first, second])
    selection.write_text(json.dumps({
        "recipe": "refresh", "incumbent": incumbent_signature(bundle), "parent_sha256": digest(parent_policy),
        "artifacts": {}, "finalized": True,
    }))
    result.write_text(json.dumps({
        "selection_sha256": digest(selection), "recipe": "refresh",
        "sha256": digest(initial), "promoted": {"pick": True, "pair": False},
    }))
    parent = forecaster()
    output = tmp_path / "deploy" / "event-policy.json"
    for games, audit, day, expected in [
        (initial, initial_audit, "2026-10-20", 1),
        (extended, extended_audit, "2026-10-19", 1),
        (extended, extended_audit, "2026-10-20", 2),
    ]:
        args = SimpleNamespace(bundle=bundle, parent=parent_policy, selection=selection, result=result,
                               games=games, audit=audit, output=output)
        package(args, parent, {PATCH: CHAMPIONS})
        model = EventForecaster.load(parent, bundle, parent_policy, output)
        assert model.recipe == "refresh"
        assert model._snapshot(day, "forthcoming")[-1] == expected
        assert json.loads(output.read_text())["files"]["events"]["sha256"] == digest(games)


def test_patch_pick_artifact_accepts_the_frozen_models_published_patch_extension(tmp_path):
    names = json.loads((Path(__file__).parents[1] / "fearless/assets/champions.json").read_text())["16.13"]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({
        "version": 1, "features": list(DRAFT_FEATURES), "models": {},
        "catalog": {"patches": {"16.12": names[:-1], "16.14": names}},
        "serving": {},
    }))
    with gzip.open(bundle / "history.jsonl.gz", "wt", encoding="utf-8"):
        pass
    artifact = tmp_path / "patch-pick.json"
    artifact.write_text(json.dumps({
        "version": 1, "model_id": bundle_fingerprint(bundle),
        "parameters": {"max_mix": 0.25, "prior_strength": 20, "other_phase_weight": 0.5,
                       "other_game_weight": 0.35, "half_life_days": 7},
    }))
    past = {**game("past", "2026-07-01", "Starter", names=names), "patch": "16.13"}
    context = {
        "date": "2026-07-02", "patch": "16.13", "region": past["region"],
        "first": past["first"], "second": past["second"], "current": past["actions"][:6],
        "pool": names, "game_number": 1,
    }
    frozen = DraftForecaster.load(bundle)
    choices = frozen.forecast(**context, limit=len(names))["choices"]
    assert names[-1] in {row["champion"] for row in choices}
    model = PatchPickModel.load(artifact, bundle).with_completed_series([past])
    assert model.adjust(**context, choices=choices, event=past["event"]) is not None
