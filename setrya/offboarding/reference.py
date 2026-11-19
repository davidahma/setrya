"""Validate and reproduce Axiom v0.3.2 reference reports using Setrya."""

import re
import sys
from datetime import datetime

from .inputs import InvalidReport, keys_match, read_json, require, save

PHASES = ("baseline", "offboarded", "worker_finished")
SUBJECTS = ("acme", "birch")
SURFACES = ("source", "summary", "support")
CASES = ("broken", "fixed", "inconclusive")
ROLES = {
    "source": "storage_read_only",
    "summary": "storage_read_only",
    "support": "support_agent",
}
CASE_KEYS = {"case", "origin", "observations", "worker"}
UTC_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)


def observed_rows(case):
    require(isinstance(case["observations"], list), "Observations must be a list.")
    rows = {}
    phases = []
    for row in case["observations"]:
        require(
            keys_match(row, {"phase", "subject", "surface", "state", "observed_at"}),
            "Unexpected observation fields.",
        )
        phase, subject, surface = row["phase"], row["subject"], row["surface"]
        require(
            phase in PHASES and subject in SUBJECTS and surface in SURFACES,
            "Unknown observation.",
        )
        allowed = (
            ("returned", "not_returned", "unavailable")
            if surface == "support"
            else ("present", "absent", "unavailable")
        )
        require(row["state"] in allowed, "Invalid state for surface.")
        timestamp = row["observed_at"]
        require(
            isinstance(timestamp, str) and UTC_TIME.fullmatch(timestamp),
            "Invalid UTC timestamp.",
        )
        try:
            datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            raise InvalidReport("Invalid UTC timestamp.") from None
        key = phase, subject, surface
        require(key not in rows, "Duplicate observation.")
        rows[key] = row["state"]
        phases.append(PHASES.index(phase))
    require(
        len(rows) == len(PHASES) * len(SUBJECTS) * len(SURFACES),
        "Incomplete observation matrix.",
    )
    require(phases == sorted(phases), "Observations cross checkpoint order.")
    return rows


def assess(case):
    """A positive observation of residue outranks gaps elsewhere in the run."""
    require(keys_match(case, CASE_KEYS), "Unexpected case fields.")
    require(
        case["case"] in CASES and case["origin"] == "controlled-fixture",
        "Unknown case or origin.",
    )
    worker = case["worker"]
    require(
        keys_match(worker, {"captured_before_delete", "terminal"}),
        "Unexpected worker fields.",
    )
    require(
        type(worker["captured_before_delete"]) is bool
        and worker["terminal"] in ("written", "discarded", "unobserved"),
        "Unknown worker state.",
    )
    rows = observed_rows(case)
    failures, gaps = [], []
    for subject in SUBJECTS:
        for surface in SURFACES:
            positive = "returned" if surface == "support" else "present"
            if rows["baseline", subject, surface] != positive:
                gaps.append(f"baseline.{subject}.{surface}")
            for phase in PHASES[1:]:
                state = rows[phase, subject, surface]
                check = f"{phase}.{subject}.{surface}"
                if state == "unavailable" or (subject == "birch" and state != positive):
                    gaps.append(check)
                elif subject == "acme" and state == positive:
                    failures.append(check)
    if not worker["captured_before_delete"] or worker["terminal"] == "unobserved":
        gaps.append("worker.checkpoint")
    if worker["terminal"] == "written":
        failures.append("worker.wrote_after_deletion")
    verdict = "FAIL" if failures else "INCONCLUSIVE" if gaps else "PASS"
    return {"verdict": verdict, "failures": failures, "gaps": gaps}


def validate(report):
    require(
        keys_match(report, {"schema_version", "fixture_sha256", "read_roles", "cases"}),
        "Unexpected top-level fields.",
    )
    require(
        type(report["schema_version"]) is int and report["schema_version"] == 1,
        "Unknown report version.",
    )
    require(
        isinstance(report["fixture_sha256"], str)
        and re.fullmatch("[0-9a-f]{64}", report["fixture_sha256"]),
        "Invalid fixture digest.",
    )
    require(report["read_roles"] == ROLES, "Unknown read roles.")
    cases = report["cases"]
    require(
        isinstance(cases, list) and len(cases) == len(CASES),
        "Expected all reference cases.",
    )
    for label, case in zip(CASES, cases):
        require(
            keys_match(case, CASE_KEYS | {"assessment"}) and case["case"] == label,
            "Unexpected reference case.",
        )
        expected = assess({key: case[key] for key in CASE_KEYS})
        require(
            case["assessment"] == expected, "Assessment disagrees with observations."
        )
    return report


def reproduced_reference_cases(cases):
    """Check the precise failure, correction and missing-control examples."""
    expected = {
        "broken": (
            "written",
            [
                "worker_finished.acme.summary",
                "worker_finished.acme.support",
                "worker.wrote_after_deletion",
            ],
            [],
        ),
        "fixed": ("discarded", [], []),
        "inconclusive": ("discarded", [], ["baseline.acme.support"]),
    }
    for case in cases:
        if case["case"] == "inconclusive" and not any(
            row["phase"] == "baseline"
            and row["subject"] == "acme"
            and row["surface"] == "support"
            and row["state"] == "unavailable"
            for row in case["observations"]
        ):
            return False
        terminal, failures, gaps = expected[case["case"]]
        assessment = case["assessment"]
        if (
            case["worker"] != {"captured_before_delete": True, "terminal": terminal}
            or assessment["failures"] != failures
            or assessment["gaps"] != gaps
        ):
            return False
    return True


def demo(path):
    from setrya.examples.offboarding import fixture

    try:
        return _produce(path, fixture)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Could not write reference report ({type(error).__name__}).", file=sys.stderr)
        return 2


def _produce(path, fixture):
    cases = []
    for mode in CASES:
        case = fixture.reference_case(mode)
        cases.append({**case, "assessment": assess(case)})
    report = validate(
        {
            "schema_version": 1,
            "fixture_sha256": fixture.revision(),
            "read_roles": ROLES,
            "cases": cases,
        }
    )
    save(report, path)
    print("SETRYA  Tenant offboarding in a controlled support system")
    print(
        f"{'Case':<14}{'Checkpoint':<18}{'Acme source':<15}{'Acme summary':<15}{'Agent read':<16}Birch control"
    )
    for case in cases:
        for phase in PHASES:
            states = {
                (r["subject"], r["surface"]): r["state"]
                for r in case["observations"]
                if r["phase"] == phase
            }
            control = all(
                states["birch", surface]
                == ("returned" if surface == "support" else "present")
                for surface in ROLES
            )
            print(
                f"{case['case']:<14}{phase:<18}{states['acme', 'source']:<15}{states['acme', 'summary']:<15}"
                f"{states['acme', 'support']:<16}{'healthy' if control else 'incomplete'}"
            )
        assessment = case["assessment"]
        details = ", ".join([*assessment["failures"], *assessment["gaps"]])
        print(
            f"  {assessment['verdict']}   worker={case['worker']['terminal']}"
            + (f"   {details}" if details else "")
        )
    print(f"Report: {path}")
    if not reproduced_reference_cases(cases):
        print(
            "Reference outcomes differed from the expected sequence.", file=sys.stderr
        )
        return 1
    return 0


def verify(path):
    try:
        report, _ = read_json(path, "report")
        validate(report)
    except (ValueError, OSError, RecursionError) as error:
        print(f"Invalid report ({type(error).__name__}).", file=sys.stderr)
        return 2
    print("Report valid. All recorded verdicts match the observations.")
    return 0
