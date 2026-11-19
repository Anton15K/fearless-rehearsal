# Data and attribution

The public replay in `public_games.jsonl` is adapted from [LoL-Draft](https://github.com/DeusExMachina1993/LoL-Draft/tree/447d68cd5b5e770aba90979be9efe6b0c398a724), which credits Leaguepedia's ScoreboardGames and PicksAndBansS7 tables under CC BY-SA 3.0. LoL-Draft and this adaptation are licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The adaptation selects LCS games and converts their draft order and team metadata to the application's replay format. It is independent of the GRID archive. Rebuild it with `python scripts/public_replay.py`.

Champion names in `champions.json` come from Riot Games' versioned Data Dragon releases. These are patch release rosters. League officials can impose additional tournament-specific disables. Riot Games owns its game and marks. Fearless Rehearsal is an independent project and is not endorsed by Riot Games.

The aggregate counts and fitted parameters in `public_forecast.json` are derived exclusively from the open LCS adaptation above and are distributed under the same [CC BY-SA 4.0 license](https://creativecommons.org/licenses/by-sa/4.0/). They can be regenerated with `python -m fearless.public_model`.
