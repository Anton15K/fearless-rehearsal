# Role-first pair transfer to unseen patches

**Registered 2026-11-23, before scoring.** This retrospective test uses the three predictors in the existing [role-pair freeze](role-pair-trial.md). All three rank up to three unordered champion pairs from the identical draft prefix at turns 7, 9 and 17. The target is an exact pair. Model weights, ranking logic, and output limits remain frozen at `81af126b8582162b587096878e312cc97274a5c8`.

## Cohort

A patch is unseen if it has no draft in the 1,741-game GRID archive, the 599-game known-event file, or the 101-game Korean confirmation. Listing a patch in the champion catalog is compatible with eligibility. The archive discovery interval is **2026-05-01 through 2026-11-22 UTC**, after the role model's 2026-04-30 fit cutoff. The observed candidate patch numbers from the archive scan are `16.12`, `16.18`, `16.19`, `16.21` and `16.23` (competitive `26.*` aliases). The archive lock registers these Leaguepedia pages, with their Bo3/Bo5 stages selected by the publisher's actual schedule and result:

- `2026 World Star Challengers Invitational`, the seven Bo5 bracket series announced by Riot. Multi-game drafts must satisfy whole-series Fearless legality. [Riot event announcement](https://www.leagueoflegends.com/en-us/news/esports/world-star-challengers-invitational-2026/)
- `2026 Demacia Cup Global Invitational`, the Fearless Bo3/Bo5 Swiss and bracket stages. [Organizer format video](https://www.huya.com/video/play/1127047248.html) and [participating team's event guide](https://navi.gg/en/tournaments/2026-demacia-cup-global-invitational/groups)

Include every qualifying whole series from these pages. Require the published game count and score, one eligible patch throughout, date and unique draft fingerprints, confirmed draft order and map-side mapping on every game, an exact champion pool, and legal actions with cross-team Fearless locks. A missing game or an inconsistent field excludes the entire series. Use the independently recorded `FirstPick` field as confirmation of the first team listed in the draft. Archive game start is a UTC dating proxy and provides no live prediction timestamp. Attach completed-game roles only when all ten champions, players, and roles match, and use these roles only for scoring roles or for dated historical features. Count all verified pair windows regardless of whether final roles are available. Keep source snapshots and labels private and publish only counts and SHA-256 digests.

The Leaguepedia schedule and scoreboard join only returned explicit first-pick records on `16.18` in the discovery scope. The NACL Promotion on `16.12`, CELOL on `16.19` and `16.21`, and Nova on `16.23` had no explicit first-pick records in the targeted sources. The NACL rulebook permits side and first pick to diverge. LoL-Draft's NACL export gives first sides, but the dataset documentation identifies Leaguepedia tables and provides no independent per-game order trace. These events are outside the archive lock. [NACL Rulebook, §2.6](https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/a/a4/NACL_2026_Rulebook.pdf/revision/latest?cb=20260415102347)

## Scoring and decision

Run two exact replays. The strict cold replay excludes all new cohort rows from model histories. The rolling replay releases a whole cohort series for use as historical evidence only after its final game has finished on an earlier UTC date. Both use the original frozen base history and each game's real legal prefix and within-series Fearless locks. Use the same top-three patch-frequency fallback, current adaptive pair predictor, and role-first predictor as the existing trial. Replay never fits or selects weights.

Report overall and per-patch Hit@3 and paired 95% intervals for role-first minus each baseline. Bootstrap whole series, stratified by patch and event for the aggregate, using the existing fixed evaluation routine. Report by event and turn as secondary descriptions. The promotion gate requires all of the following:

1. At least three genuinely unseen patches, each represented by at least two whole series, and at least 24 qualifying whole series across the cohort.
2. The lower endpoint of each paired 95% Hit@3 interval exceeds zero against **both** baselines in **both** replays.
3. Source integrity and forecast legality checks pass for the whole cohort. A passing report calls for review before any serving change.

A one-patch result remains a confirmation on that patch and cannot clear the promotion gate regardless of its score.

## Capturing further patches

For a future patch, register the event and exact stage, date window, independent first-pick authority, draft source, and same predictor identities before viewing the outcomes. A published right-of-first-selection format does not prove which team chose first in a particular game. Accept an explicit per-game source field, an organizer VOD with an unambiguous first lock and independently verified map sides, or an explicit blue-first event rule corroborated by per-game map-side and ordered action records. Capture the full order including passed bans, any tournament disables, the final series result and all games, and official availability. Pin the registration, source snapshots and normalized rows by SHA-256 before replay. If a newly released champion changes the pool, register the exact independently published pool and a metadata-only catalog extension before scoring, under a separate provenance lock. Keep the predictor weights and known-event file fixed. Preserve each completed lock and do not use scored cases for tuning. Once enough complete eligible cases are available, register the aggregate cohort before its first replay under the same gate.
