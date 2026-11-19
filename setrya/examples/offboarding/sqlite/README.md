# Observe a ticket and its cached answer

The synthetic support app keeps a ticket and a cached answer for each tenant. Its offboarding job deletes the departing tenant's rows. The observer opens separate read-only connections, checks the departing and retained tenants before and after the job, and writes the results to JSON. The policy and observations contain opaque IDs for the reads, run, and revision.

From a Python environment with Setrya installed, choose a fresh directory for the files.

```sh
setrya offboarding sqlite-policy > policy.json
setrya offboarding observe-sqlite --case residual --revision build-42 --run-id ci-17 --output observations.json
setrya offboarding gate --policy policy.json --observations observations.json --expect-run ci-17 --expect-revision build-42
```

| Observer case | What happened | Gate and exit |
| --- | --- | --- |
| `clean` | The deletion committed and the retained tenant remained readable. | PASS, 0 |
| `residual` | The job skipped the cache. The observer found the departed tenant’s answer. | FAIL, 1 |
| `unavailable` | The cache reader lost its connection before the final read. | NOT TESTED, 3 |

An observation run exits 0 when it collected the packet. Use the gate's exit code for the release decision. By default the observer generates a run ID and records its source digest as the revision. The [observer](observe.py) also runs as a standalone Python script with the standard library.

## Observe a request-specific job

Use the same policy and a fresh request. Save the digest printed by `request` in the original ticket before running the job. In this example, set `TICKET_DIGEST` from that ticket when reviewing the result.

```sh
setrya offboarding request --policy policy.json --context staging --revision build-42 --run-id case-17 --output request.json
setrya offboarding observe-sqlite --case clean --revision build-42 --request request.json --output evidence.json
setrya offboarding receipt --request request.json --expect-request "$TICKET_DIGEST" --policy policy.json --evidence evidence.json --output receipt.json
setrya offboarding verify-receipt --request request.json --expect-request "$TICKET_DIGEST" --policy policy.json --evidence evidence.json --receipt receipt.json
```

The application assigns tenant keys to the request before dispatch. It checks the subject and commits both the deletion and the job record in the same transaction. A separate reader retrieves the job ID, request digest and timestamp from that committed record. The staging example accepts requests for `sqlite-support` and the revision supplied at invocation. The issue, gate, and receipt commands check the evidence available for the declared reads and exhibits. [Offboarding guide](../../../../docs/offboarding.md)
