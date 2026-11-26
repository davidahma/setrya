# Changelog

## 1.15.0

- Export OSCAL 1.2.0 Assessment Results from a signed and replayed receipt exchange. Record the evidence, selected Assessment Plan, mapping version, signer, verdict and open items for governance review.

## 1.13.0

- Compare separately signed vendor runs by verdict and surface. A disagreement requires review before accepting the newer result. Check the fixture version before starting a linked rerun.
- Pack vendor results as signed attestations for offline verification and governance handoff. Ticket records identify the verifying key and UTC verification time.
- Run local conformance fixtures for CrewAI memory, Cognee ingestion, and SQLite session history in Google ADK and OpenAI Agents. The workflow verifies the signed results without network access.

## 1.12.0

- Record who operated a conformance fixture. Review the signer and replayed evidence separately, or verify a linked rerun under a separate signer policy.
- Walk through attaching a signed deletion receipt, its control reference, and open items to a governance ticket.

## 1.11.0

- Explain stale write-back in the README, root Action description, troubleshooting guide, and glossary.
- Recognize supported deferred writes in Python copy scans and suggest a write-back drill in PR review.
- Label a failure as stale write-back when the receipt ties the returned copy to a deferred writer's captured input and recorded write.
- Test original Celery input delivered by a retry or resumed consumer across deletion.
- Accept `--mode write-back` in the CLI. The existing `late-write` spelling and evidence stay compatible throughout 1.x.
- Replace a prior copy scan comment when the latest scan fails. Bind YAML run provenance to the bytes parsed for the case.
- Send release comparisons back for review when a retained-control probe or pinned HTTP behavior changes.

## 1.10.0

- Reuse HTTP drill behavior across deployments. Capture the endpoint's TLS certificate fingerprint and application-reported build, with an independent deployment pin for review.
- Compare two signed client assessments to show gained and lost observations, residuals, build changes, and open items. Attach the signed approval and customer summaries to an existing review.
- Give the draft receipt exchange independent verification in Python and the browser, with shared error codes and an offline keyless verifier.

## 1.9.0

- Capture v3.2 tool, test file, Git checkout and CI provenance for deletion drills and allow independent replay pins.
- Scan supported Python copy sites before writing a fixture. Report new PR coverage gaps with file, line and starter mapping.
- Use a shorter first-run guide and a two-week release cadence. Setrya 2.0 is targeted for February 2, 2027.
- Capture direct provider API baselines and readbacks, and replay the recorded results separately from unmeasured retention.
- Sign a multi-repository assessment and verify detached-key signatures in an offline browser reviewer.

## 1.8.0

- Pack a v3 person receipt and its original evidence into a signed draft v1 exchange with `setrya receipt pack`. A reviewer can authenticate the issuer, pin the completed ticket and replay the evidence offline with `setrya receipt verify-exchange`.
- Run the completed-ticket handoff against disposable PostgreSQL. The fixture captures journalled note and summary keys with a dedicated read-only observer. The release includes signed results for verified absence, a residual summary, a missing visibility control and a synthetic provider follow-up. The exchange checks that the second journal snapshot follows the first completed observation.

## 1.7.0

- Run `setrya discovery source-scan` to locate supported PDF writes in Python without running the application. Static relative destinations are recorded when known, and dynamic destinations are marked unresolved.
- Run `setrya discovery source-check` against a trusted baseline to flag new writes without a reviewed store and key. Optional replay links each approved site to the runtime baseline and deletion observations from the matching case.
- Run `setrya demo northline-source` to see an archive PDF move from a source coverage gap to a reproducible late-write failure. The guarded case passes with a provider follow-up open.
- Keep the read-only PostgreSQL observer’s eligibility check and reads on the same locked relation.

## 1.6.0

- Run `setrya demo northline` to check a shared security finding, saved chart and rendered PDF after selective erasure. The fixture uses a held refresh worker and authenticated reads of the same investigation.
- Run `setrya discovery init --example northline` to try the Macie handoff with separately pinned finding and application revisions. The signed release bundles replay the late-write failure and the guarded result with the unobserved provider open.

- Recheck known keys and scan the subject within one read-only store snapshot before closing an obligation. Discovery summaries show whether the provider was observed and which recorded worker restored a copy.

## 1.5.0

- Count scanner findings in customer and reviewer summaries and identify residuals and lost control data separately. Preserve file paths from dangling Git commits and detached trees when the objects survive.
- Record store-level forks and restore lineage. Replay branch residuals and recheck disclosed Postgres restore windows.
- Run signed vendor self-checks for pgvector, LangGraph Store and Mem0. Reports show the observed store, index and history surfaces and the gaps against Setrya Verified criteria.
- Review bundles through a local MCP server and import copy observations from OTLP/JSON using the published copy-event schema.
- Verify AWS KMS key destruction against a captured ciphertext inventory. Crypto-erasure is recorded separately from physical absence.

## 1.4.0

- Check the complete local Git object store, including unreachable loose and packed blobs. Configured remote repositories become open follow-ups. File and Git scan budgets are configurable, and receipts record the effective boundary. Doctor identifies common in-scope file formats omitted by the include patterns.
- Attribute a confirmed late residual to a recorded agent turn when the copy and revision agree. OpenAI Agents and Claude Agent SDK hooks supply turn context. Summaries show the scope, the sampled control results and the evidence digest.
- Local key signing preserves detached receipts with Cosign 3.
- The download for the first-PR workflow pins the exact released commit. The published Python extras cap SDK minors and CI qualifies candidates beyond those caps.

## 1.3.0

- Trace copy events to agent turns and opt in to source-revision lineage. The SQLite journal records a chain head, and merged worker packets retain one per producer. Run `setrya journal verify` to detect changed or missing records.
- Run `setrya demo memory-files` to see a held writer restore deleted data. The drill checks working files and Git history, including reflog-reachable commits. A separate scanner reads Turso AgentFS snapshots.
- The LangGraph/PostgreSQL starter checks checkpoints, long-term memory and a scoped SQL store. Doctor points to the related binding and reports missing baselines.
- Evidence v3.1 groups repeated replies per question and records lexical-only judging and journal provenance. Older evidence still replays. Judge chains accept lists and pull-request comments keep their existing thread when the Action is upgraded.

## 1.2.0

- Connect an existing application with journal-aware stack starters. Doctor names missing hooks, scoped store reads and scanner baselines. Run a declarative case directly with `setrya run case.yaml`.
- Issue a read-only observed-deletion receipt for journalled keys. Recheck due follow-ups and replay the linked result. Generate text or Markdown for engineering, review and customer audiences, and associate evidence with customer-defined control references.
- Check inventory drift against observed storage boundaries. Each of the four stack recipes has a standalone application example and CI.
- Direct absence checks require a physical store with sufficient read visibility. Distributed journal checks bind the expected producers and keep the case open when a producer is missing.
- Weekly SDK qualification now includes LangGraph and opens a tracking issue on a regression. Adapter and scanner request forms and the contributor conformance guide are available.

## 1.1.0

- Sample model responses repeatedly across question variants. Any positive dominates; lexical-only negatives and missing observations leave the absence unverified. Receipts expose the counts and judge coverage. Historical receipts replay under their original rules.
- Run a stack-specific or declarative HTTP case and diagnose missing fixture bindings. Use stable fixture imports from `setrya.erasure`. CI exercises current OpenAI Agents, Claude Agent and Langfuse releases.
- Update a single pull-request comment with bounded evidence and a receipt link. Pin the Action by SHA and check the installed package matches.
- Render customer summaries and inspect journalled keys for a completed request with read-only credentials. Weekly samples report uncertainty and unresolved requests.
- Stage discovery demo output until all three lanes verify, build directly from the source package and warn about legacy commands scheduled for removal in 2.0.

## 1.0.3

- Shorten the Action description to fit GitHub Marketplace and check the publishing limit in CI.

## 1.0.2

- The Marketplace quickstart gives a first PR a runnable deletion test and a workflow that saves its receipt. The guide shows how to connect the same starter to an application’s own store, authenticated answer route and deletion operation.

## 1.0.1

- A live person drill now checks that its starter returned the evidence issued by that invocation. Replay and signing honor an explicit person or tenant scope. A bundle with competing receipts is rejected, including inside a discovery case. Supported historical receipts still replay under their original rules.
- A worker checkpoint belongs to the subject whose job was paused. A drill that never held a worker skips its checkpoint callback.
- SQLite canary reads require committed data. Cache scanners check the stored answer bodies across a consistent read. Custom cache backends need a snapshot reader to verify this boundary.
- The CLI reports fixture setup failures without publishing the application's exception text. An explicit person scope now opens the same workspace demo and starter as the default. Doctor identifies missing fixture hooks, and the GitHub workflow uploads a receipt only after the action verifies it.
- A search PR review verifies the digest of the completed recording, including when the application assigns its own run ID. It keeps the report when test teardown times out. Reviewers can replay the observation and see which lane lacks a native exit.

## 1.0.0

- A single person-erasure case now accepts a private key to delete, a shared object to rewrite, a required record to preserve, or a mixture. Start with one authenticated reader and add more callers as needed.
- `setrya init` creates a working delete-only fixture. `setrya doctor` reads its baseline. The default demo tests two teammates reopening a SaaS workspace after a customer asks for deletion. The LangGraph analyst investigation remains available.
- Marker, regex and entity judges evaluate text from the serving endpoint. An optional model judge records its decision for offline replay. Fixtures choose how much matched text to retain.
- New person and tenant receipts and experimental Macie case results use the published v3 schema. Archived tenant v1, person v2, earlier person v3 and discovery evidence retain their historical verification paths. New v2 person runs have been retired.
- The scanner and instrumentation pack covers PostgreSQL/pgvector, Elasticsearch, Redis, LangGraph, OpenAI Agents and Responses, Claude Agent SDK and Langfuse. Direct reads, observed calls and provider-side unknowns are documented separately.
- The repository ships a GitHub Action for running a fixture and posting a receipt summary to a PR. `init`, `run`, `replay`, `demo` and `verify` are the primary commands. Older search, cache and offboarding tools remain accessible by name. [1.x compatibility](docs/api-stability.md)

## 0.18.0

Imported an Amazon Macie finding, bound it to a reviewed application mapping and traced its effects across declared AI services. [Release](https://github.com/davidahma/setrya/releases/tag/v0.18.0)

## 0.17.0

Introduced shared-object erasure, two-reader checks, version lineage, scoped SQLite canaries and reviewable provider follow-ups. [Release](https://github.com/davidahma/setrya/releases/tag/v0.17.0)

## 0.16.0

Introduced per-person erasure drills, copy journals, store adapters and signed v2 receipts. [Release](https://github.com/davidahma/setrya/releases/tag/v0.16.0)
