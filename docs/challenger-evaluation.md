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
