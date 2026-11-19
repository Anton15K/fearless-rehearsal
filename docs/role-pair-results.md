# Role-first pair results

The new model identified the actual unordered pair in **61 of 303** Korean postseason windows. The existing private pair model found **39**, and a same-patch frequency rule found **59**. Role-first passed the comparison with the existing model and missed the predeclared requirement to beat both comparators. The coach board continues serving its existing pair ranking. [Registered trial](role-pair-trial.md) · [Frozen model and cohort identities](role-pair-lock.json)

| Method | Exact pair first | Exact pair in top three | Mean reciprocal rank at three |
| --- | ---: | ---: | ---: |
| Patch frequency | 29/303 (9.57%) | 59/303 (19.47%) | 0.1403 |
| Existing private pair ranker | 13/303 (4.29%) | 39/303 (12.87%) | 0.0803 |
| Role-first challenger | 31/303 (10.23%) | 61/303 (20.13%) | 0.1430 |

Role-first gained **7.26 percentage points** over the existing model (paired 95% interval **+4.29 to +10.26**). Its gain over patch frequency was **0.66 points** (interval **−2.00 to +3.17**). The candidate identified the correct unordered pair of played positions in its top three on **254/303 windows (83.83%)**. There are ten possible two-position combinations before accounting for earlier selections. The study compared exact champion pairs across all three methods. It did not benchmark role-only shortlists from a separate baseline.

| Competition or window | Windows | Frequency hits | Existing model hits | Role-first hits |
| --- | ---: | ---: | ---: | ---: |
| LCK postseason | 165 | 35 | 20 | 35 |
| LCK Challengers postseason | 138 | 24 | 19 | 26 |
| First double-pick (7 prior actions) | 101 | 28 | 15 | 27 |
| Second double-pick (9 prior actions) | 101 | 22 | 18 | 22 |
| Final double-pick (17 prior actions) | 101 | 9 | 6 | 12 |

Of 106 indexed games, 101 games in 25 complete series passed the source checks. One five-game LCK Challengers series had conflicting first-pick records. All included games had ten verified played positions. Only whole series completed on earlier UTC dates contributed team, player and role features. Final played roles supplied the evaluation labels after each forecast. The two Korean competitions used the same patch, 16.16. A future-patch prospective gain remains unverified.

## Repeat the private comparison

Use the verified private artifacts from the [previous study](transfer-evaluation.md#reproduce-and-serve-privately). `events.jsonl` there contains 599 previously examined games. The pending pilot uses Riot patch 16.23; this exact public pool is joined to the earlier production catalog before creating the freeze.

```sh
w=.local/role-pairs
a=.local/adaptation
bundle=.local/training/final
mkdir -p "$w/holdout"
python -m scripts.patch_catalog "$w/holdout/catalog.json" 16.16
python -m scripts.patch_catalog "$w/riot-live.json" 16.23
python - <<'PY'
import json
from pathlib import Path
base = json.loads(Path('.local/adaptation/production/catalog.json').read_text())
live = json.loads(Path('.local/role-pairs/riot-live.json').read_text())
Path('.local/role-pairs/catalog.json').write_text(json.dumps(base | live, sort_keys=True) + '\n')
PY
python -m fearless.role_first_pairs fit --bundle "$bundle" --output "$w/model.json"
python -m scripts.pair_trial freeze --bundle "$bundle" --parent .local/improvements/serving.json \
  --adaptive "$a/pair/model.json" --roles "$w/model.json" --events "$a/production/events.jsonl" \
  --pools "$w/catalog.json" --output "$w/freeze.json"
python -m scripts.pair_trial replay --development --freeze "$w/freeze.json" --games "$a/production/events.jsonl" \
  --audit "$a/production/audit.json" --output "$w/development.json"
python -m scripts.pair_trial lock --freeze "$w/freeze.json" --cohort docs/role-pair-cohort.json \
  --source-catalog "$w/holdout/catalog.json" --output "$w/reproduced-lock.json"
python -m scripts.pair_trial replay --lock docs/role-pair-lock.json --freeze "$w/freeze.json" \
  --games "$w/holdout/games.jsonl" --audit "$w/holdout/audit.json" --output "$w/confirmation.json"
```

The role-first model selected three smoothing parameters using log loss over 825 GRID windows in complete series through April 30, 2026. At a new window it averages over possible assignments of already picked flex champions, estimates which two roles remain likely, and combines role-conditioned patch, player, team and previously observed pair counts. All three forecasters receive the same legal prefix and patch pool. [Model implementation](../fearless/role_first_pairs.py)
