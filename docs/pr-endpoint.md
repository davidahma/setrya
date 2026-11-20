# Show the two-user endpoint check on a pull request

Select one native test that calls `check_endpoint` once. It can be the [generated endpoint test](endpoint.md) connected to your own application. Install the same Setrya and application test dependencies outside both checkouts. Run from a repository with the base and candidate Git commits available locally.

```sh
setrya run-pr --endpoint --repo . --base origin/main --candidate HEAD \
  --output endpoint-review -- \
  python -m pytest -q tests/test_setrya_endpoint.py::test_access_revocation
```

Setrya resolves both refs to full commit IDs, opens two disposable clones, and runs the selected command in each. Each live endpoint recording binds its application commit, case, and run ID. The application and worker must load from the current clone. If the PR introduces the test, a single test file outside both clones can exercise the app at each revision. Use the same test, auth fixtures, cache policy configuration, and synthetic question on both sides. A test that reads an already installed application at a different revision can exercise the wrong code.

## Read the job summary

The first line gives the decision. Under it, the replay commands open the base and candidate bundles from the downloaded artifact. The summary shows the weakest evidence across both lanes, Git commits, test exits, and each user's actual response at the decisive phase. The response includes the authenticated caller, route, HTTP status, answer classification, synthetic source IDs, and cache hit or miss.

| Decision | What the check observed | Exit |
| --- | --- | --- |
| FIX VERIFIED | The base served the revoked user the resumed task's private cached answer. The retained user's control was visible. The same candidate case passed at the corresponding phase. | 0 |
| PASS | Both revisions passed, or the candidate passed and the base exposure lacked evidence needed to verify a late-worker fix. The summary identifies which occurred. | 0 |
| FAIL | The candidate recorded a violation, such as serving protected content to the revoked caller. | 1 |
| ERROR | A test could not run, produced invalid or ambiguous evidence, or its native exit disagreed with replay. | 2 |
| NOT TESTED | A necessary observation, matching case, commit binding, or matching phase was missing. | 3 |

For example, a verified fix can show the revoked caller receiving a private cache hit after the base worker commits, then a public-only answer after the candidate rejects the late fill. The retained caller receives the private cache hit on both revisions. A private response with an unknown writer can establish exposure while leaving the base worker's involvement unresolved.

Unzip the `endpoint-review` artifact, open `review.md`, and run its commands from that directory.

```sh
setrya replay base
setrya replay candidate
```

The two directories contain the redacted endpoint manifests and search journals. The `Review again` command in the summary rechecks both complete bundles, commit IDs, and captured native exits. If the command times out after the endpoint check finishes, the job reports `ERROR` and retains the completed bundle for individual replay. Replay uses the installed Setrya core and runs offline. `run-pr.json` records each manifest's SHA-256. An older endpoint bundle without a recorded Git revision still replays individually. A commit-bound PR review requires a fresh run of both revisions.

## Wire the check into GitHub Actions

Add this job to a workflow that runs on `pull_request`. Replace the dependency installation and test selector with your project's commands. Select a read-only, disposable test environment with synthetic identities and data. For a new test held outside both clones, load the application from the working directory.

```yaml
permissions:
  contents: read

jobs:
  check-access:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0
        with:
          python-version: '3.11'
      - run: python -m pip install 'setrya[pytest]>=0.15' -r requirements-test.txt
      - name: Test base and candidate
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          setrya run-pr --endpoint --base "$BASE_SHA" --candidate "$HEAD_SHA" \
            --output "$RUNNER_TEMP/endpoint-review" -- \
            python -m pytest -q tests/test_setrya_endpoint.py::test_access_revocation
      - name: Publish the decision
        if: ${{ !cancelled() }}
        run: cat "$RUNNER_TEMP/endpoint-review/review.md" >> "$GITHUB_STEP_SUMMARY"
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7
        if: ${{ !cancelled() }}
        with:
          name: endpoint-review
          path: ${{ runner.temp }}/endpoint-review/
          if-no-files-found: error
```

The packaged [Setrya workflow](../.github/workflows/endpoint-review.yml) exercises the repository's synthetic HTTP approvals fixture at the actual PR base and head, or the previous and new main commits after a push. Its external harness uses the installed SDK for both lanes and imports the HTTP app from each checkout. On an ordinary Setrya PR the fixture can pass twice. A passing pair reports PASS. The [recorded search workflow](recording.md#use-it-in-a-pull-request) uses `--drill` for search-journal tests.
