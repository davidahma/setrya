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

## Connect a disposable test application

Implement `ScenarioApplication` and `Delivery` in your adapter package. The factory receives a `setrya.adapter.RunContext` containing the run ID, temporary directory and drill. The factory must give every delivery access to the same application's state. The application performs the following operations.

1. `seed()` prepares the subject and retained control. `delivery(message_id, attempt)` returns a handle for a physical delivery. Multiple handles may refer to the same durable message.
2. `start_old()` dispatches a worker and returns its attempt ID. `checkpoint()` returns a `Checkpoint` when the worker has really captured the old input and is held before its write. It may return `None` while waiting. `message_id()` reports the message actually consumed. On the retry schedule, `retryable_failure()` stops the held attempt before commit, makes its message available for redelivery, and returns its rejected `Receipt`.
3. `publish()` performs the real update, tenant deletion or access mutation once and returns a `Publication` naming the run, document and new revision. Setrya searches before releasing any worker. Each handle's `search(probe)` invokes the real read route as the declared caller, honors the question and depth, and returns a `SearchResult` with the actual route and caller. For a failed read, return an unidentified result or raise an exception. An empty list represents a successful read.
4. `release()` resumes only that handle’s worker. `receipt()` polls for its terminal write result. Setrya samples the read path again. `close()` stops outstanding workers and removes the disposable state, including after an error.

For your own scenario TOML, use Setrya’s drill format. For a deletion or revocation, the primary probe sets `absence = true`, `require = []`, and lists the original passage markers in both `before` and `forbid`. A separate probe must name a passage retained before and after the change on the same route and caller. For revocation, add a probe for the same former passage on that route as a still-authorized caller. Set the new revision to your identifiable deletion or ACL revision. Acknowledgment of the mutation and successful search evidence are both required.

```sh
setrya scenario revoke --drill permission.toml --adapter myapp.drills:factory \
  --schedule reverse --revision "$GIT_COMMIT" --output permissions-ci
setrya scenario replay permissions-ci
```

In Python, call `run_scenario("revoke", "reverse", output, drill=drill, factory=factory, revision=commit)` from `setrya.scenarios` and inspect `verdict.status`. The function creates a fresh output directory and returns after cleanup and replay. The bundled adapter is a complete runnable implementation. [SQLite application and worker](../setrya/scenario_fixture.py)

## Hand the evidence to a reviewer

Keep the whole output directory together. Each delivery has an ordinary Setrya search report. The v1 `scenario.json` manifest binds its message IDs, physical attempts, configuration fingerprint and SHA-256 of each delivery report’s exact published bytes. Suite manifests also bind the exact child manifests. These local hashes detect accidental file substitution. Use your CI artifact provenance to authenticate the producer.

Replay recomputes each delivery verdict, checks message identity and worker ordering, then combines them. An intact delivery showing a forbidden passage establishes FAIL even if another delivery or suite member is missing. Without a confirmed failure, damaged inputs and execution errors yield ERROR, missing runtime evidence yields NOT TESTED, and only a fully verified schedule passes. A passing suite keeps the weakest visibility tier used by any delivery. If a probe relied on a refresh acknowledgment, live output and offline replay both say that write visibility is unverified.
