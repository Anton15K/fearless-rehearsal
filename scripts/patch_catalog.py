"""Fetch Data Dragon champion names by game patch for reproducible legal pools."""
import argparse
import json
import urllib.request
from pathlib import Path


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("patches", nargs="+")
    args = parser.parse_args()
    versions = fetch("https://ddragon.leagueoflegends.com/api/versions.json")
    catalog = {}
    for patch in args.patches:
        version = next(v for v in versions if v.startswith(patch + "."))
        data = fetch(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json")
        catalog[patch] = sorted(c["name"] for c in data["data"].values())
    args.output.write_text(json.dumps(catalog, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
