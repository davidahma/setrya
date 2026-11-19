# Contributing to Setrya

Run the [README quickstart](README.md#run-the-demo) first. Python 3.11 or later with SQLite FTS5 is enough for the normal test loop.

```sh
python3 -m unittest discover -s tests -v
python3 -m setrya qualify --quick
```

For changes to the optional pytest integration, install `.[pytest]`, then run `python3 -m pytest -q` and `python3 -m examples.recorded_search.verify --output /tmp/setrya-review` with a fresh output directory.

Use a pull request for changes. Explain which search behavior or public contract changes and include a command that reproduces the outcome. For a new drill, include an unaffected control and a deliberate failure that shows the assertion can catch the regression. Keep fixture data synthetic.

For diagnostics, include the exact commit, Setrya and Python versions, platform, and a redacted report if you have one. Use the [security disclosure process](SECURITY.md) for vulnerabilities and follow the [code of conduct](CODE_OF_CONDUCT.md) in issues and pull requests.
