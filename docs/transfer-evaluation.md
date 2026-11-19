# Refreshed bans and pick confidence

Use a separate, previously unopened cohort to evaluate two further changes. The pick and pair decisions described in the event-adaptation study are frozen. Whole-series features, lineups and current draft prefixes follow the same UTC availability rules. The model parameters were chosen on already examined events, some later than the confirmation games, so this is a retrospective transfer comparison.

1. Refresh the existing opening and second-phase ban models with earlier completed tournament drafts. Compare against the actual promoted ban policy. Require a positive lower 95% confidence bound on the gain in all-ban top-three accuracy, overall full-choice log loss no worse, and log loss no worse in either ban phase separately.
2. Fit one scalar pick temperature on the previously examined Worlds, MSI, LCP and LPL predictions. Apply it only to picks from the refreshed model. Require identical champion order at every opportunity, a negative upper 95% confidence bound on the difference in pick log loss and lower top-choice expected calibration error (ten fixed-width bins).
3. Compute paired intervals with 5,000 bootstrap draws of complete series within each competition, seed 10429. Preserve named-action and legal-pool denominators. Publish per-competition and per-phase metrics. The tests are separate and the intervals are per comparison.

Reserve EMEA Masters Summer 2026 Main Event (193 indexed games), CBLOL Split 2 Playoffs (40), and the LCP 2027 Promotion tournament played in November 2026 (18). Exclude a complete series if its patch, order, participants, Fearless rules or legal replay cannot be verified. For an event with no per-game first-pick field, permit recovery only if the dated rulebook explicitly fixes blue as first pick and the map-side and draft records agree for that game. Report exclusions before scoring. Disallow overlap with GRID, Worlds and the 383-game first-round confirmation set. Hash the final normalized cohort, both candidates and the legal pool before opening aggregate results.

Coverage fallback fixed while source validation was in progress and before confirmation scoring: if fewer than 50 EMEA games can verify side and first pick separately under the event rules, replace that stage with the entire LPL 2026 Split 2 Playoffs (68 indexed games). Retain CBLOL and the LCP promotion stage as the other strata. All four candidate stage identities are disjoint from the first-round stages. This decision uses only input provenance and availability.

The input lock includes 118 eligible games in 31 complete series. LPL contributed 68 games in 18 series, CBLOL contributed 32 in eight, and the LCP promotion tournament contributed 18 in five. Two CBLOL series (eight games) have conflicting first-pick records. EMEA failed the coverage rule. Ninety of its 193 records contradicted blue-first order. Every retained game has an explicit first pick and ten verifiable played positions. The importer found zero overlapping drafts across the four indexed candidate stages and the 2,222 games already used. The prior evidence stream has 481 games. The frozen temperature is 1.1051709180756477. The [cohort manifest](transfer-cohort.json) records exact sample, legal pool, prior evidence and candidate hashes.

## Confirmation results

Refresh opening and second-phase ban forecasts using completed tournament history. Keep the existing refreshed pick probabilities. The starting pick rankings placed the actual champion in their top three on **484 of 1,180 turns (41.02%)** in this second cohort.

| Competition | Games | Next ban in the top three, previous → refreshed | Pick log loss, served → trial temperature |
| --- | ---: | ---: | ---: |
| CBLOL Split 2 Playoffs | 32 | 74/320 → 109/320 | 3.0964 → 3.1019 |
| LCP Promotion | 18 | 54/180 → 64/180 | 3.1193 → 3.1335 |
| LPL Split 2 Playoffs | 68 | 236/680 → 312/680 | 3.0792 → 3.0805 |
| **All three** | **118** | **364/1,180 → 485/1,180** | **3.0899 → 3.0944** |

Ban top-three accuracy rose from **30.85% to 41.10%**. The paired gain was **10.25 percentage points** (95% interval **+6.90 to +13.72**). Ban log loss improved from 3.4245 to 3.0170 and top-choice calibration error fell from 3.93% to 2.35%. The existing rankers used tournament evidence on 1,100 bans. The remaining 80 used the previous policy. All 1,180 bans named a legal champion.

| Ban phase | Top-three hits, previous → refreshed | Paired gain in points (95% interval) | Log loss, previous → refreshed |
| --- | ---: | ---: | ---: |
| Opening | 235/708 → 345/708 | +15.54 (+11.06 to +20.22) | 3.4242 → 2.8758 |
| Second phase | 129/472 → 140/472 | +2.33 (−1.84 to +6.70) | 3.4249 → 3.2288 |

The confidence candidate preserved every pick's full ranking and improved top-choice calibration error from **6.01% to 2.91%** across 1,180 turns. It assigned slightly less probability to the champions actually picked overall. The log loss difference was +0.0044 (95% interval −0.0031 to +0.0122). It missed its promotion criterion. The board continues to label refreshed pick and ban percentages as estimates.

Leaguepedia supplied archived drafts, first selection, played positions and UTC records. The included stages used patches 16.10, 16.17 and 16.22. The source checks also considered EMEA Masters Summer, which was excluded before scoring. The model weights and recipes were selected using some later competitions, even though each replay exposed only earlier historical features. These results establish retrospective transfer. A prospective coach trial is still needed to measure preparation time, decision quality or match outcomes. [LPL](https://lol.fandom.com/wiki/LPL/2026_Season/Split_2_Playoffs) · [CBLOL](https://lol.fandom.com/wiki/CBLOL/2026_Season/Split_2_Playoffs) · [LCP Promotion](https://lol.fandom.com/wiki/LCP/2027_Season/Promotion)

## Reproduce and serve privately

Start with the artifacts from the [previous study](event-adaptation.md#repeat-the-private-run). Use cached Leaguepedia and Riot responses when reproducing historical source snapshots. Run the following commands from the repository with its data and model extras.

```sh
w=.local/adaptation
t="$w/transfer"
prod="$w/production"
bundle=.local/training/final
parent=.local/improvements/serving.json
archive=.local/training/enriched.jsonl
mkdir -p "$t" "$prod" "$w/confidence"
python -m scripts.patch_catalog "$t/catalog.json" 16.10 16.17 16.18 16.20 16.22
python -m scripts.event_drafts --cohort transfer --archive "$archive" --catalog "$t/catalog.json" \
  --previous .local/improvements/fresh/worlds.jsonl --previous "$w/holdout/games.jsonl" \
  --cache "$t/final-wire" --output "$t/drafts.jsonl" --audit "$t/draft-audit.json"
python -m scripts.event_rosters fetch --stage 'CBLOL/2026 Season/Split 2 Playoffs' \
  --stage 'LCP/2027 Season/Promotion' --stage 'LPL/2026 Season/Split 2 Playoffs' --output "$t/players.json"
python -m scripts.event_rosters attach --games "$t/drafts.jsonl" --input-audit "$t/draft-audit.json" \
  --archive "$archive" --catalog "$t/catalog.json" --players "$t/players.json" --output "$t/games.jsonl" --audit "$t/audit.json"
python -m fearless.event_confidence fit --bundle "$bundle" --parent "$parent" --catalog fearless/assets/champions.json \
  --games "$w/development/worlds.jsonl" "$w/holdout/enriched.jsonl" --artifact "$w/confidence/model.json" \
  --report "$w/confidence/development.json"
python -m scripts.transfer_forecasts --bundle "$bundle" --parent "$parent" --games "$t/games.jsonl" \
  --catalog "$t/catalog.json" --lock docs/transfer-cohort.json --prior-events "$w/serving/events.jsonl" \
  --prior-sha256 a857dabd12740eb62425a0d7600cb2cf1e8ac71a929d9a536a2b91baacc9376d \
  --confidence "$w/confidence/model.json" --confidence-sha256 4fd69a34fdb491cb6056d99e68fc97a14d193364208154acfd700512f8be26c6 \
  --output "$t/confirmation.json"
python -m scripts.event_rosters combine --source "$w/serving/events.jsonl" "$w/serving/audit.json" \
  --source "$t/games.jsonl" "$t/audit.json" --output "$prod/events.jsonl" --audit "$prod/audit.json"
python -m scripts.evaluate_adaptation package --bundle "$bundle" --parent "$parent" --catalog fearless/assets/champions.json \
  --pick "$w/pick/model.json" --pair "$w/pair/model.json" --games "$prod/events.jsonl" --audit "$prod/audit.json" \
  --selection "$w/development/selection.json" --result "$w/holdout/confirmation.json" --output "$prod/event-policy.json"
python -m scripts.promote_transfer --bundle "$bundle" --parent "$parent" --prior-policy "$w/serving/policy.json" \
  --event-policy "$prod/event-policy.json" --games "$t/games.jsonl" --confidence "$w/confidence/model.json" \
  --result "$t/confirmation.json" --lock docs/transfer-cohort.json --output "$prod/transfer-policy.json"
python - "$t/catalog.json" "$prod/catalog.json" <<'PY'
import json
from pathlib import Path
import sys
base = json.loads(Path('fearless/assets/champions.json').read_text())
extra = json.loads(Path(sys.argv[1]).read_text())
assert all(set(base[p]) == set(extra[p]) for p in base.keys() & extra.keys())
pools = {key: sorted(set(names)) for key, names in (base | extra).items()}
Path(sys.argv[2]).write_text(json.dumps(pools, sort_keys=True) + '\n')
PY
FEARLESS_BUNDLE="$bundle" FEARLESS_GAMES="$archive" FEARLESS_CATALOG="$prod/catalog.json" \
  FEARLESS_CHALLENGERS="$parent" FEARLESS_ADAPTATION="$prod/event-policy.json" \
  FEARLESS_TRANSFER="$prod/transfer-policy.json" ./scripts/start.sh
```

The combined private serving archive contains 599 games in 206 complete series. The packaging command verifies that its contents equal the two evaluated evidence sets. Neither the licensed rows nor model weights are included in the public checkout.
