# Release qualification

Run Python 3.11 or later with SQLite FTS5. The CI matrix runs deterministic unit tests and the quick qualification suite on each configured Python and OS combination. The offline release job records the exact Linux, Python, and SQLite configuration used for the full checkout and archive qualification. Review the [Checks](https://github.com/davidahma/setrya/actions/workflows/ci.yml) and [offline qualification](https://github.com/davidahma/setrya/actions/workflows/qualification.yml) runs for the release commit.

```sh
python3 -m unittest discover -s tests -v
python3 -m setrya qualify
python3 -m setrya qualify --case report-replay
scripts/qualify-offline.sh
```

The Linux CI report step also runs a known-broken check, uploads its saved report, and verifies that replay exits with FAIL. The unit suite exercises wrong-caller and post-release observations, mismatched Python search depth, false publication claims, and adapter errors after a proven regression.

The offline harness uses a loopback-only network namespace on Linux and a loopback-only sandbox profile on macOS. It checks loopback and attempts a connection to a reserved off-host address to verify the isolation. On Linux, run the harness as your normal user with `unshare`, `setpriv` (util-linux), and `ip` installed. CI uses non-interactive sudo to create the namespace and enable loopback. The launcher restores the invoking UID, GID and group memberships before it starts Python. The verification step checks that identity. An invocation that starts as root, including a root container, retains that identity and still needs permission to create a network namespace. Set `SETRYA_PYTHON` to an absolute Python executable to override interpreter discovery.

| Case | Expected evidence |
| --- | --- |
| `demo` | Three distinct verdicts and rerun commands in under two minutes. |
| `late-write` | B before resume, A after the stale commit, and a passing guarded worker. |
| `retired-chunks` | Find chunk five at baseline, expose it in a broken replacement, and remove it in a fixed replacement while retaining the control. |
| `early-stale` | Observe a stale read before the old worker resumes with matching attribution. |
| `missing-evidence` | Clean stale-view observations, exposure when the view advances, a missing revision identity, and a missing checkpoint. |
| `adapter-handoff` | Compare broken and fixed HTTP fixtures through the CLI and the plain regression test. Exercise the Python-function entry point. |
| `report-comparison` | Verify a fix and a regression. Reject changed questions, changed depth, older input bindings, and inconclusive write evidence. |
| `report-replay` | Recompute reports after the drill changes and app exits. Check exit codes, attribution, schema rejection, and default output redaction. |
| `isolation` | Run independent fixtures concurrently, interrupt and restart one, and preserve the other run's results. |
| `offboarding` | Reproduce a restored tenant summary, a guarded worker, and a missing baseline. Replay their CI gates and verify a separate request-bound receipt with a technical PASS and an outstanding backup item. |
| `offboarding-compatibility` | Generate and validate a reference report, gate all three SQLite observer cases, and verify and replay the historical OpenDesk report offline. |
| `repeatability` | 30 fresh-process samples each of the broken, guarded, and unobservable fixtures. A retry does not erase a failed first attempt. |

**Scope of evidence.** The fixture comparison uses one shared worker checkpoint and the same six HTTP controls for seeding, start, acknowledgment, publication, release, and receipt. Both consumers exercise the same source contents, questions, depth, and expectations. Setrya adds the TOML rule mapping and reusable adapter. The plain test keeps orchestration and assertions in one Python file. Both diagnose the deliberately broken refund fixture. Setrya also saves an independently replayable event history, which the plain test currently reports as assertion output. Unaided setup time, the two-minute newcomer interpretation target, third-party compatibility, and adoption through a team's later release are awaiting observation.

For a handoff trial, give an engineer and a reviewer separate copies of the same disposable drill and one broken update. Record active setup time. After the app exits, give the reviewer the saved report, the commit, and the drill path. Record time to the first correct diagnosis, whether they can explain a missing-evidence verdict, and whether they rerun the scenario without help. Use the same fixture with the plain test as a comparison. A later release decision using the drill provides a separate adoption observation.

The optional `python3 -m setrya qualify --case pgvector` runs six end-to-end checks against PostgreSQL. It verifies the clean but incomplete top-three response, the broader retrieval that still finds the third passage, and the guarded run's one-result delivery control. CI runs this case and the paired benchmark from an unpacked archive. [PostgreSQL setup](pgvector.md) · [Paired benchmark](benchmark.md)

The recorded pytest CI job compares the same test on two actual Git checkouts of the bundled fixture. It requires a failing pytest base, a passing candidate, and a verified fix in the saved evidence. It also runs the same pytest test with an opaque search view. Pytest must fail and the report must replay as NOT TESTED, even though the native passage assertions saw the expected policy. The [Dify case study](dify-regression.md) records the result of a separate upstream test on the pinned base and fixed commits. Its observable boundary is the keyword index.

For a release, keep the archive validated by CI, checksum, OS/Python/SQLite version, and qualification logs. Link the exact workflow run from release notes.

The [portable build](../.github/workflows/offboarding-portable.yml) builds the single-file CLI twice and compares the bytes. CI scans the examples, replays the exact files, and attests them on main. The [live OpenDesk trial](../.github/workflows/offboarding-trial.yml) runs the pinned application with Docker, compares the offline verdict, and publishes attested evidence. The [image audit](../.github/workflows/offboarding-trial-images.yml) checks the pinned trial dependencies on Linux amd64 and arm64.
