"""Resolve revisions and read immutable blobs without checking out application code."""
import os
from pathlib import Path
import subprocess

from .drill import InputError
from .evidence import git_revision


def git_bytes(repo: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1")
    try:
        return subprocess.run(["git", "--literal-pathspecs", "-C", str(repo), *args], env=env, check=True,
                              capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        raise InputError("could not read the requested Git repository, revision or path") from None


def resolve_commit(repo: Path, ref: str) -> str:
    return git_revision(git_bytes(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
                        .decode("ascii").strip())
