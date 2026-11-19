# Test a change while old work is in flight

An indexing worker can capture a tenant’s support passage before offboarding and write it back after deletion. A permissions change has a second failure mode. The passage should disappear for the former support agent while a caller who retains access should still retrieve it. Setrya drives both changes through the same checkpoint, publication, search, release and receipt checks as a document update.

## Run the SQLite application

Run these from a Setrya installation with fresh output directories.

```sh
setrya scenario delete --mode broken --schedule reverse --output deleted-broken
setrya replay deleted-broken
setrya offboarding drill --schedule all --output deleted-fixed
setrya scenario revoke --schedule all --output permissions
setrya scenario update --schedule duplicate --output document
```

The first two commands exit 1. Both workers have already read the old input when Setrya publishes the deletion. Releasing the second worker first restores the tenant’s passage. Its rank and caller are recorded in the delivery report. In fixed mode, the transaction checks the current epoch before writing. The stale delivery receives a rejection. The fixture observes the search projection through a separate read-only SQLite connection.

For deletion, the primary probe must see the departing tenant’s passage before the change and an identified result with no forbidden marker after it. The same route and caller must still return the retained control. For revocation, a second authorized caller must also retain the target passage. An unavailable route or an unidentified hit cannot establish absence. These checks cover the top-k results and callers declared in the drill. A driven CI drill writes search evidence. A deletion receipt belongs to a separately issued case request. [Receipt workflow](offboarding.md)

| Schedule | Held deliveries and release order |
| --- | --- |
| `single` | One worker captures the old message and resumes after the change. |
| `retry` | The adapter fails the first delivery at its pre-write hook and acknowledges rejection. Setrya requests redelivery of the same durable message under a new attempt ID. The retry captures old input before the change and resumes afterward. |
| `duplicate` | Two physical deliveries of the same message capture old input concurrently, then resume in delivery order. |
| `forward` | Two different messages for the same document capture old input and resume in delivery order. |
| `reverse` | The same pair resumes in reverse delivery order. |
| `all` | Each schedule runs against a fresh instance. |

These schedules deliberately exercise particular orderings. A PASS records the observed runs. It does not establish every possible ordering. The retry schedule covers failure before publication and a delayed retry commit after publication.

With the bundled SQLite adapter, an explicit `--mode fixed` or `--mode broken` overrides the TOML worker mode for that run. Omitting the flag preserves the configured mode. For other adapters, configure the worker in the drill’s options table.

The SQLite fixture captures `subject@A` at `before-write`. Its three operations publish `updated`, `deleted` and `revoked`, respectively. A custom drill must match the observed identities to pass. Versions through 0.9.0 copied those labels from the drill into their reports. Recollect custom SQLite fixture runs that used different labels to verify the actual target.

## Connect a disposable test application

Implement `ScenarioApplication` and `Delivery` in your adapter package. The factory receives a `setrya.adapter.RunContext` containing the run ID, temporary directory, drill and `scenario_kind` (`update`, `delete` or `revoke`). The factory must give every delivery access to the same application's state. The application performs the following operations.

1. `seed()` prepares the subject and retained control. `delivery(message_id, attempt)` returns a handle for a physical delivery. Multiple handles may refer to the same durable message.
2. `start_old()` dispatches a worker and returns its attempt ID. `checkpoint()` returns a `Checkpoint` when the worker has really captured the old input and is held before its write. It may return `None` while waiting. `message_id()` reports the message actually consumed. On the retry schedule, `retryable_failure()` stops the held attempt before commit, makes its message available for redelivery, and returns its rejected `Receipt`.
3. `publish()` performs the real update, tenant deletion or access mutation once and returns a `Publication` naming the run, document and new revision. Setrya searches before releasing any worker. Each handle's `search(probe)` invokes the real read route as the declared caller, honors the question and depth, and returns a `SearchResult` with the actual route and caller. For a failed read, return an unidentified result or raise an exception. An empty list represents a successful read.
4. `release()` resumes only that handle’s worker. `receipt()` polls for its terminal write result. Setrya samples the read path again. `close()` stops outstanding workers and removes the disposable state, including after an error.

For your own scenario TOML, use Setrya’s drill format. For a deletion or revocation, the primary probe sets `absence = true`, `require = []`, and lists the original passage markers in both `before` and `forbid`. A separate probe must name a passage retained before and after the change on the same route and caller. For revocation, every marker in the revoked caller’s baseline needs a positive control on that route. Record it in both `before` and `require` for a still-authorized caller. Multiple controls may cover different markers. Set the new revision to your identifiable deletion or ACL revision. Acknowledgment of the mutation and successful search evidence are both required.

```sh
setrya scenario revoke --drill permission.toml --adapter myapp.drills:factory \
  --schedule reverse --revision "$GIT_COMMIT" --output permissions-ci
setrya scenario replay permissions-ci
```

In Python, call `run_scenario("revoke", "reverse", output, drill=drill, factory=factory, revision=commit)` from `setrya.scenarios` and inspect `verdict.status`. The function creates a fresh output directory and returns after cleanup and replay. For cross-commit review, configure an importable adapter entry point in the drill or pass a module-level factory function or class. Injected closures, partials and callable instances can run and replay with a run-specific identity. The bundled adapter is a complete runnable implementation. [SQLite application and worker](../setrya/scenario_fixture.py)

## Hand the evidence to a reviewer

Keep the whole output directory together. Each delivery has a search report. The schedule manifest binds the reports’ SHA-256 digests, message IDs and physical attempts. Replay checks the workers against the same publication and release order. In v2, if one delivery sees the change and another delivery starts a search that returns a forbidden passage, the failure names both observations. A search already in flight when the first response arrived cannot establish that ordering.

A suite publishes its manifest before the first schedule. It adds a digest for each completed member before printing that member’s result. If CI stops the process, replay the suite directory. A bound failure still exits 1. An unfinished suite with only passing completed members exits 3. A missing or damaged *published* member produces an error unless another intact member establishes failure. A worker killed before its evidence was saved can leave its schedule unfinished.

### Compare the base and candidate in CI

To try the handoff first, unpack the [two-commit review bundle](https://github.com/davidahma/setrya/releases/download/v0.9.1/scenario-review.zip) and run the command printed in `review-command.txt` from that directory. The base and candidate contain the saved observations from two revisions of the SQLite worker.

In each checkout, use the same Setrya release, adapter entry point, options, drill, and schedule. Capture the scenario’s exit even when it fails. Supply the full commit ID for the checkout that actually runs the worker.

```sh
# Run in each application checkout. Give each lane a fresh evidence directory.
lane_exit=0
setrya scenario revoke --drill permission.toml --adapter myapp.drills:factory \
  --schedule all --revision "$(git rev-parse HEAD)" --output "$LANE_DIR" || lane_exit=$?
printf '%s\n' "$lane_exit" > "$LANE_DIR.exit"
```

Upload each entire directory and its exit file, including on failure. In the review job, obtain the expected base and candidate commits from CI. Set the paths to the two downloaded directories and run the installed verifier.

```sh
export BASE_REPORT=base CANDIDATE_REPORT=candidate
setrya review "$BASE_REPORT" "$CANDIDATE_REPORT" \
  --base-commit "$BASE_COMMIT" --candidate-commit "$CANDIDATE_COMMIT" \
  --base-exit "$(cat base.exit)" --candidate-exit "$(cat candidate.exit)" \
  --summary review.md
```

The Markdown shows the verdict for each schedule, the decisive delivery or cross-worker observation, and the weakest passing evidence tier across paired deliveries. An intact candidate FAIL can still be reported alongside incomplete coverage. A verified fix requires complete matching schedules and candidate proof for the affected delivery, probe and phase. A base failure that depends on an ordering across workers needs more evidence than two independently passing delivery reports can supply. In this case the review is inconclusive. A passing result that relies on a refresh acknowledgment says write visibility is unverified.

New scenario manifests use v2. The configuration fingerprint covers the factory, its options and the drill. Each delivery separately records the tested Git revision, which the manifest binds. Manifest digests protect against accidental substitution. Your CI checkout, artifact permissions and provenance establish who produced the evidence. Existing v1 scenario directories replay under their original rules. A v2 revocation recorded with an unrelated or partial authorized control remains readable. It now replays as NOT TESTED unless a recorded read establishes a failure. Earlier verifiers could treat deletion for all callers as a passing revocation. To compare their runs across commits, collect v2 evidence in both checkouts. Individual delivery reports continue to use the [frozen search v1 format](evidence.md#frozen-search-report-v1).
