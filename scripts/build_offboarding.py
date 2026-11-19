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

invocation = (sys.executable, "-I", str(Path(sys.argv[0]).resolve()))
raise SystemExit(main(sys.argv[1:], invocation=invocation))
'''
# The release can be built from a source archive without git. Only these reviewed
# sources and fixtures belong in the executable, regardless of other local files.
PACKAGE_FILES = (
    "LICENSE",
    "setrya/__init__.py",
    "setrya/offboarding/__init__.py",
    "setrya/offboarding/cli.py",
    "setrya/offboarding/gate.py",
    "setrya/offboarding/inputs.py",
    "setrya/offboarding/receipt.py",
    "setrya/offboarding/receipt_cli.py",
    "setrya/offboarding/reference.py",
    "examples/__init__.py",
    "examples/offboarding/__init__.py",
    "examples/offboarding/demo.py",
    "examples/offboarding/fixture.py",
    "examples/offboarding/sqlite/__init__.py",
    "examples/offboarding/sqlite/observe.py",
    "examples/offboarding/sqlite/policy.json",
    "examples/opendesk/__init__.py",
    "examples/opendesk/cli.py",
    "examples/opendesk/compose.yaml",
    "examples/opendesk/observed.json",
    "examples/opendesk/policy.json",
    "examples/opendesk/policy-v2.json",
    "examples/opendesk/provider.py",
    "examples/opendesk/run.py",
    "examples/opendesk/to_gate.py",
    "examples/opendesk/transport.py",
    "examples/opendesk/verify_report.py",
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
