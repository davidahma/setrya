"""Exercise the installed PR commands against a small synthetic route registry."""
import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from setrya.cli import EXAMPLE
from setrya.examples.recorded_search.verify import git


ROUTES = '''from setrya.examples.support_search.adapter import python_fixture

class Router:
    def __init__(self):
        self.handlers = {}

    def get(self, path):
        def register(handler):
            self.handlers[path] = handler
            return handler
        return register

router = Router()

@router.get("/support/search")
def support(search, probe):
    return search(probe)

def adapter(context):
    backend = python_fixture(context)
    search = backend.search
    backend.search = lambda probe: router.handlers["/support/search"](search, probe)
    return backend
'''

AGENT_ROUTE = '''
@router.get("/agent/summary")
def agent_summary(search, probe):
    return search(probe)
'''

MAP = '''version = 1
[[component]]
name = "support-retrieval"
root = "app"
owner = "@search-platform"
decorators = ["router.get"]

[[component.binding]]
file = "app/routes.py"
surface = "router.get /support/search"
drill = "tests/refund.toml"
probe = "refund-window"
'''

TEST = '''import os
from pathlib import Path
from setrya.runner import run

result = run(os.environ["SETRYA_REVIEW_DRILL"], revision=os.environ["SETRYA_REVIEW_REVISION"])
result.report.save(Path(os.environ["SETRYA_REVIEW_REPORT"]))
raise SystemExit(result.verdict.status.exit_code)
'''


def command(argv, code, cwd, expected):
    proc = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=120)
    if proc.returncode != code or expected not in proc.stdout:
        raise RuntimeError(f"expected exit {code} and {expected!r}, got {proc.returncode}: {proc.stdout}")
    return proc.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="setrya-coverage-") as root:
        repo = Path(root)
        git(repo, "init", "--quiet", "--initial-branch=main")
        git(repo, "config", "user.name", "Setrya fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        git(repo, "config", "commit.gpgsign", "false")
        (repo/"app").mkdir()
        (repo/"app/__init__.py").touch()
        (repo/"tests").mkdir()
        drill = (EXAMPLE/"python.toml").read_text().replace(
            "setrya.examples.support_search.adapter:python_fixture", "app.routes:adapter")
        (repo/"tests/refund.toml").write_text(drill)
        (repo/"app/routes.py").write_text(ROUTES)
        (repo/"test_release.py").write_text(TEST)
        (repo/"setrya-coverage.toml").write_text(MAP)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Test the support read path")
        git(repo, "tag", "base")
        (repo/"app/routes.py").write_text(ROUTES + AGENT_ROUTE)
        git(repo, "commit", "-qam", "Expose generated summaries to the agent")
        git(repo, "tag", "candidate")
        # Both native commands exercise the existing route successfully.
        common = ["--repo", str(repo), "--base", "base", "--candidate", "candidate"]
        handoff = output/"handoff"
        command(["setrya", "run-pr", *common, "--exit-contract", "setrya",
                 "--drill", str(repo / "tests/refund.toml"), "--output", str(handoff),
                 "--", sys.executable, "test_release.py"], 0, output, "PASS")
        command(["setrya", "coverage", *common, "--evidence", str(handoff/"candidate.json"),
                 "--summary", str(output/"coverage.md")], 3, output, "router.get /agent/summary")
        result = (output/"coverage.md").read_text()
        if "@search-platform" not in result or result.count("GAP  ") != 1:
            raise RuntimeError("expected one unmapped route and its owner")
        git(repo, "bundle", "create", str(output/"application.bundle"), "main", "base", "candidate")
        print(result)


if __name__ == "__main__":
    main()
