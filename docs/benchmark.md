# Compare Setrya with plain pytest

The pgvector benchmark schedules final queries after the worker returns its terminal receipt in both the Setrya and pytest runs. This makes the stale and guarded revisions comparable at the same observation. The ordinary live drill also samples while the worker is settling. [Comparison rules](evidence.md#compare-two-saved-runs)

Both tools can catch the bundled defects. This comparison measures the work of getting to that result, handing it off, and making the next change. The fixtures cover a late refund overwrite and a delete-and-reupload that removes a good passage from the top three.

## Run the synthetic comparison

Start the [disposable pgvector database](pgvector.md), then run this from the same environment.

```sh
python3 -m setrya.examples.pgvector_search.benchmark --repeats 3 --output /tmp/setrya-benchmark
```

The output directory contains machine timings, an environment record, the plain pytest logs, the Setrya JSON reports, and offline comparisons. Each implementation uses the same HTTP search endpoints, caller, queries, depths, database, and worker hooks. Each trial gets a fresh fixture schema. Trial order alternates between Setrya and pytest. Both implementations have to detect the broken cases and pass the guarded cases.

Fixture-to-checkpoint time begins before starting the HTTP app and ends at its worker acknowledgment. Whole-command time includes interpreter startup, the check, and cleanup. The comparison time is the wall time for a separate process reading both reports after their fixtures close. These are machine timings on a warm database. See the **pgvector-benchmark** artifact on the [CI runs](https://github.com/davidahma/setrya/actions/workflows/ci.yml) for the recorded results.

In a local run with PostgreSQL 17.12, pgvector 0.8.7 and Python 3.13.7, both implementations caught both defects and passed both fixes. These are median whole-command times over three runs per cell.

| Fixture | Setrya broken / fixed | Plain pytest broken / fixed |
| --- | --- | --- |
| Refund overwrite | 0.459 s / 0.449 s | 0.450 s / 0.494 s |
| Delete and re-upload | 0.454 s / 0.456 s | 0.517 s / 0.512 s |

Reading the saved Setrya evidence took a median 0.044 s. These numbers describe process runtime. The timed human handoff uses the separate scorecard below.

## Measure an unaided handoff

Give engineers who have not seen the adapter the same disposable app, fixture instructions, worker hooks and local dependencies. Both arms may use the same coding agent, model, tools and time budget. Give each arm a fresh conversation and checkout. Encourage the engineer to use the agent as they normally would and record its contribution. Alternate which tool they use first. Record active minutes and any help received. Use the first indexing change for setup and diagnosis, then give them the re-upload change and record what they edit and rerun. Give each result to a reviewer who did not write that check.

| Measure | Start and finish | Proposed target |
| --- | --- | --- |
| Active setup | Start from a clean checkout. Stop when broken and guarded runs produce the expected diagnosis. Record any time spent waiting on prerequisites separately. | The adopter connects the second app in one hour. |
| Reviewer diagnosis | Start when the reviewer gets the exact commit and artifacts. Stop when they independently name the affected query and decisive observation. Record wrong conclusions and requests for help. | Two minutes. |
| Reuse | Start when the engineer gets the second indexing change. Stop when an updated check catches the missing top-three passage and passes the guarded worker. Record active time, changed files, and what the reviewer has to ask for. | Compare with the same measures for pytest. |

Use a timer and retain the commit, verdict, and artifact with each observation. For Setrya, hand over the two saved reports. For pytest, hand over the ordinary failure output and rerun instructions. Ask both reviewers to explain the case with missing write evidence in a separate exercise.

Compare diagnosis and handoff quality alongside active time. If the plain test is equally usable, keep the recipes and reassess further tooling. A team using the check on a later release is the adoption signal. Across later releases, two teams reusing the check and an engineering owner willing to fund ongoing maintenance would justify testing the paid support hypothesis. Backfill cutover and permission changes are candidates when those teams can point to a recurring release problem. Human setup, reviewer timing, reuse, and willingness to pay still need observation.

### Coding-agent trial of the endpoint starter

One coding agent used the README, the endpoint guide, the generated test, and the public type signatures to connect a disposable Python HTTP app. It built a worker that paused before filling the answer cache. The guarded configuration passed on the first connected run. Disabling its revision check exposed the private answer to the revoked caller. After shutting down the app, the agent replayed both results and the untouched starter’s missing-`seed` result from a separate directory.

The exercise took 107 seconds from setup through verification. The agent built the app for this trial and could give it the required worker and HTTP observations. A team adopting Setrya may have to add those observations to an existing app. The one-hour onboarding and two-minute review targets above still need human participants.

### Record the session

With Setrya installed, create a private scorecard outside Git. The study tool reads and writes local files.

```sh
python -m setrya.examples.handoff_study init ../handoff-session.json
python -m setrya.examples.handoff_study report ../handoff-session.json
```

Use aliases for the engineer, reviewer and repository. Record the date, which arm ran first, and the coding agent, tools and time budget available equally to both arms. Mark `own_application` true only for a participating team's own app. Give the reviewer just the commit and ordinary handoff. Ask which caller got which answer, when it happened relative to the worker, and what the evidence leaves unknown. Keep the answer key with the coordinator.

For each arm and change, enter the application commit (the same revision in both arms), evidence location, active setup and review seconds, the reviewer's answer, whether it was correct, any help, and any earlier incorrect conclusions. For Setrya, use the [PR handoff](recording.md#use-it-in-a-pull-request). In the plain pytest arm, allow the engineer to improve the handoff with the same coding agent. Count that work. Compare time on correct decisions and keep incorrect and unfinished decisions visible.

Count a repository as **activated** when a different human correctly reviews its first Setrya drill against the team's own endpoint. Count the assigned second exercise as a measure of maintenance effort. For evidence of **independent reuse**, follow up after a later release. Record the commit, artifact, and whether the team reran the drill or added a second route without the facilitator. Set `team_chose_to_reuse` to true when a team member confirms they chose to use Setrya for that change. An unattended CI run or a facilitator's assignment leaves voluntary reuse unmeasured. Leave the follow-up blank until it happens.

If plain pytest is as easy to review and maintain, ask the team which changed routes still escape coverage. The [coverage command](coverage.md) lets a platform lead try that workflow in the repository. A funded request to track gaps across repositories would be evidence for a hosted product. The SDK, local checks, CI handoff and replay are available in the open-source package.

The one-hour setup and two-minute review targets remain unmeasured until an unfamiliar engineer and a separate reviewer complete the exercise.
