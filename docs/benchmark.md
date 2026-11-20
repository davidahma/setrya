# Compare Setrya with plain pytest

Both tools can catch the bundled defects. This comparison measures the work of getting to that result, handing it off, and making the next change. The fixtures cover a late refund overwrite and a delete-and-reupload that removes a good passage from the top three.

## Run the synthetic comparison

Start the [disposable pgvector database](pgvector.md), then run this from the same environment.

```sh
python3 -m setrya.examples.pgvector_search.benchmark --repeats 3 --output /tmp/setrya-benchmark
```

The output directory contains machine timings, an environment record, the plain pytest logs, the Setrya JSON reports, and offline comparisons. Each implementation uses the same HTTP search endpoints, caller, queries, depths, database, and worker hooks. Each trial gets a fresh fixture schema. Trial order alternates between Setrya and pytest. Both implementations have to detect the broken cases and pass the guarded cases.

Fixture-to-checkpoint time begins before starting the HTTP app and ends at its worker acknowledgment. Whole-command time includes interpreter startup, the check, and cleanup. The comparison time is the wall time for a separate process reading both reports after their fixtures close. These are machine timings on a warm database. See the **pgvector-benchmark** artifact on the [CI runs](https://github.com/davidahma/setrya/actions/workflows/ci.yml) for the recorded results.

In a local run with PostgreSQL 17.12, pgvector 0.8.7 and Python 3.11.13, both implementations caught both defects and passed both fixes. These are median whole-command times over three runs per cell.

| Fixture | Setrya broken / fixed | Plain pytest broken / fixed |
| --- | --- | --- |
| Refund overwrite | 0.424 s / 0.413 s | 0.496 s / 0.486 s |
| Delete and re-upload | 0.465 s / 0.440 s | 0.470 s / 0.511 s |

Reading the saved Setrya evidence took a median 0.041 s. These numbers describe process runtime. The timed human handoff uses the separate scorecard below.

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

### Participant scorecard

Use one copy per engineer and change. A coordinator records the commit IDs and expected observations before handing out the task. On the first change, ask the engineer to decide whether the late refund write was fixed. On the second, ask whether a re-upload still loses a required passage in the top three. Give the reviewer only the engineer’s normal handoff. Start the reviewer’s timer when they open it. Stop it when they name the affected caller and query, the decisive observation, and what remains unknown.

| Record | Setrya and pytest | Plain pytest |
| --- | --- | --- |
| Engineer / independent reviewer, trial date, task order | | |
| App commits, fixture version, coding agent and tools available | | |
| First change: active setup minutes / prerequisite wait | | |
| First change: reviewer answer / elapsed seconds / correct? | | |
| Second change: active minutes / files edited / reruns | | |
| Second change: reviewer answer / elapsed seconds / correct? | | |
| Help requested, who supplied it, and elapsed minutes | | |
| Incorrect conclusions before correction; artifacts handed over | | |

For the Setrya arm, use the [one-command PR handoff](recording.md#use-it-in-a-pull-request). For the plain arm, the engineer can keep ordinary pytest output, add equivalent assertions, and use the coding agent to improve the handoff. Count those edits and reruns in the corresponding arm. Compare time only for correct decisions and record incomplete decisions separately. If pytest matches Setrya on accurate review and the second change, investigate missing coverage with the team before investing in more handoff tooling.

The automated comparison exercises both implementations. The one-hour setup and two-minute review targets remain unmeasured until an unfamiliar engineer and a separate reviewer complete this scorecard.
