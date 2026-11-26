# Find new copy sites before writing a fixture

Use the copy scan to give a pull request reviewer a file and line where a new write may retain customer data. Start before the application has a Setrya fixture.

```sh
setrya discovery copy-scan . --output /tmp/setrya-copy-scan.json
setrya discovery copy-check --base origin/main --candidate HEAD \
  --output /tmp/setrya-copy-report.json
```

`copy-scan` reads source in the selected directory. `copy-check` reads regular Git blobs for changed source files and compares the merge base with the candidate. It skips unchanged copy sites and reports new or modified calls. Both default to Python. Neither command executes application code or connects to stores. Choose fresh output paths. A copy check exits 3 when a site needs review, 2 when it cannot complete the scan, and 0 when there are no supported new sites needing review.

## Scan TypeScript

```sh
python -m pip install 'setrya[typescript]'
setrya discovery copy-scan . --language typescript
setrya discovery copy-check --base origin/main --candidate HEAD --language typescript
```

Use `--language all` for a mixed Python and TypeScript repository. The scanner uses Python wheels and requires neither Node nor npm. It reads `.ts`, `.tsx`, `.mts`, and `.cts` files, skips declaration files and the `node_modules`, `.next`, and `.turbo` directories, and does not load project configuration. The report and the PR summary identify the selected languages.

TypeScript rules recognize known imports and simple local aliases for Node file and SQLite writes, S3 uploads, Redis writes, Pinecone and Qdrant upserts, LangGraph and Mem0 memory, potentially stored OpenAI calls, and Langfuse and OpenTelemetry traces. SQLite coverage includes literal insert, replace and update statements passed to `DatabaseSync.prepare(...).run(...)`. The scan recognizes direct local callbacks passed to imported Node timers and BullMQ workers. Dynamic dispatch, external wrappers, and inter-file resolution are outside this scope. [Try the TypeScript starter](typescript-starter.md)

## Scan coverage

The scan recognizes write-mode Python `open`, `pathlib.Path` writes and atomic moves, boto3 S3 uploads and Google Cloud Storage blob writes, Qdrant, Pinecone, Chroma, Elasticsearch and pgvector writes, Redis writes, LangGraph Store, Mem0, Graphiti, Zep and Letta memory calls, potentially stateful OpenAI and Bedrock Agent Runtime calls, and Langfuse and OpenTelemetry trace sites. The file, provider and PDF scanners share bounded AST parsing. The copy report contains static labels and hashed site IDs. It omits source snippets, storage keys and path literals from the call arguments.

For pgvector, the scan looks for literal `INSERT` or `UPDATE` statements with vector type casts passed to `execute` or `executemany`. For Elasticsearch, it recognizes writes on known Python SDK clients and bulk helpers. It also checks `request` calls with a literal write verb and recognizable `/_doc`, `/_create` or `/_update` path, including simple formatted document IDs. Dynamically assembled SQL and arbitrary HTTP writes are outside these rules.

The scan recognizes known SDK constructors and simple aliases. It does not follow dispatch through arbitrary wrappers, configuration, generated code or dynamic receivers. A candidate means the source has a supported call that might make a copy. The scan cannot determine whether it runs or whether its input contains customer data.

## Prioritize background writers

The report lists deferred writes first and identifies the writer in the `writer` field. The scan recognizes decorated Celery, RQ, Dramatiq and Temporal tasks, functions with supported retry decorators, and local callbacks handed to an RQ queue or scheduled with `asyncio`. It also recognizes LangMem's `ReflectionExecutor.submit` and CrewAI's `Memory.remember_many` background saves. Detection follows simple aliases and calls between functions defined in the same Python file.

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

Save the map to `setrya-copy-coverage.json` at the repository root. For a TypeScript app, point `test` at the HTTP case YAML or the fixture that runs the write-back drill. A map records an operator-reviewed association between one call and a case artifact. The copy check displays candidate map changes for review. A candidate-proposed map alone cannot close its own finding. For a map already approved in the merge base, add `--receipt path/to/bundle` to the copy check. Setrya replays the evidence and requires a clean candidate checkout, a test entrypoint bound to that commit, and a passing local person drill for the exact store and key with a delete or rewrite action. A separate failing or incomplete matching receipt keeps the finding open. Replaying a matching receipt confirms what the test observed for the approved binding.

## Run it in a pull request

[Download the release workflow](https://github.com/davidahma/setrya/releases/download/v1.17.0/setrya-copy-quickstart.yml) and save it under `.github/workflows/` in your repository. A read-only job with full Git history calls the SHA-pinned Setrya Action with `operation: copy-scan` and the event's base and candidate commits. It publishes a JSON report and a job summary. The scan is advisory in the Action. The `exit-code` output is 3 for sites needing review while the step succeeds. A scan error fails the step.

A separate job can post the bounded summary with `operation: copy-comment` and `pull-requests: write`. Use this job on same-repository PRs, without a repository checkout or an application test step. It updates one copy review comment, including when the scan fails, and skips superseded PR commits. Fork PRs can read the workflow run and any published report. The JSON report includes sites omitted from the bounded comment and a starter mapping per site.

For TypeScript, set `copy-language: typescript` under the **scan** step's `with` inputs and install `'setrya[typescript]==1.17.0'` in that job. Use `copy-language: all` for mixed repositories. Keep the comment job separate.

Use the [first deletion drill](github-action.md) to observe what survives a completed deletion once you have a storage lead to check. The [changed-route check](coverage.md#find-changed-routes-without-matching-tests) covers registered retrieval routes and jobs as a separate view of the application.
