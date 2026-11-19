# Draft challenger evaluation

## Decision protocol

The incumbent is the private pick/ban bundle trained by Aphroditi job 432883. The March–June test and the later GRID sample have already been examined. Use those series for development diagnostics. Reserve the 2026 Worlds play-in and main event for a single retrospective comparison after freezing each challenger. These games occurred after the last eligible GRID game on October 13. This benchmark measures transfer to one international event on a newer patch.

Use all available completed Worlds series with a full draft for every game. Reconcile the game index, actual first-pick team, patch and numbered games. Replay both sides' actions against the patch catalog and Fearless locks. Exclude a whole series on missing or illegal draft evidence and report its reason. Resolve team aliases using pre-event information. Supply the same feature history, prior-game locks and legal choices to both methods. Keep the historical reference fixed at the archive cutoff. The observed prefix supplies the current draft context. Do not use participation or role assignments revealed by the target game to choose a lineup.

Compare the following paired metrics, resampling entire series with replacement 5,000 times (seed 10429). Each interval is a per-comparison 95% interval. They are not simultaneous confidence intervals.

- For consecutive opponent picks, score the unordered observed pair against the three pairs shown at the same decision point. Promote the pair ranking if the lower end of the interval for its Hit@3 gain is positive.
- For the four second-phase bans per game, use the same named-target Hit@3 gate and require full-choice log loss to stay level or improve. Leave opening bans alone.
- For pick probability calibration, require a reduction in full-choice log loss whose paired interval excludes zero, top-choice expected calibration error no higher, and an unchanged pick order. Report calibration coverage and any fallback to the incumbent.
- Require legal suggestions for all three. Report target coverage and all denominators. Evaluate the composed serving policy before activating it. Keep candidates that miss their gates available for reproducible research. A further training or threshold change requires a different unused cohort for confirmation.

Leaguepedia supplies match, first-pick and ordered draft records. The independently licensed public demo has a separate estimator and scorecard. [Worlds 2026](https://lol.fandom.com/wiki/2026_Season_World_Championship)

### Test the pick-calibration refresh

The last GRID patch is 16.17 and Worlds is on 16.20. The calibration policy falls back when its nearby-patch support expires. In addition to checking that frozen-archive behavior, evaluate its intended offline refresh. Before scoring any Worlds predictions, designate October 25 (the end of the first three Swiss playing days) as the refresh cutoff. Fit the existing calibration policy once on completed pre-cutoff picks, including 26 complete Worlds series (45 games) with a frozen one-step predictor. Freeze the resulting artifact and evaluate it on the other 20 complete series (53 games), starting October 26. Use only these 53 games for the calibration promotion decision. Keep CatBoost weights, feature history, pair model and ban challenger independent of the refresh. For pair/ban decisions use all 46 Worlds series (98 games). No series crosses this cutoff.

## Worlds results and serving decision

Promote the second-phase ban forecast. It enumerates feasible assignments for the opponent's revealed champions and estimates how removing each legal champion would affect the open roles, taking team and expected starters' comfort picks into account. Aphroditi job 433320 trained the CatBoost challenger on one RTX PRO 6000. Validation selected a blend with 70% weight on the new model.

| Task | Frozen model | Challenger | Paired change, 95% interval | Decision |
| --- | ---: | ---: | --- | --- |
| Second-phase ban, top-three hit | 63/392 (16.07%) | 109/392 (27.81%) | +11.73 percentage points [+7.55, +15.79] | Enable |
| Unordered pick pair, top-three hit | 28/294 (9.52%) | 36/294 (12.24%) | +2.72 percentage points [0.00, +5.28] | Keep experimental |
| Next pick, log loss after refresh | 3.3997 | 3.3905 | −0.0092 [−0.0192, +0.0016] | Keep frozen probabilities |

The ban model reduced log loss from 3.9354 to 3.3746. Its top-choice calibration error increased from 1.61% to 2.30%, so improved ranking should not be presented as improved top-choice confidence. The pick refresh covered all 530 evaluation picks. Their order stayed identical, with 166/530 top-three hits, while top-choice calibration error increased from 1.97% to 3.16%. The pair challenger reranks candidates using plausible joint positions and earlier team and lineup pair observations. On March–June diagnostics it found 15.69% against 11.62% for the original forecast. Its smaller Worlds gain fell short of the promotion criterion.

The importer included all 98 games in all 46 series from October 15 through November 14 on patch 16.20. All targets were named and legal. The game timestamp and duration determine the UTC completion date used by the refresh. All 588 opening bans retained the frozen probabilities. Across 196 team appearances, 118 matched known historical identities. The others used population priors when team or player evidence was unavailable. The test covers one international tournament and one patch. Path probabilities and impact on match wins still need separate validation. [Leaguepedia Worlds 2026](https://lol.fandom.com/wiki/2026_Season_World_Championship)

### Coaching benchmark

Across the entire event, the selected private forecast put 303 of 980 picks (30.92%) and 307 of 980 bans (31.33%) in its top three. The frozen ban model had 261 hits (26.63%). After the selection decision we ran the existing frequency baseline on the same turns with two evidence cutoffs. With archive history only, it scored 110/980 picks (11.22%) and 230/980 bans (23.47%). With completed Worlds series added strictly before each forecast date, it reached 190/980 picks (19.39%) and 267/980 bans (27.24%). Neither reference used target-day drafts. The serving policy replay reproduced all 109 second-phase ban hits, the original pair shortlist and frozen pick probabilities. The public edition uses its separate open-LCS estimator. [Public and private scorecards](methodology.md#public-forecast)

### Reproduce the comparison

Start in the repository with its data and model extras, the authorized pre-Worlds archive and the frozen bundle described in the methodology. Commands below use ignored private output paths. Run the ban training command on the GPU worker.

```sh
work=.local/improvements
bundle=.local/training/final
games=.local/training/enriched.jsonl
mkdir -p "$work/fresh" "$work/calibration"
python -m fearless.ban_challenger train --games "$games" --frozen "$bundle" \
  --output "$work/ban" --device gpu --iterations 220 --threads 8
python -m fearless.pair_model fit --incumbent "$bundle" --games "$games" --output "$work/pair"
python -m fearless.calibration_replay --games "$games" --bundle "$bundle" --fit-through 2026-10-13 \
  --artifact "$work/calibration/pick.json" --report "$work/calibration/old-splits.json"
python -m scripts.worlds_holdout --archive "$games" --catalog fearless/assets/champions.json \
  --cache "$work/fresh" --output "$work/fresh/worlds.jsonl" --audit "$work/fresh/audit.json"
cohort="$work/fresh/worlds.jsonl"
catalog=fearless/assets/champions.json
python -m scripts.compare_challengers refresh --bundle "$bundle" --games "$cohort" --catalog "$catalog" \
  --previous "$work/calibration/pick.json" --output "$work/calibration/worlds-1025.json"
python -m scripts.compare_challengers configure --bundle "$bundle" --pair "$work/pair" --ban "$work/ban" \
  --calibration "$work/calibration/worlds-1025.json" --output "$work/candidates.json"
python -m scripts.compare_challengers evaluate --bundle "$bundle" --games "$cohort" --catalog "$catalog" \
  --policy "$work/candidates.json" --output "$work/all-gate.json" --selected-policy "$work/serving.json"
python -m scripts.compare_challengers evaluate --bundle "$bundle" --games "$cohort" --catalog "$catalog" \
  --policy "$work/serving.json" --output "$work/serving-gate.json"
python -m scripts.compare_challengers reference --bundle "$bundle" --games "$cohort" --catalog "$catalog" \
  --output "$work/reference.json"
FEARLESS_GAMES="$games" FEARLESS_BUNDLE="$bundle" FEARLESS_CHALLENGERS="$work/serving.json" ./scripts/start.sh
```

The evaluated private bundle fingerprint is `5ac3161294a277376d5b96b799f9229c6bdc18c33ccd6405479ef29c9233a395`. The normalized Worlds file SHA-256 is `93a084eb57b1284997060a244760da1f441b5c29d9216921c7b46fd5b532307d`. The importer records the raw table hashes for detecting later Leaguepedia edits.
