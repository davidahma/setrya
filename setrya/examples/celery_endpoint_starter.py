"""Connect your disposable app's authenticated answer route and bound cache-fill task.

https://github.com/davidahma/setrya/blob/v0.15.0/docs/endpoint-framework.md
"""

from contextlib import contextmanager
import shlex

import pytest

from setrya.endpoint import EndpointCase, MissingHook, check_endpoint, check_endpoint_reads
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
    """Return your disposable app fixture as a context manager.

    For the first two reads it needs only answer(caller, question). Send each
    request using the user's session and map the observed HTTP status, route,
    authenticated caller, served question fingerprint, and answer text.
    """
    raise MissingHook("application")


@contextmanager
def application(run_id):
    with open_test_app(run_id) as app:
        with CeleryLangChainEndpoint.from_application(app, run_id, CASE) as endpoint:
            yield endpoint


def test_access_revocation(tmp_path, request):
    output = tmp_path / "endpoint-run"
    reads_only = request.config.getoption("setrya_reads_only", default=False)
    check = check_endpoint_reads if reads_only else check_endpoint
    verdict, text = check(CASE, application, output)
    message = f"{text}\nReplay  " + shlex.join(["setrya", "replay", str(output)])
    if verdict.status is not Status.PASS:
        pytest.fail(message, pytrace=False)
    print(message)
