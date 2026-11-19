# Private pair forecast log

## Game 1 pilot

The November 20, 2026 Legends Premier League opener is scheduled for 10:00 UTC. At 17:02 UTC on November 19 we sealed predictions for Forsaken versus Colossal Gaming under both possible first-pick orders, on client patch 16.23 with no declared champion disables. Each scenario contains three champion-pair rankings for each of the three double-pick windows from each of the three models. These are forecasts made before seeing any picks or bans, using 32 shared simulated drafts. The public commitment contains fixture metadata and a digest. The actual file stays in a private directory. [Timestamped commitment](https://github.com/Anton15K/fearless-rehearsal/issues/26#issuecomment-7016129918) · [Organizer schedule](https://docs.google.com/spreadsheets/d/1vTxypUCtYqdDUHwvC9B90nLVG50ZRAl07A4haHOLUcI/edit)

The organizer specifies Tournament Draft and the latest official patch. We registered a standard Game 1 operating pilot with no prior-game locks, using the captain check-in time of 09:30 UTC as the commitment deadline. The experiment treats any mismatch in patch, first-pick teams or disabled champions as ineligible. The organizer did not verify Fearless for this series. Registering both pick orders removes the need to guess which team gets first pick. [Organizer rulebook](https://docs.google.com/document/d/13zPaGwzccwMiRlrkTMj9DI5gGFfXfGz4BE6P_8f61mY/edit) · [Riot patch 26.23](https://www.leagueoflegends.com/en-us/news/game-updates/league-of-legends-patch-26-23-notes/)

The result collector reads the public schedule, explicit first-pick selection, ordered actions, duration and patch. It refuses to label scoreboard gameplay start or pick order as proof of when a champion was locked. Use an independently verified UTC draft start from the registered authority before scoring pre-draft rankings. A public hash receipt proves when the shortlists existed. The organizer's draft timing establishes whether they were available before the draft.

```sh
w=.local/role-pairs
python -m scripts.pair_fixture_result --catalog "$w/catalog.json" \
  --snapshot "$w/pilot/source-snapshot.json" --output "$w/pilot/observed-game.json"
python -m scripts.pair_pilot verify --freeze "$w/freeze.json" --seal "$w/pilot/legends-opener.sealed.json" \
  --sha256 4baf712e8b9ec02061b62d41ac7ae5dc47037eb9db4009e0a8773ae825d5e8a5 \
  --fixture-id legends-premier-s5-r1m1-game1 --published-at 2026-11-19T17:02:58Z \
  --commitment-source 'https://github.com/Anton15K/fearless-rehearsal/issues/26#issuecomment-7016129918' \
  --actual "$w/pilot/verified-outcome.json" --output "$w/pilot/scored.json"
```

The collector exits with status 75 while result data is missing. Its observed-game file contains the ordered result and its provenance. Only a separately verified-outcome file with matching context, timing authority and documented draft start qualifies for the second command. The GitHub comment’s body and server-created timestamp should both be checked against the sealed file.

## Conditional forecasts during a live draft

The SQLite ledger records game commitments, live forecasts, source observations and later outcomes as separate append-only records. A complete game has three eligible double-pick windows. A collector must have access to the observed prefix before the first champion of a pair is locked. Only use a registered source with verified timing. Delayed scoreboards can reconcile outcomes.

All commands share the prefix `python -m scripts.pair_shadow --ledger PRIVATE.sqlite3 --trial TRIAL_ID`. Their JSON input files stay private.

| Command | Required input |
| --- | --- |
| `register --freeze FREEZE --input FILE` | UTC enrollment deadline, trusted timing sources, and the registry of game IDs with start times, eligibility and any exclusion reasons. |
| `commit-game --freeze FREEZE --game GAME --input FILE` | Date, event, region, series ID, patch, first/second team, game number, Fearless locks, disabled champions and confirmed roster overrides (use an empty object if unavailable). Stores simulated pre-draft rankings. |
| `window --freeze FREEZE --game GAME --input FILE` | Exact observed prefix, observation UTC time and registered source. Rankings are computed from the previously committed context. |
| `attest-open --game GAME --input FILE` | An independent observation of the same prefix still open after the prediction was written. |
| `attest-unopened --game GAME --input FILE` | An independent observation that the draft was still unopened after the game commitment. |
| `outcome --game GAME --input FILE` | Twenty ordered actions. Verified actual context includes game ID, patch, both teams in draft order, game number, activated pool and series locks. Verified times and source may accompany the result. |
| `timing`, `verified-context` | Supply later verified facts with `--game GAME --input FILE`. Actual champion lock times are keyed by prefix length. |
| `cancel` | Record cancellation with source and reason using `--game GAME --input FILE`. |
| `score` | Reports pre-draft and live rankings separately with all eligible, missing, unverified, late, mismatched and cancelled windows. |

The operator authenticates the upstream source before submitting observations. The ledger checks its registered identifier, UTC timestamp and consistency with the immutable forecast. No live conditional picks have been claimed for the November 20 pilot.

The local collector checks for the Legends result from November 20 at 10:20 UTC and retries through November 23 at 10:20 UTC while the Mac is logged in and awake. A successful read saves the completed public draft privately and ends collection. Its status remains pending independent draft timing.

## LEC Winter opening-day watch

The hackathon submission closes January 12, 2027. The LEC opener is a follow-up model test after submission. To demonstrate a forecast before the opponent locks a pick during the hackathon, use an organizer-approved scrim with recorded lock times. Riot's free GRID feed supports delayed playback with a five-minute delay. [Hackathon deadline](https://cloud9.devpost.com/) · [Riot Open Access](https://lolesports.com/en-US/news/build-with-official-league-of-legends-esports-data)

Riot announced a January 15, 2027 Winter Split opening and confirmed Fearless Draft and First Selection. Its announcement did not identify the first match or its time. The local watcher checks Leaguepedia for the opening fixture, including an exact UTC start and both team identities. Inside a 48-hour window it can generate a private Game 1 forecast for both possible first-pick teams. The collection deadline is 30 minutes before the scheduled match. [Riot season announcement](https://lolesports.com/en-GB/news/2027-lec-a-look-at-the-year-ahead)

When the event patch is unavailable, the watcher checks at most two compatible candidate patches from Riot Data Dragon and records the assumption with the seal. It checks the champion vocabulary and leaves unsupported patches unscored. The three models, their parameters and historical evidence remain frozen. The accepted fixture, the exact legal pool and the resulting digest are retained privately. An independently timestamped publication of that digest and verified actual draft timing are required by the pilot verifier before a prospective score can be reported.

```sh
python -m scripts.lec_pair_watch --freeze .local/role-pairs/freeze.json \
  --team-archive .local/training/enriched.jsonl --output-dir .local/role-pairs/lec-winter
```

On the project Mac, the login agent runs an initial schedule check and checks hourly January 13–15 UTC. It stops on a seal, a missed deadline or the end of opening day. The Mac must be logged in and awake during the collection window. The watcher records local status, and the receipt contains no champion rankings.
