# Find new copy sites before writing a fixture

Use the copy scan to give a pull request reviewer a file and line where a new write may retain customer data. Start before the application has a Setrya fixture.

```sh
setrya discovery copy-scan . --output /tmp/setrya-copy-scan.json
setrya discovery copy-check --base origin/main --candidate HEAD \
  --output /tmp/setrya-copy-report.json
```

`copy-scan` reads source in the selected directory. `copy-check` reads regular Git blobs for changed source files and compares the merge base with the candidate. It skips unchanged copy sites and reports new or modified calls. Both default to Python and run without executing application code or connecting to stores. Choose fresh output paths.

The report lists skipped files by extension and includes a bounded path sample for skipped source files. A recognized storage import with no matching write, an unresolved storage write, or skipped source in the selected directory or change set yields **Coverage unknown**. A read-only import can create an advisory. Inspect the file to decide whether it writes data. Both commands exit 3 for unknown coverage. `copy-check` also exits 3 when a new site needs review. A scan that cannot complete exits 2.

## Scan TypeScript and JavaScript

```sh
python -m pip install 'setrya[typescript]'
setrya discovery copy-scan . --language typescript
setrya discovery copy-check --base origin/main --candidate HEAD --language typescript
```

Use `--language all` for a repository mixing Python and Node. The parser installs from Python wheels and runs without Node or npm. It reads `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs` and `.cjs` files. Declaration files and the `node_modules`, `.next` and `.turbo` directories are outside its write scan. It reads static ESM imports and CommonJS `require()` without loading project configuration.

Node rules recognize supported writes for files, SQLite, PostgreSQL, S3, Redis, vector stores, agent memory, stateful model calls and traces. SQL leads include supported literal writes and client operations through `pg`, postgres.js, Prisma, Drizzle and Kysely. SDK rules include known write calls for Vercel AI, Mastra, LangGraph, Mem0, Anthropic, Claude Agent SDK, Letta, Chroma, Supabase and Upstash Vector. The scan follows recognized clients through simple class fields and local arguments or type annotations. It flags some dynamic SQL as unknown. Cross-file dispatch and arbitrary wrappers require review. [Try the TypeScript starter](typescript-starter.md)

## Scan coverage

The scan recognizes write-mode Python `open`, `pathlib.Path` writes and atomic moves, boto3 S3 uploads and Google Cloud Storage blob writes, Qdrant, Pinecone, Chroma, Elasticsearch and pgvector writes, Redis writes, LangGraph Store, Mem0, Graphiti, Zep and Letta memory calls, potentially stateful OpenAI and Bedrock Agent Runtime calls, and Langfuse and OpenTelemetry trace sites. The file, provider and PDF scanners share bounded AST parsing. The copy report contains static labels and hashed site IDs. It omits source snippets, storage keys and path literals from the call arguments.

For SQLite, psycopg and SQLAlchemy, the scan recognizes supported calls using literal insert, update, replace or merge statements. It also recognizes common SQLAlchemy ORM and statement-builder writes. Existing pgvector checks recognize literal vector casts on insert or update. For Elasticsearch, it recognizes known clients and bulk helpers, including `request` calls with a literal verb and recognizable document-write path. Dynamically assembled SQL on a recognized database client can yield unknown coverage.

The scan recognizes known SDK constructors and simple aliases. It does not follow dispatch through arbitrary wrappers, configuration, generated code or dynamic receivers. A candidate means the source has a supported call that might make a copy. The scan cannot determine whether it runs or whether its input contains customer data.

## Prioritize background writers

The report lists deferred writes first and identifies the writer in the `writer` field. Python checks include decorated Celery, RQ, Dramatiq and Temporal tasks, supported retry decorators, and callbacks scheduled with `asyncio`, `threading.Thread`, `threading.Timer`, or `ThreadPoolExecutor.submit`. They also recognize LangMem's `ReflectionExecutor.submit` and CrewAI's `Memory.remember_many`. Node checks include direct local callbacks passed to BullMQ, imported timers, unshadowed `setTimeout`, `setImmediate`, `queueMicrotask` and `process.nextTick`.

For these sites, the PR advisory asks you to hold the writer across deletion. The suggested mapping includes `mode: late-write`, the evidence value for the CLI's `--mode write-back`. Closing a deferred site with an approved mapping requires a passing receipt from an exercised write-back check. A deletion-only receipt leaves the site open. [Why deleted data comes back](write-back.md)

## Review a new Redis answer cache

A new `cache.set(key, answer)` on a receiver built with `redis.Redis.from_url` appears as a cache site. The JSON report gives the path, line, operation and stable site ID. A PR comment includes a starter mapping for the first displayed site. Review the storage surface and replace the example store, key and test path with those used by your disposable deletion test.

```json
{
  "format": "setrya.copy-site-mapping.v1",
  "sites": [
    {
      "site_id": "<copy the 64-character ID from the report>",
      "store": "redis-answer-cache",
      "key": "answer-for-disposable-test-subject",
      "test": "setrya-case/test_erasure.py"
    }
  ]
}
```

Save the map to `setrya-copy-coverage.json` at the repository root. A map records an operator-reviewed association between one call and a case artifact. The copy check displays candidate map changes for review. A candidate-proposed map alone cannot close its own finding.

The suggested TypeScript YAML path lets the reviewer find the HTTP case and replay its deletion result. That case leaves the copy site advisory open. To mark a site as `evidence-replayed`, use the entrypoint of a trusted local Python fixture as the mapping's `test`. With the map already approved in the merge base, pass `--receipt path/to/bundle`. Setrya requires a clean candidate checkout, a test entrypoint bound to that commit, and a passing local person drill for the exact store and key with a delete or rewrite action. A separate failing or incomplete matching receipt keeps the finding open.

## Run it in a pull request

[Download the release workflow](https://github.com/davidahma/setrya/releases/download/v1.18.0/setrya-copy-quickstart.yml) and save it under `.github/workflows/` in your repository. A read-only job with full Git history calls the SHA-pinned Setrya Action with `operation: copy-scan` and the event's base and candidate commits. It publishes a JSON report and a job summary. The advisory step succeeds and sets `exit-code` to 3 when sites need review or coverage is unknown. A scan error fails the step.

A separate job can post the bounded summary with `operation: copy-comment` and `pull-requests: write`. Use this job on same-repository PRs, without a repository checkout or an application test step. It updates one copy review comment, including when the scan fails, and skips superseded PR commits. Fork PRs can read the workflow run and any published report. The JSON report includes sites omitted from the bounded comment and a starter mapping per site.

For TypeScript, set `copy-language: typescript` under the **scan** step's `with` inputs and install `'setrya[typescript]==1.18.0'` in that job. Use `copy-language: all` for mixed repositories. Keep the comment job separate.

Use the [first deletion drill](github-action.md) to observe what survives a completed deletion once you have a storage lead to check. The [changed-route check](coverage.md#find-changed-routes-without-matching-tests) covers registered retrieval routes and jobs as a separate view of the application.
