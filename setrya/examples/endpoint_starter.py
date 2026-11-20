"""Test what two users receive after a delayed worker resumes.

Connect these hooks to a disposable instance of your application.
https://github.com/davidahma/setrya/blob/v0.13.0/docs/endpoint.md
"""

from contextlib import AbstractContextManager
import shlex

import pytest

from setrya.endpoint import EndpointCase, MissingHook, check_endpoint
from setrya.status import Status


CASE = EndpointCase(
    route="policy-answer", revoked="case-reader", retained="case-editor",
    question="What is the refund window?",
    private_source="internal-policy-v1", public_source="public-policy-v1",
    private_answer="The staff refund window is 45 days.",
    public_answer="The public refund window is 14 days.",
)


class Application(AbstractContextManager):
    def __init__(self, run_id):
        self.run_id = run_id

    def __exit__(self, exc_type, exc, traceback):
        # Tear down this run's worker and fixtures, even if a hook fails.
        pass

    def seed(self):
        """Grant both users access and warm their caches with the private answer."""
        raise MissingHook("seed")

    def answer(self, caller, question):
        """Ask as this user. Map the served HTTP response and its cache metadata."""
        raise MissingHook("answer")

    def start_old(self):
        """Start a cache fill for the soon-to-be-revoked user. Return its task ID."""
        raise MissingHook("start_old")

    def checkpoint(self):
        """Return the held worker's Checkpoint, or None while waiting."""
        raise MissingHook("checkpoint")

    def captured(self):
        """Read the caller, question, answer and sources captured by that task."""
        raise MissingHook("captured")

    def revoke(self):
        """Revoke the first user's access, evict their entry and return Publication."""
        raise MissingHook("revoke")

    def release(self):
        """Resume the same task at its acknowledged checkpoint."""
        raise MissingHook("release")

    def receipt(self):
        """Return None while pending, then the task's terminal Receipt."""
        raise MissingHook("receipt")


def test_access_revocation(tmp_path):
    output = tmp_path / "endpoint-run"
    verdict, text = check_endpoint(CASE, Application, output)
    message = f"{text}\nReplay  " + shlex.join(["setrya", "replay", str(output)])
    if verdict.status is not Status.PASS:
        pytest.fail(message, pytrace=False)
    print(message)
