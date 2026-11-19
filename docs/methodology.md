# Forecasting the next draft decision

A coach who wants Poppy for Game 3 needs two different answers. The rules can determine whether each hypothetical branch preserves the pick. The forecast estimates which currently legal branch an opponent might take. The coach supplies their own decisions. They can attach a backup and a note at the branch that changes the plan.

## Inputs and historical roles

The private pipeline streamed 745 GRID series files from source revision `496fa7f41df598ede863f1244f8a9b08e2868482`. It recovered 1,741 game records, including two unfinished attempts. It retained 1,718 eligible games and 34,360 ordered draft turns. Exclusions cover incomplete or illegal drafts, missing earlier games, and ambiguous chronology or patch. The importer tracks uncertainty and can write a per-run audit, JSONL and Parquet. [Official participant archive](https://github.com/cloud9esports/stl-hackathon-2026-grid-starter)

Player-to-champion assignments come from completed games. The feeds supplied no direct verified position labels for the 17,390 roster rows. Early support or jungle items and repeated early lane positions supplied historical role evidence for 13,643 rows. The other 3,747 stayed unknown. Lane inference needs at least three observed 30-second buckets with 70% agreement. Ambiguous teammate lane assignments are dropped. A flex pick can accumulate evidence in several positions. Role inference has no independently measured accuracy in this study.

Champion availability uses patch-specific Data Dragon releases. An organizer’s temporary disable needs an adjusted catalog. Both teams’ earlier picks are removed on a Fearless series, and current-game picks and bans are removed at every step. Skipped bans consume a turn. The training target and reported probabilities are conditional on a named champion action.

## Features, training and selection

Separate CatBoost PairLogit rankers score picks and bans. Each training group contains the observed champion and up to 48 legal negatives sampled from common and other legal choices. Evaluation and serving score the full legal pool. Features include patch, region, side in draft order, phase and slot, game number, 56-day popularity, team and player champion pools, target bans, role evidence, and the champions already picked or banned. The baseline smooths historical frequencies across patch and region, draft slot, and game number.

Training history includes only eligible games completed on earlier UTC dates. Same-day series history supplies the locks and already played game information visible to the coach. A roster override is matched to known player identities and the service reports the number recognized. New or unresolved players contribute no invented player evidence.

| Partition | Dates | Games | Use |
| --- | --- | ---: | --- |
| Train | 2025 calendar year | 1,270 | Fit weights, 12,700 turns per action kind |
| Validation | Jan 1–Mar 27, 2026 | 155 | Select serving method and fit temperatures |
| Test | Mar 28–Jun 14, 2026 | 238 | Compare the frozen promotion choice on 89 whole series |
| Later | Jun 15 onward, 2026 | 55 | Additional drift check across 18 series |

The run used one NVIDIA RTX PRO 6000, 300 iterations, seed 10429 and eight CPU threads. Validation selected the learned pick ranker and a ban blend with 80% learned weight. A shared ranker and a feature ablation were evaluated as experiments. An earlier engineering smoke run inspected the same dates, so this is a retrospective chronological evaluation. A prospective blind trial would require fresh series.

## Held-out results

Hit@3 is the percentage of individual turns whose actual champion appears among the three highest-ranked legal choices. Each test column uses 2,380 turns. The uncertainty interval comes from 1,000 paired bootstrap samples grouped by series.

| Forecast | Pick Hit@3 | Ban Hit@3 |
| --- | ---: | ---: |
| Dated frequency baseline | 20.42% | 30.50% |
| Shared pick/ban ranker | 37.69% | 35.13% |
| Separate rankers using meta features only | 24.62% | 31.97% |
| Validation-selected serving methods | **38.45%** | **37.86%** |

| Serving metric | Pick | Ban |
| --- | ---: | ---: |
| Top-three hits | 915 / 2,380 | 901 / 2,380 |
| Absolute Hit@3 improvement, 95% interval | +18.03 pp [16.23, 20.00] | +7.35 pp [5.54, 9.10] |
| Hit@1 | 17.10% | 17.90% |
| Log loss on legal targets | 3.146 | 3.191 |
| Top-choice expected calibration error | 0.066 | 0.011 |
| Legal-target coverage | 100% | 100% |
| Illegal suggestions | 0 | 0 |
| Later-period Hit@3 | 35.27% | 33.45% |

The later-period baselines were 16.18% and 25.45%. The metrics file produced by training includes per-patch, region, phase and game slices. Model estimates describe observed professional behavior. We have not evaluated win impact, the eventual availability of a future pin, or path-level calibration. For two consecutive opponent picks, a pair’s displayed mass is the sum of its represented orders. Omitted orders and candidates remain in the residual.

## Public forecast

Judges can use the same tree with an independent public forecast on the August 1 Cloud9–Dignitas replay. The open LCS corpus contains 160 games. The estimator uses 90 earlier games from 33 series for legal-opportunity counts by action, region, recency and slot. Another 38 games from 16 series set its temperatures. A further 29 games from eight series were held out. The three showcased games were excluded. It has no player-pool feature.

On the public holdout, pick Hit@3 was **9.31%** versus **4.48%** for a simple frequency baseline. Ban Hit@3 was **25.17%** versus **22.41%**. Ban log loss was slightly worse than the baseline (4.105 versus 4.096), as was pick top-choice calibration error (5.17% versus 3.95%). The interface labels this model’s probabilities **estimated**. Its small single-league sample measures a different task population from the private scorecard above. Each public path keeps its unrepresented mass visible.

Rebuild the deterministic, open-data aggregate with the standard Python library from the repository root.

```sh
python3 -m fearless.public_model --games fearless/assets/public_games.jsonl \
  --catalog fearless/assets/champions.json --output .local/public_forecast.json
```

## Reproduce privately

From the repository root, use a participant-authorized local copy of the official archive, Python 3.11+ and the project’s data and model extras. All paths below keep derived data under the ignored `.local` directory.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[data,model,test]'
export GRID_DIR=/path/to/authorized/archive
mkdir -p .local/training
.venv/bin/python -m fearless.data --grid-dir "$GRID_DIR" \
  --output .local/training/games.jsonl --audit .local/training/audit.json \
  --parquet .local/training/games.parquet
.venv/bin/python -m fearless.roles --grid-dir "$GRID_DIR" \
  --games .local/training/games.jsonl --output .local/training/enriched.jsonl
.venv/bin/python -m fearless.model train --games .local/training/enriched.jsonl \
  --catalog fearless/assets/champions.json --output .local/training/bundle \
  --device cpu --threads 8 --iterations 300 --bootstrap-samples 1000 --joint-experiment
```

For Slurm, create a private run directory readable by the allocated node. Place the enriched input at `games.jsonl`, the same patch catalog at `catalog.json`, and the repository’s `fearless` package under `code/fearless`. Export `FEARLESS_RUN_DIR` and an absolute `FEARLESS_PYTHON` pointing to the cluster environment with the model dependencies. Submit `sbatch --account=YOUR_ACCOUNT cluster/train.sbatch`. The job requests one GPU in the RTX partition and writes `bundle/` under the private run directory. GPU and CPU floating-point results may vary.

```sh
FEARLESS_GAMES="$PWD/.local/training/enriched.jsonl" \
FEARLESS_BUNDLE="$PWD/.local/training/bundle" ./scripts/start.sh
```

Open **Match prep → historical Cloud9 Kia vs LYON**. Rehearse from Game 2 with the earlier game’s lineup and locks. Select an opponent turn to inspect the dated ranking. `FEARLESS_DB` can select another private SQLite file and `FEARLESS_CATALOG` can select a tournament-adjusted patch map. The historical corpus, model files and cached histories belong in the participant environment for the licensed hackathon period. The public replay is rebuilt independently via `python scripts/public_replay.py`. [Public data attribution](../fearless/assets/NOTICE.md)

## Development and verification

Five Codex workers handled rules, data, forecasting, API persistence, and the coaching interface in separate Git worktrees. The lead reviewed and integrated the pull requests and ran the production GPU training job. PyCharm inspected the rules source through its command-line inspections. Its pytest runner exercised the integrated Python suite. Browser verification followed a legal Game 2 fork, checked both outcomes for the same future-game pin, saved a branch note, and replayed a historical game into the next draft. The board was also checked at 320px and 390px.

One integration review caught an important distinction. A verified roster identifies the player and champion, while telemetry supplies an inferred lane. The evidence text says “Historical role observations” so a coach can use observed flex history without treating every inferred position as certain.
