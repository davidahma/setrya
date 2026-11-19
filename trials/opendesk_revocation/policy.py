"""Materialize immutable Git revisions of the two deployment manifests."""

import json
import os
import subprocess

from setrya.examples.opendesk.verify_report import TRIAL_BUILD


def policies(directory):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="Setrya trial",
               GIT_AUTHOR_EMAIL="trial@example.test", GIT_COMMITTER_NAME="Setrya trial",
               GIT_COMMITTER_EMAIL="trial@example.test", GIT_AUTHOR_DATE="2000-01-01T00:00:00Z",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")

    def git(*args, stdin=None):
        return subprocess.check_output(["git", *args], cwd=directory, env=env, input=stdin, text=True).strip()

    git("init", "--quiet", "--object-format=sha1", "--template=")
    result = {}
    parent = []
    for lane, private in (("base", False), ("candidate", True)):
        manifest = json.dumps({"opendesk": TRIAL_BUILD["commit"], "private": private}, sort_keys=True) + "\n"
        blob = git("hash-object", "-w", "--stdin", stdin=manifest)
        tree = git("mktree", stdin=f"100644 blob {blob}\tteam.json\n")
        revision = git("commit-tree", tree, *parent, "-m", f"Configure {lane} team")
        parent = ["-p", revision]
        result[lane] = revision, json.loads(git("show", revision + ":team.json"))
    return result
