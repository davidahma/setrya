# Security policy

Report suspected vulnerabilities privately using [GitHub's vulnerability reporting form](https://github.com/davidahma/setrya/security/advisories/new). Include the affected Setrya version, what you observed, and a minimal synthetic reproduction. Keep credentials, customer queries, passages, and private system details out of public issues.

The current 0.5 release series receives fixes. Setrya loads adapter code from the local Python environment, so review the drill and its dependencies before executing it. Use the installed `setrya replay` command or `python -I -m setrya replay` to inspect downloaded evidence. The installed console adds the working checkout to Python’s import path only for live adapter checks.
