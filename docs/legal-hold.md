# Check deletion when a record is on hold

A deletion request covers a disposable customer note. The application's policy keeps an incident packet under hold. The same register consolidates its records and republishes the summary used to answer questions. Setrya checks that the note stays erased and that the exact held packet survives.

```sh
python -m pip install setrya
setrya demo legal-hold --output ./legal-hold
setrya replay ./legal-hold/guarded
setrya receipt summary ./legal-hold/guarded --audience reviewer
```

The demo runs a disposable SQLite application with two source rows and two materialized views. The serving path reads the stored summary. It answers separate questions about the note and the held packet. The fixture fingerprints the packet's contents and hold marker as its revision.

| Result | What the receipt shows |
| --- | --- |
| `guarded` · PASS | The note stays absent. The held packet has the same key and revision after deletion, after consolidation, after summarization, and in the final read. |
| `lost-hold` · FAIL | The delete operation removes the held packet. |
| `restored-hold` · FAIL | Consolidation drops the held packet and summarization restores it. The interim read still records a failure. |
| `resurfaced-target` · FAIL | Summarization republishes a stale copy of the note. The source remains absent and the held packet remains intact. |

For each step, `maintenance_checks` identifies the operation, the exact key, the artifact's rule, the observed outcome, and a pointer into `erasure-evidence.json`. Reviewers can follow separate lines for the note's absence and the held packet's unchanged revision. The failed lane exits 1 when replayed. A missing required read or operation acknowledgment prevents PASS.

## Connect the case to your application

Set `maintenance_steps=("consolidate", "summarize")` on a `SharedCase`. Declare the record to remove as `delete`, the held record as `preserve`, and derived records that must lose the erased fact as `rewrite`. Use a revision that covers the content and metadata you require the hold to preserve. Return `Maintenance(step)` from `maintain(subject, step)` after the selected operation commits and the direct reads can see it.

```python
from setrya.erasure import Maintenance

def maintain(self, subject, step):
    if step == "consolidate":
        self.consolidate(subject)
    elif step == "summarize":
        self.publish_summary(subject)
    else:
        raise ValueError("Unknown maintenance step.")
    return Maintenance(step)
```

Setrya reads the declared artifacts after each acknowledged operation. The runner performs its normal endpoint reads, scans, and direct observations immediately after erasure and in the final stage. A case can declare at most eight steps and 256 direct maintenance reads. The [SQLite fixture](../setrya/examples/legal_hold.py) shows the entire workflow.

Use a disposable test subject and let the application own its retention decision. A preservation rule expects a distinct held record whose contents exclude the fact the case requires erased. The result covers the exact records and the completed checkpoints in the receipt. Authorization, retention schedules, and legal sufficiency remain with the organization reviewing the case.

## Share the result

Pack the guarded demo into a signed receipt exchange. The receiving reviewer provisions the issuer's trusted public key separately.

```sh
setrya receipt pack ./legal-hold/guarded --synthetic \
  --system incident-register --system-version 1 --key ./issuer.key --output ./hold-exchange
setrya receipt verify-exchange ./hold-exchange --key ./trusted-issuer.pub
setrya receipt summary ./hold-exchange --audience reviewer
```

The receipt and companion evidence preserve the maintenance observations. Replay recomputes the verdict from those observations. Use Setrya 1.16 or later to replay a case that declares maintenance steps. Cases without maintenance steps keep their existing evidence fields. An OSCAL export using `artifact-preserve` can link the direct maintenance reads for the held record. [Receipt exchange](receipt-exchange.md) · [OSCAL export](oscal.md)
