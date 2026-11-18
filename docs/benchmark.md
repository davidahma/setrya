# Compare Setrya with plain pytest

Both tools can catch the bundled defects. This comparison measures the work of getting to that result, handing it off, and making the next change. The fixtures cover a late refund overwrite and a delete-and-reupload that removes a good passage from the top three.

## Run the synthetic comparison

Start the [disposable pgvector database](pgvector.md), then run this from the same environment.

```sh
python3 -m examples.pgvector_search.benchmark --repeats 3 --output /tmp/setrya-benchmark
```

The output directory contains machine timings, an environment record, the plain pytest logs, the Setrya JSON reports, and offline comparisons. Each implementation uses the same HTTP search endpoints, caller, queries, depths, database, and worker hooks. Each trial gets a fresh fixture schema. Trial order alternates between Setrya and pytest. Both implementations have to detect the broken cases and pass the guarded cases.

Fixture-to-checkpoint time begins before starting the HTTP app and ends at its worker acknowledgment. Whole-command time includes interpreter startup, the check, and cleanup. The comparison time is the wall time for a separate process reading both reports after their fixtures close. These are machine timings on a warm database. See the **pgvector-benchmark** artifact on the [CI runs](https://github.com/davidahma/setrya/actions/workflows/ci.yml) for the recorded results.

## Measure an unaided handoff

Give engineers who have not seen the adapter the same disposable app, fixture instructions, hooks, and access to a coding agent. Alternate which tool they use first. Record active minutes and any help received. Use the first indexing change for setup and diagnosis, then give them the re-upload change and record what they edit and rerun. Give each result to a reviewer who did not write that check.

| Measure | Start and finish | Proposed target |
| --- | --- | --- |
| Active setup | Start from a clean checkout. Stop when broken and guarded runs produce the expected diagnosis. Record any time spent waiting on prerequisites separately. | The adopter connects the second app in one hour. |
| Reviewer diagnosis | Start when the reviewer gets the exact commit and artifacts. Stop when they independently name the affected query and decisive observation. Record wrong conclusions and requests for help. | Two minutes. |
| Reuse | Start when the engineer gets the second indexing change. Stop when an updated check catches the missing top-three passage and passes the guarded worker. Record active time, changed files, and what the reviewer has to ask for. | Compare with the same measures for pytest. |

Use a timer and retain the commit, verdict, and artifact with each observation. For Setrya, hand over the two saved reports. For pytest, hand over the ordinary failure output and rerun instructions. Ask both reviewers to explain the case with missing write evidence in a separate exercise.

Compare diagnosis and handoff quality alongside active time. If the plain test is equally usable, keep the recipes and reassess further tooling. A team using the check on a later release is the adoption signal. Across later releases, two teams reusing the check and an engineering owner willing to fund ongoing maintenance would justify testing the paid support hypothesis. Backfill cutover and permission changes are candidates when those teams can point to a recurring release problem. Human setup, reviewer timing, reuse, and willingness to pay still need observation.
