# Fearless Rehearsal

A League of Legends coach can fork a pick or ban during prep, see the opponent’s likely replies, and trace what happens to a champion reserved for a later game. The board keeps the draft order, both teams’ locks, each branch and its coach notes together. Built for the League of Legends Macro Scouting & Draft Management category of the Cloud9 × JetBrains hackathon.

![A Game 2 rehearsal with the draft sequence, available decisions, and a Poppy reservation for Game 3](docs/rehearsal-board.png)

[Watch the 80-second coach walkthrough](docs/walkthrough.mp4)

## Run the board

From a checkout, run the following command with Python 3.11+ and Node.js 22 installed. It creates a virtual environment, builds the frontend, starts FastAPI and serves the board at [localhost:8000](http://localhost:8000).

```sh
./scripts/start.sh
```

GitHub Codespaces can also run the repository’s dev container. After setup, open forwarded port 8000. [Open in Codespaces](https://codespaces.new/Anton15K/fearless-rehearsal)

For a static standalone build, run `npm --prefix web ci` and `npm --prefix web run build:demo`, then `python3 -m http.server 8002 --directory web/dist-demo`. Open [localhost:8002](http://localhost:8002). This edition runs the same Python draft rules inside a browser worker and saves plans in that browser’s IndexedDB. The runtime, draft data and public forecast are served from the build directory. Champion portraits load from Riot when available.

## Rehearse a decision

For an upcoming series, choose **Match prep → New matchup**. Enter both teams, your coaching team, first pick, as-of date, patch, draft format and series length. Choose **First Selection**, **Classic Blue First**, or the organizer’s policy. The board recognizes 2026 LEC and LCS First Selection. For an unrecognized event it leaves the assignments to the organizer. Add a roster override if needed. The board starts at Game 1 with no actions or series locks. Known team and player identities come from games completed before the selected date.

At a game opening, changing first pick refreshes the decision panel for the team now on the clock. **Champion eligibility** shows the effective game pool and the source of known restrictions. For 2026 LEC the board applies the catalogued new-champion release restriction. Enter organizer decisions for reworks, bug disables and events with manual eligibility. Historical games retain sourced restrictions when available. [LEC rulebook and competition policies](https://www.emea-competitiveops.com/)

In **Hard Fearless**, either team's picks leave the pool for the rest of the series. In **Standard**, picks return every game. Bans expire each game in both modes. On a prospective board, record the winner after the twentieth turn. For a continuing 2026 First Selection series, enter which choice the previous game's loser made and each team's resulting side and draft position. The board ends the confirmed series at one, two or three wins in a Bo1, Bo3 or Bo5 respectively. You can open a hypothetical game for prep within the selected series length. In an archived match, **Reveal historical result** advances the score from sourced results.

1. Open the illustrative Cloud9 Kia–LYON board. It starts at Cloud9’s last ban of the first ban phase in Game 2. Blaber has Poppy pinned for Game 3.
2. Choose the **Poppy** ban. Neither team can pick her in Game 2. The ban expires at the game boundary, so the Game 3 pin is protected.
3. Return to the opening position and ban **Vi**. Poppy stays available. Explore LYON picking Poppy, then compare the two branches. The same pin now shows a conflict because either team’s pick locks the champion for the remainder of the series.
4. Save a backup and attach a note to the branch in **Coach brief**. Reopen the plan from **Match prep**. Server-backed plans use SQLite and a browser-specific cookie. Clearing that cookie removes access from that browser.
5. In **Match prep**, choose the historical Cloud9–Dignitas replay. Follow the recorded actions or fork at any turn. Replay can advance along any branch that still matches the recorded draft prefix.

## What the rankings mean

| Board | Evidence shown |
| --- | --- |
| Illustrative Poppy scenario | Authored coaching options that demonstrate the rules and pin outcomes. |
| Historical board in a public checkout | An independently fitted LCS frequency model. It shows estimated probabilities, available historical support, and conditional paths. |
| Historical board with the private evaluated bundle | A model ranking of legal choices at this turn, with evidence dates and calibrated probabilities when available. On opponent turns, the board can expand one or two replies and group consecutive picks in either order. |

The top three are three candidates for **one next action**. A path combines successive forecasts and stops at the coach’s next decision. The board displays how much probability the visible paths cover and how much lies outside them. It does not estimate which team wins the match.

On 238 held-out pro games, the original private models put the actual next champion in the top three on **38.45% of picks** and **37.86% of bans**. The dated frequency baseline scored 20.42% and 30.50%. [Dataset, evaluation and training instructions](docs/methodology.md)

On the 98 games at Worlds 2026, the selected private model identified **30.92% of picks** and **31.33% of bans** in its top three. Modeling the opponent’s remaining comfort pool raised second-phase ban accuracy from **16.07% to 27.81%**. [Worlds comparison and private serving instructions](docs/challenger-evaluation.md#worlds-results-and-serving-decision)

The private board also learns from earlier completed tournament series. On 383 previously unused MSI, LPL and LCP games, it raised next-pick top-three accuracy from **25.61% to 40.42%** and exact unordered-pair top-three accuracy from **5.31% to 11.84%**. [Tournament transfer test and serving instructions](docs/event-adaptation.md#confirmation-results)

A second comparison used 118 previously unused LPL, CBLOL and LCP promotion games. Refreshing ban history raised next-ban top-three accuracy from **30.85% to 41.10%**. Pick accuracy was **41.02%** on this cohort. A trial change to pick confidence missed its log-loss criterion. The board keeps the existing pick estimates. [Ban and confidence comparison](docs/transfer-evaluation.md#confirmation-results)

A simple shortlist of the most frequent legal actions on the patch scored **16.84% on picks**, **30.68% on bans** and **10.62% on exact pairs** in the corresponding comparisons. The pair model’s advantage over pair frequency remains uncertain. [Patch-popularity comparison](docs/transfer-evaluation.md#comparison-with-patch-popularity)

A role-first challenger found **61/303** exact pairs on two previously unused Korean postseason stages. The existing model found **39/303** and patch frequency found **59/303**. The narrow edge over frequency missed the promotion criterion. [Role-first model and results](docs/role-pair-results.md)

A separate retrospective test on an unseen patch covered 90 games and 270 exact two-pick forecasts. With earlier series available, role-first hit 24.8%, the current predictor hit 23.0%, and patch frequency hit 20.0%. The 95% interval for the lead over the current predictor included zero. The candidate remains in shadow. [Unseen-patch results](docs/unseen-patch-results.md)

## Project map

| Source | Responsibility |
| --- | --- |
| `web/src/` | React and TypeScript draft board, matchup setup, branch comparison, pinning and coach brief |
| `fearless/rules.py`, `fearless/outlook.py` | Draft turns, champion legality, series locks, pin evaluation and conditional path expansion |
| `fearless/api.py`, `fearless/rehearsal.py` | FastAPI endpoints and persisted rehearsal trees |
| `fearless/data.py`, `fearless/grid.py`, `fearless/roles.py` | Feed normalization, audit and historical role evidence |
| `fearless/model*.py`, `cluster/train.sbatch` | As-of-date features, training, calibration, evaluation and GPU job |

The public checkout includes an independently licensed historical replay. Official feeds, derived rows and the trained bundle stay in participants’ private environments. Source code is MIT licensed. The data and Riot assets have separate terms. [Asset attribution](fearless/assets/NOTICE.md) · [GRID license](https://github.com/cloud9esports/stl-hackathon-2026-grid-starter/blob/main/LICENSE.md)

## Verify a checkout

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[data,model,test]'
.venv/bin/python -m pytest -q
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
```

The automated checks run on pull requests. The team also used PyCharm’s command-line inspections on the draft-rule module and its bundled pytest runner on the integrated backend. [Build and verification notes](docs/methodology.md#development-and-verification)
