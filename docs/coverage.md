# Which changed read paths have evidence?

A team adds an agent-summary endpoint to the module that already serves support search. The existing refund regression passes on both commits. The new endpoint has a different caller, and nobody has tested it. Use a coverage map to put its name and owner in the PR.

Commit `setrya-coverage.toml` in the application repository. This example discovers Python route registrations and maps a background job by filename.

```toml
version = 1

[[component]]
name = "support-retrieval"
root = "app/routes"
owner = "@search-platform"
decorators = ["router.get"]

[[component.binding]]
file = "app/routes/search.py"
surface = "router.get /support/search"
drill = "tests/refund.toml"
probe = "refund-window"

[[component]]
name = "index-jobs"
root = "app/jobs"
owner = "@indexing"

[[component.binding]]
file = "app/jobs/refund.py"
drill = "tests/refund.toml"
probe = "refund-window"
test_id = "refund-update"
```

For a recorder binding, use the exact test ID supplied to the recorder. For an adapter-driven check, omit `test_id`; the drill identifies the adapter. To require a worker schedule, replace `test_id` with `scenario = "revoke"` and `schedule = "reverse"`, and point `drill` to the same configured scenario TOML used to collect the evidence. Use `schedule = "all"` to require the entire suite.

Run this after collecting candidate evidence. Both Git refs must be available locally, with sufficient history to find their common ancestor. Give the command each search report or scenario directory with a separate `--evidence`.

```sh
setrya coverage --repo . --base origin/main --candidate HEAD \
  --evidence refund-review/candidate.json --summary coverage.md
```

The command reads the map, drills, and source at the candidate commit. It checks the diff from the merge base. A report must replay and match the commit, drill rules, query fingerprints, and adapter or recorder identity. The mapped probe supplies the caller and route in the summary. The search report format omits adapter options. For an options-sensitive test, retain the `run-pr` handoff, which records the selected drill digest and checks the full live drill in each lane. A scenario also has to match the configured application and requested schedule. A matching failure takes precedence over a passing report. Keep both the native test and coverage exit codes as CI gates. A successful coverage check cannot clear a failed test.

| Exit | Meaning |
| --- | --- |
| 0 | Every affected mapped surface passed, or the diff touched no declared surfaces. The summary distinguishes these cases. |
| 1 | A matching report shows a failure. |
| 2 | The map, source, or evidence could not be evaluated. |
| 3 | A changed surface lacks a binding or matching evidence, was removed, or the matching evidence is NOT TESTED. |

Decorator mode recognizes the configured Python decorator names with literal paths or job names. It ignores formatting and comments, checks a changed handler, and rechecks registrations in the same module when shared code changes. Changes to registration order can affect later handlers. Use a file-level component for dynamic registration and remove that decorator from the overlapping decorator component. File-level components can also cover shared helpers in other modules, non-Python code, or other files whose changes need review. In file-level mode every changed file under the root needs a binding or an owner decision. Removing a route or narrowing its component in the same PR still brings the changed surface to its previous owner's attention.

This is an inventory the team maintains. Owners choose the relationship between a file or registration and a drill. The command does not trace calls across modules or discover undeclared components. Review map changes alongside the code.

## Replay the example

Unpack the [coverage review bundle](https://github.com/davidahma/setrya/releases/download/v0.11.0/route-coverage-review.zip). The bundle contains a two-commit synthetic application, the ordinary test handoff and the coverage summary.

```sh
git clone -b main application.bundle fixture
setrya coverage --repo fixture --base base --candidate candidate \
  --evidence handoff/candidate.json --summary reproduced.md
```

Both native test runs pass. Coverage exits 3 and names `router.get /agent/summary` and `@search-platform`. An independent coding-agent reviewer also spotted the omission from the diff and ordinary test handoff, without seeing the map or coverage result. The check makes that finding repeatable and assigns an owner. This trial did not show a reviewer-time advantage. No human reviewer participated.
