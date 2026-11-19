"""Build the reproducible Python archive for Setrya's offboarding commands."""

import argparse
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = b'''"""Portable entry point for the same offboarding commands as the installed SDK."""
import sys
from pathlib import Path
from setrya.offboarding.cli import main

invocation = ("python3", "-I", str(Path(sys.argv[0]).resolve()))
raise SystemExit(main(sys.argv[1:], invocation=invocation))
'''
# The release can be built from a source archive without git. Only these reviewed
# sources and fixtures belong in the executable, regardless of other local files.
PACKAGE_FILES = (
    "LICENSE",
    "setrya/__init__.py",
    "setrya/adapter.py",
    "setrya/checker.py",
    "setrya/drill.py",
    "setrya/evidence.py",
    "setrya/recorder.py",
    "setrya/runner.py",
    "setrya/scenario_fixture.py",
    "setrya/scenarios.py",
    "setrya/scenario_order.py",
    "setrya/status.py",
    "setrya/store_signals.py",
    "setrya/offboarding/__init__.py",
    "setrya/offboarding/cli.py",
    "setrya/offboarding/gate.py",
    "setrya/offboarding/inputs.py",
    "setrya/offboarding/receipt.py",
    "setrya/offboarding/receipt_cli.py",
    "setrya/offboarding/signature.py",
    "setrya/offboarding/observer.py",
    "setrya/offboarding/receipt-v1.schema.json",
    "setrya/offboarding/receipt_schema.py",
    "setrya/offboarding/reference.py",
    "setrya/examples/__init__.py",
    "setrya/examples/offboarding/__init__.py",
    "setrya/examples/offboarding/demo.py",
    "setrya/examples/offboarding/fixture.py",
    "setrya/examples/offboarding/sqlite/__init__.py",
    "setrya/examples/offboarding/sqlite/observe.py",
    "setrya/examples/offboarding/sqlite/policy.json",
    "setrya/examples/opendesk/__init__.py",
    "setrya/examples/opendesk/cli.py",
    "setrya/examples/opendesk/compose.yaml",
    "setrya/examples/opendesk/Dockerfile.trial",
    "setrya/examples/opendesk/observed.json",
    "setrya/examples/opendesk/policy.json",
    "setrya/examples/opendesk/policy-v2.json",
    "setrya/examples/opendesk/provider.py",
    "setrya/examples/opendesk/run.py",
    "setrya/examples/opendesk/to_gate.py",
    "setrya/examples/opendesk/transport.py",
    "setrya/examples/opendesk/verify_report.py",
)


def build(path):
    sources = [ROOT / name for name in PACKAGE_FILES]
    if any(source.resolve() != source or not source.is_file() for source in sources):
        raise ValueError("Package inputs must be regular files in the checkout.")
    if path.is_symlink() or (
        path.exists() and (not path.is_file() or any(path.samefile(src) for src in [*sources, Path(__file__)]))
    ):
        raise ValueError("Choose an output separate from the package sources.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with ZipFile(stream, "w") as archive:
                contents = {"__main__.py": ENTRY_POINT}
                for name, source in zip(PACKAGE_FILES, sources, strict=True):
                    contents[name] = source.read_bytes().replace(b"\r\n", b"\n")
                for name, content in contents.items():
                    entry = ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = 0o100644 << 16
                    archive.writestr(entry, content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "setrya-offboarding.pyz")
    try:
        build(parser.parse_args().output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Could not build the archive: {exc}\n")
