"""Run Setrya's PR check against the HTTP approvals fixture in this checkout."""
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from setrya.endpoint import check_endpoint


def main():
    # The installed SDK and this external harness stay identical between lanes.
    # The HTTP handler, worker and cache policy come from the selected commit.
    path = Path.cwd() / "tests" / "fixtures" / "endpoint_app.py"
    spec = importlib.util.spec_from_file_location("approvals_fixture", path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    os.environ["APP_TEST_MODE"] = "guarded"
    with TemporaryDirectory() as tmp:
        verdict, text = check_endpoint(app.CASE, app.Application, Path(tmp) / "run")
    print(text)
    return verdict.status.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
