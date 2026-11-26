# Test deletion in a TypeScript application

Use the bundled starter to see a real HTTP deletion checked against both the serving route and SQLite. It runs on Node.js 24 LTS or later with built-in HTTP, SQLite and TypeScript support. The starter uses no npm packages or model credentials.

```sh
python -m pip install 'setrya[case-yaml]'
setrya init --stack typescript --output ts-fixture
python ts-fixture/run.py --output guarded-receipt
setrya replay guarded-receipt
```

The launcher creates a fresh test database and distinct tokens for two readers and an erasure caller. It starts the server on a free loopback port. `doctor` establishes the positive baseline, then `run` calls the erasure route, reads both answers and inspects three declared rows. The `PASS` receipt shows that the source note and cached note disappeared while the team dispatch day survived.

Run the same drill against the version that deletes the source and leaves a cached answer behind. Both commands return exit code 1. The receipt names the residual key and the callers who can still read it.

```sh
python ts-fixture/run.py --mode broken --output broken-receipt
setrya replay broken-receipt
```

## Find the writes in the source

The optional scanner locates the SQLite writes for the note, its cached copy and the dispatch day. It inspects the TypeScript source directly and can run on a machine without Node.js.

```sh
python -m pip install 'setrya[typescript]'
setrya discovery copy-scan ts-fixture --language typescript
```

## Use the case with your application

Start with the generated `case.yml`. Bind the answer and erasure routes to a disposable deployment, declare the exact store keys to erase and retain, and supply test credentials through the named environment variables. Use a test fact whose previous and expected answers you can verify. Run the ordinary HTTP case against those bindings.

```sh
setrya doctor --case ts-fixture/case.yml
setrya run ts-fixture/case.yml --output my-app-receipt
setrya replay my-app-receipt
```

This case exercises deletion and direct readback. Replay its receipt to see what the app returned. For a copy-site review that closes a finding automatically, use a commit-bound local Python fixture. To hold a background writer across the deletion, use the write-back drill with worker hooks. [HTTP case settings](http-cases.md) · [Write-back drills](write-back.md) · [Copy-site scanning](copy-coverage.md#review-a-new-redis-answer-cache)
