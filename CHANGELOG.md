# Changelog

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
