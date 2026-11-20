"""Inspect two authenticated reads from an already prepared endpoint fixture."""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .checker import Verdict
from .drill import InputError, mapping
from .endpoint import MissingHook, _Contract
from .offboarding.inputs import InvalidReport, read_json, save
from .status import Status


MANIFEST = "endpoint-reads.json"
FORMAT = "setrya.endpoint-reads"


def _sample(response, contract):
    response = response if isinstance(response, dict) else {}
    status = response.get("http_status")
    text = response.get("answer")
    digest = sha256(text.encode()).hexdigest() if isinstance(text, str) else None
    # Persist only expected fixture identifiers and fingerprints. An unrelated
    # server principal, unexpected response or exception can contain user data.
    return {
        "error": None,
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "route": contract.route if response.get("route") == contract.route else None,
        "caller": contract.known_caller(response.get("caller")),
        "question": contract.question_sha256 if response.get("query_sha256") == contract.question_sha256 else None,
        "answer": digest if digest in (contract.private_answer_sha256, contract.public_answer_sha256) else None,
    }


def _checks(row, caller, contract):
    return (
        (row["status"] == 200, "HTTP 200", "inspect the HTTP status and sign-in for this session"),
        (row["caller"] == caller, "authenticated caller", "return the effective principal from the serving path"),
        (row["route"] == contract.route, f"route {contract.route}", "return the route that served this request"),
        (row["question"] == contract.question_sha256, "question", "return query_sha256 from the executed question"),
        (row["answer"] == contract.private_answer_sha256, "expected private answer",
         "return the actual response text and check the seeded private answer"),
    )


def _result(data):
    mapping(data, "endpoint reads", {"format", "version", "case", "reads", "application_error"})
    if data["format"] != FORMAT or type(data["version"]) is not int or data["version"] != 1:
        raise InputError("unsupported endpoint reads report")
    contract = _Contract.load(data["case"])
    reads = data["reads"]
    if not isinstance(reads, list) or len(reads) != 2 or type(data["application_error"]) is not bool:
        raise InputError("invalid endpoint reads report")
    lines = []
    next_step = None
    for caller, row in zip((contract.revoked, contract.retained), reads, strict=True):
        mapping(row, "endpoint read", {"status", "route", "caller", "question", "answer", "error"})
        status = row["status"]
        if (row["error"] not in (None, "missing-hook", "exception")
                or (row["error"] and any(row[key] is not None for key in ("status", "route", "caller", "question", "answer")))
                or (status is not None and (type(status) is not int or not 100 <= status <= 599))
                or row["route"] not in (None, contract.route)
                or row["caller"] not in (None, contract.revoked, contract.retained)
                or row["question"] not in (None, contract.question_sha256)
                or row["answer"] not in (None, contract.private_answer_sha256, contract.public_answer_sha256)):
            raise InputError("invalid endpoint read observation")
        checks = _checks(row, caller, contract)
        observed = [name for ok, name, _ in checks if ok]
        missing = [name for ok, name, _ in checks if not ok]
        line = f"{caller} · confirmed " + (", ".join(observed) or "none")
        if missing:
            line += " · missing " + ", ".join(missing)
            if status is not None and status != 200:
                line += f" · HTTP {status}"
            if row["caller"] is not None and row["caller"] != caller:
                line += f" · served as {row['caller']}"
            if row["error"] == "exception":
                line += " · answer() raised an exception"
            if next_step is None:
                if row["error"] == "missing-hook":
                    next_step = "Connect answer(caller, question) to the signed-in test sessions."
                elif row["error"] == "exception":
                    next_step = f"Check the exception from answer() for {caller} in the test application's logs."
                else:
                    hint = next(hint for ok, _, hint in checks if not ok)
                    next_step = f"Check answer() for {caller}. {hint[0].upper()}{hint[1:]}."
        lines.append(line)
    if data["application_error"]:
        next_step = "Connect application() to a disposable fixture and check its setup and cleanup."
    elif next_step is None:
        next_step = ("Both endpoint reads are confirmed. Run the full test with seed() warming both entries "
                     "and answer() returning their source IDs and cache writers, then connect start_old().")
    verdict = Verdict(Status.NOT_TESTED, "Worker race not exercised. " + next_step)
    return verdict, "\n".join([f"{verdict.status.label}  {verdict.detail}", *lines])


def replay(directory):
    try:
        data, _ = read_json(Path(directory) / MANIFEST, "endpoint reads")
    except InvalidReport as error:
        raise InputError(str(error)) from None
    return _result(data)


def run_reads(case, application, output):
    """Call answer() once per user. The caller prepares both sessions with private access."""
    directory = Path(output).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    contract = case._contract()
    data = {"format": FORMAT, "version": 1, "case": asdict(contract),
            "reads": [_sample(None, contract), _sample(None, contract)], "application_error": False}
    try:
        with application(uuid4().hex) as app:
            for index, caller in enumerate((case.revoked, case.retained)):
                try:
                    answer = getattr(app, "answer", None)
                    if not callable(answer):
                        raise MissingHook("answer")
                    data["reads"][index] = _sample(answer(caller, case.question), contract)
                except MissingHook:
                    data["reads"][index]["error"] = "missing-hook"
                except Exception:
                    # An unavailable session should not hide the other caller's response.
                    data["reads"][index]["error"] = "exception"
    except Exception:
        data["application_error"] = True
    save(data, directory / MANIFEST)
    return replay(directory)
