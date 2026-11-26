# Release schedule

Setrya batches routine changes into one scheduled release every two weeks. Individual PRs can merge as they pass CI. The package, Action, release notes, and quickstart pins update together at the release. A fix that corrects a misleading verdict or addresses a security issue can ship earlier. Its release notes record why. Each release checks the exact merge commit and exercises a clean PyPI install. [Release notes](../CHANGELOG.md)

Releases 1.9 through 1.17 shipped more frequently during development. The 1.18 correction batches the scan, verification, and handoff fixes. The next regular release window opens December 10, 2026. Update to the latest release for ordinary use. For a paired vendor run, follow the recorded fixture setup and backend versions. [Vendor rerun compatibility](vendor-conformance.md#review-who-ran-the-test)

Setrya 2.0 is targeted for February 2, 2027. It is the planned removal point for deprecated 1.x CLI commands and their top-level discoverability. The list is maintained with their migration paths. Teams that still run those commands can pin a 1.x release during migration. Open a [compatibility issue](https://github.com/davidahma/setrya/issues) for a workflow without an equivalent. [Legacy migration guide](other-drills.md) · [API stability](api-stability.md)

Setrya 1.x accepts `--mode write-back` and `--mode late-write` for the same drill. The CLI rename to `write-back` is planned for 2.0. Archived receipts retain their recorded mode and replay rules. [CLI modes](erasure-cli.md)
