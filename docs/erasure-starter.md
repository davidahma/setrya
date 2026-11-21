# Archive: the v2 LangGraph starter

This page describes the v0.18 legacy starter. New integrations use the [current case model](erasure-v3.md). Use an isolated environment if reproducing the historical example.

A queued job has already read Alice’s chart. Can it save that snapshot after Alice is deleted? The demo reopens the saved chat through the application’s HTTP endpoint as two members of her care team. Bob’s fictional chart must stay readable to both of them.

## Run the example

Use Python 3.11 or later. The demo runs on loopback with a SQLite source table, a SQLite-backed LangGraph checkpointer and LangGraph’s in-memory long-term store.

```sh
python -m pip install 'setrya[erasure]==0.18.0'
setrya erasure demo --example legacy --output chart-trial
setrya replay chart-trial/guarded
```

| Run | What the two readers see after the worker finishes | Receipt |
| --- | --- | --- |
| `broken` | The worker saves its old snapshot. Alice and Bob both appear when the chat reopens. | `FAIL`. Alice’s checkpoint is still present. |
| `guarded` | The worker checks Alice’s source record again and drops the captured snapshot. Both readers see only Bob. | `PASS`. Alice’s source row, checkpoint thread and memory entry are verified gone. |
| `opaque` | The guarded app still shows only Bob. The fixture also records a synthetic provider invocation. | `NOT TESTED`. One provider copy is unknown. |

Each run writes `erasure-evidence.json` and `erasure-receipt.json`. Send both files to a reviewer. Replay needs neither the app nor LangGraph. It recomputes the verdict from the recorded reads, worker attempt and per-store checks. The provider example makes no network request.

## Connect three functions

Use an isolated test environment. The drill deletes the subject’s rows, cache entries and subject-exclusive graph threads through the adapters you register.

```sh
setrya erasure init --example legacy --output app-erasure
cd app-erasure
```

Edit `test_erasure.py` and its neighboring `inventory.toml`. The unconnected starter exits 3 and names the first function to implement. Run the connected v2 starter with `setrya erasure run --starter test_erasure.py --mode late-write --output erasure-run`. V2 requires a held worker.

1. **Seed the app in `open_fixture(subject, journal)`.** Return a context manager that seeds the test subject and a separate retained record, then yields your app and the deletion adapters. Both test callers need access to both records at the start. Bind the subject to your normal checkpoint and long-term memory writes. Register the same store names in the inventory.
2. **Pause your real job in `snapshot_job(app, subject, before_write)`.** Call `before_write()` after the job captures the old data and immediately before its write. Once released, let the job take its normal commit or discard path. Return after the work, queue and read-model refresh have settled. The starter runs a synchronous job on a local thread. A job in another process needs an application checkpoint and a terminal acknowledgment from that worker.
3. **Reopen the chat in `read_chat(app, subject, caller, question)`.** Authenticate as the requested caller and call the app’s normal serving route. Set `CASE` to that route, the question and your two fixture identities. Return `Read` with the observed principal, route, question and presence of each synthetic marker. A failed request cannot establish absence. Use `None` for an observation the endpoint cannot supply.

For example, a fixture with a SQLite LangGraph saver and an in-memory long-term store can record and register both writes like this. Use opaque keys and customer-chosen classification labels.

```python
from setrya.erasure.adapters import LangGraphMemoryAdapter, LangGraphSQLiteCheckpointAdapter
from setrya.erasure.hooks import CopyHooks

hooks = CopyHooks(journal, subject)
hooks.invoke_graph(graph, {"chart": synthetic_chart}, thread="handoff",
                   store="checkpoints", source="chart-17")
hooks.put_memory(memory, "summary", {"chart": synthetic_chart},
                 store="memory", source="chart-17")

adapters = {
    "checkpoints": LangGraphSQLiteCheckpointAdapter(saver),
    "memory": LangGraphMemoryAdapter(memory),
}
```

Use the subject-specific thread and namespace for subsequent reads. A checkpoint can contain an entire conversation. Shared conversations need an application-specific rewrite and verification that preserves the other participants. Include any source table, cache, provider or tool that receives a copy in the inventory. An observed but undeclared location appears as a coverage gap.

```sh
python test_erasure.py --output candidate
setrya replay candidate
```

The starter is also a pytest test. Run `python -m pytest -q -s test_erasure.py` or let your normal suite collect it. Each run gets a new evidence directory under pytest’s temporary folder and prints its replay command. Pytest reports any result other than `PASS` as a failed test. Use the standalone command above when CI needs a named directory to upload or the distinct Setrya exit codes.

The verdict covers the declared stores, observed copies and two serving sessions during this run. The receipt lists remaining unknowns. The timer starts when the drill starts, after setup and instrumentation. The one-hour integration goal requires a timed trial with a developer new to Setrya. The automated acceptance test connects the same starter to a second fictional dispatch app where an old job repopulates long-term memory.

## Sign the evidence

Cosign signs the receipt, which binds the evidence file by SHA-256. Send the signature with both JSON files. Obtain the verifier’s trusted public key separately. With Cosign 3 and an existing key pair, this works locally.

```sh
setrya erasure sign-receipt --receipt candidate/erasure-receipt.json \
  --bundle candidate/receipt.sig --key cosign.key
setrya erasure verify-signature --receipt candidate/erasure-receipt.json \
  --bundle candidate/receipt.sig --key cosign.pub
setrya replay candidate
```

For keyless signatures, provider configuration evidence, adapter boundaries and all five copy statuses, see the [erasure reference](erasure.md).
