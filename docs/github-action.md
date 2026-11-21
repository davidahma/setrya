# Put the erasure receipt on a pull request

Commit the connected starter and its disposable fixture to the application repository. This workflow runs the same test as the developer, retains its receipt on failure and posts the engineering verdict and case-review status to the PR. Change the Python installation step to include the dependencies your fixture needs.

```yaml
name: Erasure
on: pull_request
permissions: {}
jobs:
  drill:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    outputs:
      code: ${{ steps.erasure.outputs.exit-code }}
      review: ${{ steps.erasure.outputs.review-status }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
        with:
          python-version: '3.13'
      - run: python -m pip install setrya==1.0.1
      - id: erasure
        uses: davidahma/setrya@v1.0.1
        with:
          starter: setrya-case/test_erasure.py
          mode: late-write
          output: ${{ runner.temp }}/setrya-receipt
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9
        if: ${{ !cancelled() && steps.erasure.outputs.review-status != '' && steps.erasure.outputs.review-status != 'UNAVAILABLE' }}
        with:
          name: setrya-receipt
          path: ${{ runner.temp }}/setrya-receipt/
          if-no-files-found: error
  comment:
    needs: drill
    if: ${{ !cancelled() && needs.drill.outputs.code != '' && github.event.pull_request.head.repo.full_name == github.repository }}
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
    steps:
      - uses: davidahma/setrya@v1.0.1
        with:
          operation: comment
          token: ${{ github.token }}
          exit-code: ${{ needs.drill.outputs.code }}
          review-status: ${{ needs.drill.outputs.review }}
```

The action uses the installed Python package for the drill and recomputes the result from the receipt. GitHub marks a nonzero step as failed. The `exit-code` output preserves the distinction between a residual (1), an error (2), and missing evidence (3). PASS is 0. When the fixture cannot create a receipt, the case-review status is `UNAVAILABLE`.

The second job uses the GitHub CLI on the hosted Ubuntu runner. It posts two enumerated statuses and the link to the run. It never checks out the PR or reads answer text. Fork PRs still get the job summary and available artifacts with read-only permissions. Pin the Setrya action to the full release commit in repositories that require immutable action references. Use synthetic subjects in public CI and review what your fixture puts in artifacts before enabling upload.

A reviewer downloads and unzips the artifact, then runs `setrya replay PATH` on the directory containing `erasure-receipt.json`. Add `--case-review` when an outstanding provider or backup obligation should block release. The exit code printed by a fixture is checked against the evidence it emits. [GitHub action metadata](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax) · [GitHub PR comment permissions](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)
