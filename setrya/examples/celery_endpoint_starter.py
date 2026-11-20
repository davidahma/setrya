"""Connect your disposable app's authenticated answer route and bound cache-fill task.

https://github.com/davidahma/setrya/blob/v0.14.0/docs/endpoint-framework.md
"""

from contextlib import contextmanager
import shlex

import pytest

from setrya.endpoint import EndpointCase, MissingHook, check_endpoint
from setrya.integrations.celery_langchain import CeleryLangChainEndpoint
from setrya.status import Status
from setrya.store_signals import passage_metadata


CASE = EndpointCase(
    route="policy-answer", revoked="case-reader", retained="case-editor",
    question="What is the refund window?",
    private_source=passage_metadata("internal-policy", "1", "0")["setrya_marker"],
    public_source=passage_metadata("public-policy", "1", "0")["setrya_marker"],
    private_answer="The staff refund window is 45 days.",
    public_answer="The public refund window is 14 days.",
)


def open_test_app(run_id):
    """Return your disposable app fixture as a context manager."""
    raise MissingHook("application")


@contextmanager
def application(run_id):
    with open_test_app(run_id) as app:
        with CeleryLangChainEndpoint(
            app.test_redis, run_id, CASE,
            task=app.fill_answer.s(CASE.revoked, CASE.question),
            seed=app.seed, revoke=app.revoke, send=app.send_answer,
        ) as endpoint:
            yield endpoint


def test_access_revocation(tmp_path):
    output = tmp_path / "endpoint-run"
    verdict, text = check_endpoint(CASE, application, output)
    message = f"{text}\nReplay  " + shlex.join(["setrya", "replay", str(output)])
    if verdict.status is not Status.PASS:
        pytest.fail(message, pytrace=False)
    print(message)
