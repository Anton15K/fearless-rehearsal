import json
import zstandard
from fearless.roles import match_roles


def test_conflicting_lanes_stay_unknown_and_item_roles_are_dated(tmp_path):
    location = {"x": 3100, "y": 12200}
    players = [{"id": name, "position": location} for name in ("a", "b")]
    players += [{"id": "j", "inventory": {"items": [{"name": "Scorchclaw Pup", "quantity": 1}]}}]
    path = tmp_path / "test.zst"
    with zstandard.open(path, "wt") as out:
        for second in (150, 180, 210):
            game = {"id": "g", "clock": {"currentSeconds": second}, "teams": [{"id":"t", "players": players}]}
            out.write(json.dumps({"events": [{"seriesState": {"games": [game]}}]}) + "\n")
    assert match_roles(path) == {("g", "t", "j"): ("jungle", "observed role item")}
