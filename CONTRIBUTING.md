# Contributing to Setrya

Run the [README quickstart](README.md#run-the-demo) first. Python 3.11 or later with SQLite FTS5 is enough for the normal test loop.

```sh
python3 -m unittest discover -s tests -v
python3 -m setrya qualify --quick
```

For changes to the optional pytest integration, install `.[pytest]`, then run `python3 -m pytest -q` and `python3 -m setrya.examples.recorded_search.verify --output /tmp/setrya-review` with a fresh output directory.

Use a pull request for changes. Explain which search behavior or public contract changes and include a command that reproduces the outcome. For a new drill, include an unaffected control and a deliberate failure that shows the assertion can catch the regression. Keep fixture data synthetic.

For diagnostics, include the exact commit, Setrya and Python versions, platform, and a redacted report if you have one. Use the [security disclosure process](SECURITY.md) for vulnerabilities and follow the [code of conduct](CODE_OF_CONDUCT.md) in issues and pull requests.

Tenant offboarding development, including the portable tool and OpenDesk trial, takes place in this repository. To reproduce their release checks, use the [portable build workflow](.github/workflows/offboarding-portable.yml) and [OpenDesk trial guide](setrya/examples/opendesk/README.md).

## Publish a Python release

When a GitHub release is published, the PyPI workflow builds the wheel and source distribution from its tag, checks both installs, and publishes the same files using the `pypi` GitHub environment. A final job installs the public release, runs the refund demo, and checks the generated pytest starter and its offline replay. The tag must match the package version, for example `v0.14.0` for version `0.14.0`.

For an existing published GitHub release that has not reached PyPI, dispatch the workflow from the default branch with the release tag:

```sh
gh workflow run publish.yml --ref main -f tag=v0.14.0
```

If only the final install check fails, rerun that failed job. PyPI release files are immutable. [Publishing runs](https://github.com/davidahma/setrya/actions/workflows/publish.yml)
