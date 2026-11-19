# Reproduce Dify’s keyword cleanup regression

Dify’s native test covers a document removed while a keyword write is in flight. The test exercises the upstream indexing service, real Jieba keyword processing, a disposable SQLite database, and a mocked Redis boundary. It asserts that a late write leaves no orphaned keyword entries. [Reported defect](https://github.com/langgenius/dify/issues/44363) · [Merged fix](https://github.com/langgenius/dify/pull/44364/files)

We ran the same upstream pytest test against the fix’s first parent and the fix commit using CPython 3.13.7 with outbound networking blocked during both test runs. The two parameterizations cover the ordinary write path and an outstanding pause flag.

| Application commit | Native pytest outcome | Observed failure |
| --- | --- | --- |
| [Base `226110d`](https://github.com/langgenius/dify/commit/226110d61179f9242c76d6550c524ae92f83e844) | 2 failed | The final keyword table still contained the deleted node in both cases. |
| [Candidate `06b8640`](https://github.com/langgenius/dify/commit/06b8640c4aedcff6737723b5799e8f9f0bb48f1d) | 2 passed | The keyword cleanup assertion held for both cases. |

This establishes the indexing regression on real upstream code. A search verdict takes additional observations from the actual retrieval path. The upstream test supplies the worker timing and keyword-table assertion. To obtain a Setrya verdict for user retrieval, add a replacement publication and query through the real route and caller before and after the held write. The [recorded pytest example](../examples/recorded_search/existing_test.py) demonstrates that full sequence on Setrya’s fixture. Adoption and reviewer time on an independent team’s release are still unmeasured.

## Repeat the native test

From a new temporary directory with Git, uv, and Python 3.13 available, fetch the two pinned application revisions. Dify’s API depends on the sibling `dify-agent` package.

```sh
set -eu
scratch="$(mktemp -d)"
repo="$scratch/dify"
base=226110d61179f9242c76d6550c524ae92f83e844
candidate=06b8640c4aedcff6737723b5799e8f9f0bb48f1d
test_file=tests/unit_tests/services/knowledge/indexing/adapters/test_execution.py
test_name=test_document_deleted_during_its_keyword_write_leaves_no_orphan_keywords

git init -q "$repo"
git -C "$repo" remote add origin https://github.com/langgenius/dify.git
git -C "$repo" config remote.origin.promisor true
git -C "$repo" config remote.origin.partialclonefilter blob:none
git -C "$repo" fetch -q --depth=2 --filter=blob:none origin "$candidate"
git -C "$repo" sparse-checkout set api dify-agent
git -C "$repo" checkout -q --detach "$candidate"

cd "$repo/api"
export UV_PROJECT_ENVIRONMENT="$scratch/venv"
uv sync --frozen --python 3.13 --group dev --no-install-project
cp "$test_file" "$scratch/native-test.py"
"$scratch/venv/bin/python" -m pytest -o addopts='' -q "${test_file}::${test_name}"

git -C "$repo" switch -q --detach "$base"
cp "$scratch/native-test.py" "$test_file"
base_exit=0
"$scratch/venv/bin/python" -m pytest -o addopts='' -q "${test_file}::${test_name}" || base_exit=$?
test "$base_exit" -eq 1
```

The setup downloads upstream source and locked dependencies. The pytest test uses disposable local state. Read the base output to confirm that it failed at the orphan-keyword assertion.
