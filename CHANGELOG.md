# Changelog

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
