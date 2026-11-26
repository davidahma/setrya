# Get a deletion verdict on your first pull request

The first PR runs a synthetic fixture shipped with Setrya. Once you see a verdict and can replay its receipt, connect the same workflow to your application.

## First PR with the sample

Start in the root of a GitHub repository with Python 3.11 or later on macOS or Linux. The commands create a branch and a virtual environment, generate `setrya-case/test_erasure.py` and download the release workflow to `.github/workflows/setrya.yml`. Start with an unused starter directory and workflow path.

```sh
git switch -c try-setrya
python3 -m venv .venv-setrya
. .venv-setrya/bin/activate
python -m pip install setrya==1.18.0
setrya init --output setrya-case
setrya doctor --starter setrya-case/test_erasure.py --mode deletion
mkdir -p .github/workflows
curl -fsSL https://github.com/davidahma/setrya/releases/download/v1.18.0/setrya-quickstart.yml -o .github/workflows/setrya.yml
git add setrya-case/test_erasure.py .github/workflows/setrya.yml
git commit -m "Run the Setrya deletion check on pull requests"
git push -u origin HEAD
```

Open a PR against your default branch. The [release workflow](https://github.com/davidahma/setrya/releases/download/v1.18.0/setrya-quickstart.yml) installs `setrya==1.18.0` and runs the SHA-pinned Setrya Action in `deletion` mode. It asks as the sample reader, calls the deletion operation, and checks the store and the answers again. No worker hook is required.

In the PR’s **Checks** tab, open the **Setrya erasure** run. Expect **PASS · READY_FOR_REVIEW** and the `setrya-receipt` artifact. On a same-repository PR, each push updates one verdict comment. It links to the receipt, names any observed failure or evidence gap and identifies outstanding review items. The artifact contains the underlying store and caller observations. Fork PRs get the check summary and artifact; the comment job requires write permission and runs separately. [Example PR comment](https://github.com/davidahma/setrya/pull/38#issuecomment-7065545463)

Download and unzip `setrya-receipt`. In the directory containing `erasure-receipt.json`, run:

```sh
setrya replay .
```

## Switch from the sample to your app

Keep the workflow and generated starter. Add an application-owned fixture that opens an isolated instance of your app, then change the two imports in `setrya-case/test_erasure.py`.

```diff
-from setrya.examples.workspace import SIMPLE as CASE
-from setrya.examples.workspace import open_fixture
+from helpdesk_fixture import CASE, open_fixture
```

The [working adapter](examples/first-pr/helpdesk_fixture.py) connects a [Flask help desk](examples/first-pr/helpdesk.py) to the starter. Its case declares a duty agent, the `/desk/answer` route, a customer note to delete and a team note to preserve. Its `open_fixture()` seeds a fresh SQLite database and yields `(subject, application, external_items)`. The application object supplies three methods:

| Method | What the help-desk fixture does |
| --- | --- |
| `inspect(subject, store, key)` | Reads the SQLite row at the declared key and records its revision and which facts it contains. A missing row is an observed absence. |
| `read(subject, caller, conversation, question)` | Uses the duty agent’s test session to call the Flask answer route. Returns the caller, route, conversation, question and text from the response. |
| `erase(subject)` | Calls the application's customer-deletion route using the test owner's session. |

To try the exact switch in the sample PR, save the linked files as `helpdesk.py` and `helpdesk_fixture.py` in the repository root, make the import edit above, and install `'Flask>=3.1,<4'` locally. Add the same dependency to the **drill** job’s Python install step. For your service, use its normal test dependencies and fixtures. Use disposable data and review receipt contents before sharing them.

```sh
setrya doctor --starter setrya-case/test_erasure.py --mode deletion
setrya run --starter setrya-case/test_erasure.py --mode deletion --output desk-receipt
setrya replay desk-receipt
```

Commit the application fixture, the starter edit and the job’s dependency change. Push to the same PR. In this example the duty agent can see depot code `Harbor-17` before deletion, then loses it while still getting `Thursday` for dispatch. If the deletion route removes the row but leaves the old answer cached, the drill returns **FAIL · RESIDUAL_FOUND**. [Follow the sample, application failure and fix on one PR](https://github.com/davidahma/setrya/pull/38)

## Test a stale write-back

The first PR establishes what happens after ordinary deletion. Connect the deferred writer's hooks and use `mode: write-back`. Setrya holds the writer after it reads the source, runs the deletion, then releases it and checks again. A missing checkpoint yields **NOT TESTED**. A FAIL with a witnessed stale write-back opens with that finding in the PR comment. [Worker and observation contract](erasure-v3.md#required-evidence) · [Fix a write-back](fix-write-back.md)

The Action returns 0 for PASS, 1 for FAIL, 2 for ERROR and 3 for NOT TESTED. The workflow keeps verified receipts on failure. To include outstanding provider or retention obligations in the release decision, use `setrya replay . --case-review`. Both jobs pin the Action by its full commit SHA. The run rejects a mismatched installed Setrya version before opening the fixture. For GitHub Enterprise Server, use a runner, checkout and artifact uploader supported by your server version. The comment uses the runner’s GitHub API and web URLs. Uploaders without an artifact ID still give reviewers the workflow run link. [GitHub artifact compatibility](https://github.com/actions/upload-artifact#ghes-support)

If the setup or installation step fails before the fixture starts, the same-repository workflow updates the existing comment to **ERROR · UNAVAILABLE**. The workflow run links to the failing step.

The downloaded workflow uses the exact commit behind the release tag in both jobs. The release build checks the generated file against that commit before publishing the package. The repository keeps the source workflow under `docs/examples/first-pr/`.

## Start from a TypeScript app

Use the [copy coverage workflow](copy-coverage.md#run-it-in-a-pull-request) with `copy-language: typescript` and the Python `typescript` extra to flag new copy sites before wiring a drill. The [TypeScript starter](typescript-starter.md) runs the HTTP deletion drill against a Node and SQLite app with no npm dependencies. Run that case from a workflow step and upload its receipt for replay.
