### Setrya · two-user endpoint review

```text
FIX VERIFIED  The resumed worker exposed the private answer on base. Both candidate callers passed.
Replay base  setrya replay base
Replay candidate  setrya replay candidate
Weakest evidence · write-confirmed
Base commit 10a803ed09238b8923706373f8f2954ef3afc552 · test exit 1
FAIL · At event 11 the revoked user received the private answer from the resumed task's cache write (job-73).
  revoked reviewer · final event 11 · served as reviewer on approval-assistant · HTTP 200 · private-answer · sources limits@2, handbook@5 · cache hit
  retained owner · final event 12 · served as owner on approval-assistant · HTTP 200 · private-answer · sources limits@2, handbook@5 · cache hit
Candidate commit fa2c247737e977f2412e387cc685d9b089434f0f · test exit 0
PASS · The worker rejected the late fill. The revoked user received the public answer and the retained user received a private cache hit.
  revoked reviewer · final event 11 · served as reviewer on approval-assistant · HTTP 200 · public-only · sources handbook@5 · cache miss
  retained owner · final event 12 · served as owner on approval-assistant · HTTP 200 · private-answer · sources limits@2, handbook@5 · cache hit
Review again  setrya review base candidate --base-commit 10a803ed09238b8923706373f8f2954ef3afc552 --candidate-commit fa2c247737e977f2412e387cc685d9b089434f0f --base-exit 1 --candidate-exit 0 --exit-contract setrya
```
