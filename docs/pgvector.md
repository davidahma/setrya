# Test indexing changes against pgvector

Use the optional PostgreSQL example if your team controls its indexing worker and search endpoint. The app has a PostgreSQL queue and an independent worker process. It serves its regular retrieval route over loopback HTTP and ranks fixed vectors with pgvector's L2 operator. The examples use synthetic policies, a disposable database, and a separate schema for each run.

## Start the local database

Run from the checkout or unpacked release with Python 3.11 or later and Docker Compose.

```sh
docker compose -f setrya/examples/pgvector-search/compose.yml up -d --wait
docker compose -f setrya/examples/pgvector-search/compose.yml exec -T postgres \
  psql -U setrya -d setrya_fixture -c 'CREATE EXTENSION IF NOT EXISTS vector'

python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install '.[pgvector,pytest]'
export SETRYA_PGVECTOR_DSN='postgresql://setrya:setrya-local@127.0.0.1:55432/setrya_fixture'
python3 -m setrya qualify --case pgvector
```

The Docker database uses test credentials. Use an isolated local database with permission to create schemas. The fixture takes its DSN from the environment and accepts a loopback TCP connection to `127.0.0.1`. A different local port can be set with `SETRYA_PGVECTOR_PORT` before starting Compose and in the DSN above. The SQLite quickstart is available with Python and FTS5 alone.

## Check a delete and re-upload

```sh
sed 's/mode = "fixed"/mode = "broken"/' setrya/examples/pgvector-search/reupload.toml > /tmp/setrya-broken.toml
python3 -m setrya check /tmp/setrya-broken.toml --report before.json
python3 -m setrya check setrya/examples/pgvector-search/reupload.toml --report after.json
python3 -m setrya compare before.json after.json --summary comparison.md
```

The first command that runs `check` should exit 1. It holds the worker after it reads the old upload, deletes that upload, and publishes the replacement. In the broken run, the old worker resumes and leaves a stale candidate under the deleted upload. The search route ranks candidates before filtering deleted uploads. The stale candidate takes a top-three slot, so search returns two current passages and drops the clause about refunding to the original payment method. A broader search still finds the clause. The guarded worker rejects the old write and all three passages return. The unrelated delivery probe asks for up to three and legitimately returns one.

The [refund drill](../setrya/examples/pgvector-search/drill.toml) covers the original late overwrite. Try `mode = "opaque"` to keep a clean-looking search view while withholding proof that it has seen the old write. `python3 -m setrya diagnose run.json` prints the first gap. The code and issue that inspired the delete-and-reupload example are available upstream. [Dify issue](https://github.com/langgenius/dify/issues/44363) · [Merged fix](https://github.com/langgenius/dify/pull/44364)

## Connect another local application

1. Copy the [adapter factory](../setrya/examples/pgvector_search/adapter.py) and a drill. Point the factory at a client for your own app. The [reference HTTP client](../setrya/examples/support_search/client.py) shows the controls the app exposes, and the [adapter](../setrya/examples/support_search/adapter.py) shows how responses become Setrya evidence.
2. Implement the minimum hooks on disposable data. Seed an old revision, pause the intended attempt after it reads that revision, and let an independent publication path publish and acknowledge the replacement.
3. Send the question, caller, and depth to the real retrieval route. Return identifiers for the passages that actually reached the caller. Include the snapshot watermark when the query path exposes it.
4. Release the old attempt. Tie a rejection or commit to that run and attempt. Run the broken, guarded, and missing-evidence cases once. Use `diagnose` to identify which observation is missing if the verdict is inconclusive.

The [adapter contract](adapters.md) covers implementation details and the [paired trial](benchmark.md) specifies how to measure the one-hour setup target with a developer who has not seen the fixture. The local qualification measures synthetic outcomes.

Stop the disposable database when finished. Use `-v` to remove its fixture data from this Compose project.

```sh
docker compose -f setrya/examples/pgvector-search/compose.yml down -v
```

[pgvector query reference](https://github.com/pgvector/pgvector#querying) · [Supabase’s queue and embedding architecture](https://supabase.com/blog/automatic-embeddings)

The worker records a native WAL position in its terminal job row after the index transaction commits. Search reads that row and the vector candidates in the same repeatable-read snapshot. [Store evidence](store-signals.md#postgresql-and-pgvector)
