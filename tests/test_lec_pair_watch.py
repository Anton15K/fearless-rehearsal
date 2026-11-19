from datetime import datetime, timezone
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from fearless.model_features import ChampionCatalog
from scripts import lec_pair_watch as watch


NOW = datetime(2027, 1, 14, 12, tzinfo=timezone.utc)
NAMES = [f"Champion {number:02}" for number in range(30)]
CATALOG = ChampionCatalog({}, patches={"16.23": NAMES})
ALT_STAGE = "LEC/2027 Season/Winter Season/Regular Season"


def fixture_row(stage=watch.STAGE, **changes):
    return {
        "OverviewPage": stage, "MatchId": stage + "_Week 1_1", "BestOf": "3",
        "DateTime_UTC": "2027-01-15 16:00:00", "HasTime": "1",
        "Team1": "G2 Esports", "Team2": "Fnatic", **changes,
    }


def test_first_fixture_is_bounded_and_rejects_placeholder_teams_and_time():
    later = fixture_row(DateTime_UTC="2027-01-15 19:00:00", MatchId=watch.STAGE + "_Week 1_2")
    first = fixture_row()
    match = watch.opening_match([later, first], watch.STAGE, now=NOW)
    assert match["row"] == first
    assert match["starts_at"].isoformat() == "2027-01-15T16:00:00+00:00"
    assert match["deadline"].isoformat() == "2027-01-15T15:30:00+00:00"

    for row in (fixture_row(Team1="TBD"), fixture_row(HasTime="0"), fixture_row(Team2="Unknown")):
        with pytest.raises(watch.Pending, match="unconfirmed"):
            watch.opening_match([row, later], watch.STAGE, now=NOW)
    with pytest.raises(watch.Pending, match="lookahead"):
        watch.opening_match([first], watch.STAGE, now=datetime(2027, 1, 12, 12, tzinfo=timezone.utc))
    with pytest.raises(watch.Pending, match="deadline"):
        watch.opening_match([first, later], watch.STAGE, now=datetime(2027, 1, 15, 15, 30, tzinfo=timezone.utc))


def test_stage_alternative_must_be_tied_to_verified_winter_overview():
    requests = []

    def fetch(url):
        params = parse_qs(urlsplit(url).query)
        requests.append(params)
        if params["action"] == ["query"]:
            return {"query": {"pages": {"1": {"title": watch.STAGE, "links": [
                {"title": "LEC/2027 Season/Spring Season"}, {"title": ALT_STAGE},
            ]}}}}
        rows = [fixture_row(ALT_STAGE)] if ALT_STAGE in params["where"][0] else []
        return {"cargoquery": [{"title": row} for row in rows]}

    schedule = watch.discover_schedule(fetch)
    assert schedule["stage"] == ALT_STAGE
    assert schedule["rows"] == [fixture_row(ALT_STAGE)]
    assert schedule["lookup_source"]
    assert len(requests) == 3


def dragon(versions, pools):
    def fetch(url):
        if url == watch.VERSIONS:
            return versions
        version = url.split("/")[4]
        return {"version": version, "type": "champion", "data": {
            str(number): {"name": champion} for number, champion in enumerate(pools[version])
        }}
    return fetch


def test_patch_candidates_never_clip_unseen_champions_or_rewrite_frozen_pools():
    fetch = dragon(["17.1.2", "17.1.1", "16.24.1", "16.23.1"], {
        "17.1.2": [*NAMES, "New Champion"],
        "16.24.1": NAMES,
    })
    original = {"16.23": NAMES}
    result = watch.resolve_patches(fetch, fixture_row(), CATALOG, original)
    assert result["official_patch"] is None
    assert result["pools"] == {"16.24": NAMES}
    assert result["candidates"]["17.1"]["abstention"] == "unknown_champions"
    assert result["candidates"]["17.1"]["unknown"] == ["New Champion"]
    assert original == {"16.23": NAMES}
    exact = watch.resolve_patches(fetch, fixture_row(Patch="27.1", LegacyPatch="17.1"), CATALOG, original)
    assert exact["official_patch"] == "17.1"
    assert exact["pools"] == {}
    assert set(exact["candidates"]) == {"17.1"}


@pytest.mark.parametrize("version, reason", [("16.23.1", "stale"), ("17.3.1", "outside")])
def test_unknown_patch_feed_abstains_when_stale_or_outside_the_predeclared_range(version, reason):
    with pytest.raises(watch.Pending, match=reason):
        watch.resolve_patches(dragon([version], {}), fixture_row(), CATALOG, {})


def test_an_exact_published_patch_cannot_change_a_previous_pool():
    fetch = dragon(["17.1.1"], {"17.1.1": NAMES})
    result = watch.resolve_patches(fetch, fixture_row(Patch="27.1"), CATALOG, {"17.1": NAMES[:-1]})
    assert result["pools"] == {}
    assert result["candidates"]["17.1"]["abstention"] == "conflicting_frozen_pool"


def test_unpublished_opener_reports_pending_without_touching_private_models(tmp_path):
    out = tmp_path / "private"
    def fetch(url):
        if "cargoquery" in url:
            return {"cargoquery": []}
        return {"query": {"pages": {"1": {"title": watch.STAGE}}}}
    result = watch.watch_once(tmp_path / "nonexistent-freeze", out, fetch=fetch, clock=lambda: NOW)
    assert result["status"] == "pending"
    assert result["reason"] == "schedule_unpublished"
    assert not (out / "seal.json").exists()
    assert json.loads((out / "status.json").read_text()) == result
    assert (out / "status.json").stat().st_mode & 0o077 == 0


def test_seal_uses_both_orders_exact_riot_pool_and_stays_first_write(tmp_path, monkeypatch):
    raw = fixture_row(Patch="27.1", DisabledChampions=["Champion 00"])
    frozen_pools = tmp_path / "base-pools.json"
    frozen_pools.write_text(json.dumps(CATALOG.patches))
    freeze = tmp_path / "base-freeze.json"
    artifacts = {key: tmp_path / key for key in ("bundle", "parent", "adaptive", "roles", "events")}
    artifacts["pools"] = frozen_pools
    freeze.write_text("{}")
    manifest = {"version": 1, "protocol_sha256": "a" * 64}
    monkeypatch.setattr(watch, "load_freeze", lambda path: (manifest, artifacts))
    monkeypatch.setattr(watch, "identities", lambda **files: {"pool": watch.digest(files["pools"])})
    contexts = []

    class Trial:
        ends = {}
        series = []
        archive = []
        frozen = SimpleNamespace(catalog=ChampionCatalog({}, patches={"16.23": NAMES}))

        def before_draft(self, context, **kwargs):
            from fearless.pair_trial import METHODS
            assert context["fearless"] is True and context["game_number"] == 1
            assert context["patch"] == "17.1"
            assert self.frozen.catalog.patches["17.1"] == tuple(NAMES)
            assert "Champion 00" not in context["pool"]
            contexts.append(context)
            return {str(slot): {name: [[NAMES[1], NAMES[2]]] for name in METHODS} for slot in (7, 9, 17)}

    monkeypatch.setattr(watch.PairTrial, "load", lambda **kwargs: Trial())
    public_dragon = dragon(["17.1.1"], {"17.1.1": NAMES})
    def fetch(url):
        if "cargoquery" in url:
            return {"cargoquery": [{"title": raw}]}
        return public_dragon(url)
    output = tmp_path / "private"
    result = watch.watch_once(freeze, output, fetch=fetch, clock=lambda: NOW)

    assert result["status"] == "sealed"
    assert result["sha256"] == watch.digest(output / "seal.json")
    assert {(item["first"], item["second"]) for item in contexts} == {
        ("leaguepedia:G2 Esports", "leaguepedia:Fnatic"),
        ("leaguepedia:Fnatic", "leaguepedia:G2 Esports"),
    }
    assert freeze.read_text() == "{}"
    assert json.loads(frozen_pools.read_text()) == {"16.23": NAMES}
    assert (output / "seal.json").stat().st_mode & 0o077 == 0
    again = watch.watch_once(freeze, output, fetch=lambda _: pytest.fail("already sealed"), clock=lambda: NOW)
    assert again["sha256"] == result["sha256"]
    assert len(contexts) == 2


def test_team_names_join_only_by_frozen_team_and_game_identity():
    archived = {"game_id": "past-game", "series_id": "past", "date": "2026-10-20",
                "first": "47380", "second": "47376"}
    later = {"game_id": "future-game", "series_id": "future", "date": "2027-01-14",
             "first": "future-g2", "second": "future-fnc"}
    trial = SimpleNamespace(archive=[("2026-10-20", archived), ("2027-01-14", later)], series=[], ends={})
    names = [
        {**archived, "team_names": {"47380": "G2 Esports", "47376": "Fnatic"}},
        {**later, "team_names": {"future-g2": "G2 Esports", "future-fnc": "Fnatic"}},
        {**archived, "game_id": "unfrozen", "team_names": {"other-g2": "G2 Esports", "other-fnc": "Fnatic"}},
    ]
    result = watch._team_ids(fixture_row(Team1="G2", Team2="FNC"), trial, "2027-01-14", names)
    assert result == {"G2": "47380", "FNC": "47376"}


def test_network_failure_returns_a_retryable_private_status(tmp_path):
    from urllib.error import URLError

    def unavailable(_):
        raise URLError("Temporary service failure")

    result = watch.watch_once(tmp_path / "freeze", tmp_path / "private", fetch=unavailable, clock=lambda: NOW)
    assert result["status"] == "pending"
    assert result["reason"] == "source_unavailable"
