# Patch and tournament adaptation

## Decision protocol

The starting point is the private policy at commit 0492cfa. It uses the original pick ranker, original unordered pair forecast and the second-phase ban model confirmed at Worlds. Worlds 2026 and previous GRID results are development data for this iteration. Older external events are retrospective untouched benchmarks. Only games whose outcomes postdate the development window can additionally support a temporal transfer claim.

Compare three pick recipes on the 98 development Worlds games: the existing CatBoost ranker with its historical features refreshed, the fitted event-prior adjustment to frozen pick scores, and the same adjustment on refreshed pick scores. Fit adjustment parameters on development data only. Select a single recipe with the highest development Hit@3 among those with log loss no worse than the fixed incumbent; break ties by lower log loss, then prefer the simpler recipe. If none qualifies, keep pick serves frozen. For pairs, fit one direct-retrieval model using only development records and compare it to the actual unordered pair shortlist on the board. Freeze both selected recipes and their parameter hashes before opening the new benchmark results. Leave all bans on the existing policy.

The new benchmark is selected by documented rules, coverage and independent competition identity. Prefer professional tier-one tournaments absent from the archive, including China and Asia-Pacific. Publish the chosen events and frozen input hashes before scoring. Audit draft fingerprints against development data. Test every eligible complete series, all ten pick turns and all three double-pick turns per game. Keep optional bans in the prefix. Compare the same legal choices, Fearless locks and dated archive history for both sides. Do not supply target rosters from scoreboards. Refresh models with prior completed series only, with a strict previous-UTC-day boundary, including across stages of the same tournament when the event identity matches. Start an evaluation without preloaded development event rows. State which standings, if any, an evaluator could have observed before the prediction.

Promote picks when the paired 95% interval for the gain in top-three hits has a lower bound above zero and full legal log loss is no worse. Promote pairs when the paired 95% interval for the gain in top-three unordered hits has a lower bound above zero. For both criteria, resample whole series within each tournament 5,000 times with seed 10429. Metrics weight each draft opportunity equally. Report each event separately, alongside sample size, target coverage, top-choice calibration error for pick probabilities, and any fallbacks. These are per-comparison intervals. Keep any candidate that misses its criterion off the serving policy. A subsequent model selection requires a different untouched benchmark.

### Reserved competitions

Before scoring the challengers, Leaguepedia's scoreboard index identified 71 games at MSI 2026, 80 at LCP Split 3, and 234 across LPL Split 3 (167), Grand Finals (55) and Regional Finals (12). Reserve these five stages for confirmation. Use the three competitions as bootstrap strata. Audit all 385 indexed games against match results, Fearless rules, draft completeness, available champions and archive duplication. When a game lacks verifiable evidence, exclude its entire series and report it. Other tournaments remain available for a future comparison. All three reserved competitions preceded Worlds, so this experiment measures transfer to untouched competitions on multiple patches retrospectively.

Each source game supplies its confirmed ten player/champion/position rows when those rows agree with both completed drafts. The join requires five distinct positions and unique players on each side. It falls back to draft history when verification fails. The game becomes historical evidence only after its entire series has finished on an earlier UTC date. For evaluation, the forecast request itself supplies the actual draft prefix and team identities, with no roster override. Previously unseen players and teams keep stable external identifiers. The private board resolves the same earlier tournament evidence when a coach enters those teams for a new matchup.

### Frozen selection

The Worlds development selection chose `refresh`. Its manifest SHA-256 is `4f0f51ed7373ce5ac09212e9a9a7db90e61ea53622fdf655502bf64258692435`. The cold pick artifact hash is `a4b7cfb44bb5a04f4e97f38b4b3cccd2cfbc5e246fba69b0ad28a65ad3572caa` and the pair artifact hash is `25334c2bf02229a85a7117d2f3a50512d00171cb785fe39e3f2ca30b82748c7c`. The confirmation cohort contains 383 games in 129 complete series. The scoreboard omitted the patch for one two-game LPL series. All 383 included games have all ten verified played positions. [Frozen cohort hashes](event-adaptation-cohort.json)

## Confirmation results

Enable both the refreshed pick ranker and direct unordered-pair model in the private policy. The pick ranker retained the original CatBoost weights. Updating the patch, team, player and role features after completed series outperformed a separately fitted pick-frequency adjustment on development data. The pair model retrieved previously observed legal pairs, including pairs outside the original shortlist, and learned their rank from 720 dated GRID double-pick opportunities in 90 series. The coach sees ranked pairs with dated evidence. The existing branch expansion supplies its own path probabilities.

| Competition | Games | Pick top-three, old → new | Exact pair top-three, old → new |
| --- | ---: | ---: | ---: |
| MSI 2026 | 71 | 23.24% → 32.96% | 5.63% → 5.63% |
| LCP Split 3 | 80 | 24.12% → 40.75% | 5.00% → 10.83% |
| LPL Split 3 and finals | 232 | 26.85% → 42.59% | 5.32% → 14.08% |
| **All three** | **383** | **981/3,830 → 1,548/3,830** | **61/1,149 → 136/1,149** |

Pick accuracy increased **14.80 percentage points** with a paired 95% interval of **+13.17 to +16.37**. Pick log loss improved from **3.775 to 3.162**, while top-choice calibration error rose from **1.65% to 7.92%**. The board labels updated probabilities as estimates. Sixty picks preceded all available tournament evidence and used the original predictor. Exact pair accuracy increased **6.53 percentage points**, with an interval of **+4.75 to +8.45**. The three ranked pairs still miss most exact outcomes. All 3,830 ban forecasts matched the existing serving policy. Every scored choice was legal.

The source was Leaguepedia's match schedule, scoreboards, confirmed first-pick teams, ordered drafts and player records. The sole excluded series lacked a verifiable source patch. No draft matched a fingerprint in the 1,741-row training archive or the 98-game Worlds development set. The LPL and LCP teams were absent from the earlier GRID identity directory, so this also tested how the model learns from previously unseen opponents. [MSI](https://lol.fandom.com/wiki/2026_Mid-Season_Invitational) · [LPL](https://lol.fandom.com/wiki/LPL/2026_Season/Split_3) · [LCP](https://lol.fandom.com/wiki/LCP/2026_Season/Split_3)

### Repeat the private run

Use the authorized GRID archive, the frozen bundle and the ban-only parent policy from the previous comparison. The public demo continues to use its own LCS estimator. All commands run from the repository with its model/data extras. Source snapshots can change after Leaguepedia edits; the confirmation command verifies the locked cohort and catalog hashes.

```sh
w=.local/adaptation
old=.local/improvements
bundle=.local/training/final
archive=.local/training/enriched.jsonl
catalog=fearless/assets/champions.json
mkdir -p "$w"/{development,holdout,pick,pair,serving}
python -m scripts.event_drafts --archive "$archive" --previous "$old/fresh/worlds.jsonl" \
  --catalog "$catalog" --cache "$w/holdout/wire" --output "$w/holdout/games.jsonl" --audit "$w/holdout/audit.json"
python -m scripts.event_rosters fetch --stage '2026 Season World Championship/Play-In' \
  --stage '2026 Season World Championship/Main Event' --stage '2026 Mid-Season Invitational' \
  --stage 'LPL/2026 Season/Split 3' --stage 'LPL/2026 Season/Grand Finals' \
  --stage 'LPL/2026 Season/Regional Finals' --stage 'LCP/2026 Season/Split 3' --output "$w/players.json"
python -m scripts.event_rosters attach --games "$old/fresh/worlds.jsonl" --input-audit "$old/fresh/audit.json" \
  --archive "$archive" --catalog "$catalog" --players "$w/players.json" --event 'Worlds 2026' \
  --output "$w/development/worlds.jsonl" --audit "$w/development/audit.json"
python -m scripts.event_rosters attach --games "$w/holdout/games.jsonl" --input-audit "$w/holdout/audit.json" \
  --archive "$archive" --catalog "$catalog" --players "$w/players.json" \
  --output "$w/holdout/enriched.jsonl" --audit "$w/holdout/enriched-audit.json"
python -m fearless.patch_picks fit --bundle "$bundle" --games "$w/development/worlds.jsonl" --fit-through 2026-10-26 \
  --artifact "$w/pick/model.json" --report "$w/pick/development.json"
python -m fearless.adaptive_pairs fit --bundle "$bundle" --wide-challenger "$old/pair" --output "$w/pair/model.json"
common=(--bundle "$bundle" --parent "$old/serving.json" --catalog "$catalog" --pick "$w/pick/model.json" --pair "$w/pair/model.json")
python -m scripts.evaluate_adaptation develop "${common[@]}" --games "$w/development/worlds.jsonl" \
  --audit "$w/development/audit.json" --output "$w/development/selection.json"
python -m scripts.evaluate_adaptation confirm "${common[@]}" --games "$w/holdout/enriched.jsonl" \
  --audit "$w/holdout/enriched-audit.json" --selection "$w/development/selection.json" \
  --lock docs/event-adaptation-cohort.json --output "$w/holdout/confirmation.json"
python -m scripts.event_rosters combine --source "$w/development/worlds.jsonl" "$w/development/audit.json" \
  --source "$w/holdout/enriched.jsonl" "$w/holdout/enriched-audit.json" \
  --output "$w/serving/events.jsonl" --audit "$w/serving/audit.json"
python -m scripts.evaluate_adaptation package "${common[@]}" --games "$w/serving/events.jsonl" \
  --audit "$w/serving/audit.json" --selection "$w/development/selection.json" \
  --result "$w/holdout/confirmation.json" --output "$w/serving/policy.json"
FEARLESS_GAMES="$archive" FEARLESS_BUNDLE="$bundle" FEARLESS_CHALLENGERS="$old/serving.json" \
  FEARLESS_ADAPTATION="$w/serving/policy.json" ./scripts/start.sh
```

To refresh again, supply each new audited event file to `combine`, package the updated snapshot with the existing decision, and restart the private API. Replays expose only series completed before the requested UTC date. A coach can supply confirmed lineup overrides and match-specific disabled champions during matchup setup.
