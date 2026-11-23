# Unseen-patch pair confirmation results

The frozen role-first predictor improved exact pair Hit@3 against both comparators in the strict cold replay of patch **16.18**. With whole-series history released during the rolling replay, its advantage over the adaptive predictor was **1.9 percentage points** with a 95% interval of **−1.57 to +5.02**. The promotion gate failed on patch coverage and on that comparison. The role-first predictor remains in shadow.

## Locked cohort

The [protocol](unseen-patch-pairs.md) and [cohort lock](unseen-patch-cohort.json) were committed before scoring. The replay contains 90 games in 27 whole series played from September 27 to October 17, 2026, all on the same previously unseen patch. WSCI contributed 28 games from seven series and the Demacia Cup Global Invitational contributed 62 games from 20 series. The source had 145 games across 82 scheduled series. The protocol excluded 52 Bo1 series, two schedule-only records, and one Demacia Bo3 whose published UTC starts and game lengths overlapped within the series. No source draft matched the GRID archive, prior 599-game development file, or 101-game Korean confirmation by the available identifiers or draft fingerprints. Explicit first-pick records put 61 qualifying games on red first and 29 on blue first. All 90 final role lineups matched the drafts, and every scored prefix and returned pair passed legality checks.

Riot documents the seven WSCI Bo5 bracket series, and the Demacia organizer and a participating team describe its Fearless stages. Leaguepedia supplied the cross-checked schedule, sides, first team, and draft actions. [Riot WSCI format](https://lolesports.com/en-US/news/2026-world-star-challengers-invitational), [Huya Demacia format](https://www.huya.com/video/play/1127047248.html), [NAVI Demacia series rules](https://navi.gg/en/tournaments/2026-demacia-cup-global-invitational/groups)

The role-first parameters were fitted on GRID through April 30, 2026. The adaptive pair parameters were fitted through July 31, 2026. Every locked game was played after both cutoffs, and none was used as a development case. The models were frozen before the replay, which used archived prefixes. This measures retrospective transfer on the two sampled events and does not measure live prediction performance.

## Exact unordered pair Hit@3

| History replay | Pair windows | Role-first | Adaptive | Patch frequency | Role-first − adaptive, 95% CI | Role-first − frequency, 95% CI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Strict cold on 16.18 | 270 | 65 (24.1%) | 50 (18.5%) | 51 (18.9%) | +5.56 pp [+1.61, +9.58] | +5.19 pp [+2.46, +8.24] |
| Rolling on 16.18 | 270 | 67 (24.8%) | 62 (23.0%) | 54 (20.0%) | +1.85 pp [−1.57, +5.02] | +4.81 pp [+2.04, +7.94] |

Strict cold used earlier evidence from other patches and the overall frequency fallback in all 270 windows. Rolling released whole series strictly before the target UTC date and still used the overall fallback in 27 windows. Confidence intervals resample paired whole series within event and patch. They measure game variation within the two sampled events on 16.18. They provide no estimate of how the result varies across patches.

| Event and history | Windows | Role-first hits | Adaptive hits | Frequency hits |
| --- | ---: | ---: | ---: | ---: |
| WSCI, strict cold | 84 | 19 | 10 | 12 |
| WSCI, rolling | 84 | 18 | 12 | 9 |
| Demacia, strict cold | 186 | 46 | 40 | 39 |
| Demacia, rolling | 186 | 49 | 50 | 45 |

With rolling history, role-first beat adaptive on the first double-pick window (32 of 90 vs 25) and trailed it in the late window (10 of 90 vs 15). Its two-role combination appeared in the top three on 234 of 270 rolling windows. The distinction matters for this model because correctly identifying the roles still leaves many legal champion combinations.

## Reproduce the private replay

Use the original private model freeze and data artifacts. The normalized game rows, wire snapshots and full diagnostic JSON remain private. SHA-256 for the committed cohort lock is `2ed2a3b6ea1da791bd3ac292d52aa262df048c164550a470b53275d83cc79b67`. SHA-256 for the full score report is `5f28b26fbaaac6b87a7327bad897207f98e368bc7a07f611110f4253f802e7f8`.

From the project root, with the research checkout at `.worktrees/nov23-eval` and the unchanged predictor checkout detached at `81af126b8582162b587096878e312cc97274a5c8`:

```sh
cd .worktrees/role-pair-frozen
study=../../.local/unseen-patch-pairs
runner=../nov23-eval/scripts/unseen_patch_pairs.py
lock=../nov23-eval/docs/unseen-patch-cohort.json
freeze=../../.local/role-pairs/freeze.json
umask 077
mkdir -p "$study"
# Skip this fetch if the independently checked private catalog is already present.
../../.venv/bin/python -m scripts.patch_catalog "$study/catalog.json" 16.18
PYTHONPATH=. ../../.venv/bin/python "$runner" prepare \
  --freeze "$freeze" --archive ../../.local/training/enriched.jsonl \
  --korea ../../.local/role-pairs/holdout/games.jsonl \
  --source-catalog "$study/catalog.json" --work "$study" --lock "$lock"
PYTHONPATH=. ../../.venv/bin/python "$runner" replay \
  --freeze "$freeze" --games "$study/games.jsonl" --lock "$lock" \
  --output "$study/report.json"
```

Preserve the private source snapshots for exact reproduction. Live upstream corrections can produce a different digest. Preparation verifies byte identity against the registered lock if it already exists. The next separate capture needs at least two more previously unseen patches and the same independent per-game source checks. [Future capture protocol](unseen-patch-pairs.md#capturing-further-patches)
