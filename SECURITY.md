# Security policy

Report suspected vulnerabilities privately using [GitHub's vulnerability reporting form](https://github.com/davidahma/setrya/security/advisories/new). Include the affected Setrya version, what you observed, and a minimal synthetic reproduction. Keep credentials, customer queries, passages, and private system details out of public issues.

The current 0.6 release series receives fixes. Setrya loads adapter code from the local Python environment, so review the drill and its dependencies before executing it. Use the installed `setrya replay` command or `python -I -m setrya replay` to inspect downloaded evidence. The installed console adds the working checkout to Python’s import path only for live adapter checks.

## Tenant offboarding evidence

Use the installed `setrya offboarding gate` or `setrya offboarding verify-receipt` command to review portable offboarding evidence. Treat request digests and job IDs in the original case ticket as the reference. Keep customer receipts, provider documents and backup schedules in the authorized case system. Only the bundled synthetic example is intended for public CI artifacts.

Setrya checks the files, request binding, and internal consistency. Someone who controls the observer or job log could fabricate the recorded events. Review the identity and permissions of each evidence producer before relying on its readings. A passing technical result covers the declared reads and checkpoints. Provider statements identify their issuer, and scheduled retention stays an open item. [Receipt guide](docs/offboarding.md#review-one-deletion-request)
