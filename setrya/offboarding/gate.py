"""Evaluate staging observations against an independently supplied probe policy.

Adapted from Axiom v0.3.2 for Setrya's offboarding CLI.
"""

from pathlib import Path
import re
import sys

from setrya.status import Status

from .inputs import InvalidReport, keys_match, read_json, require, save

MAX_PROBES = 256
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CHECKPOINTS = ("after_offboarding", "after_worker")
STATES = ("present", "absent", "unavailable", "not_tested")
PROBE_FIELDS = {"id", "scope", "checkpoint"}
EXIT_CODES = {v: Status.from_gate(v).exit_code for v in ("PASS", "FAIL", "INCONCLUSIVE")}


def valid_token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def validate_document(document, *, observations):
    fields = {"version", "target", "probes"}
    if observations:
        fields.update(("revision", "run_id", "offboarding"))
    require(keys_match(document, fields), "Unexpected or missing top-level fields.")
    require(
        type(document["version"]) is int and document["version"] == 1,
        "Unsupported format version.",
    )
    require(valid_token(document["target"]), "Invalid target identifier.")
    if observations:
        require(valid_token(document["revision"]), "Invalid revision identifier.")
        require(valid_token(document["run_id"]), "Invalid run identifier.")
        require(
            document["offboarding"] in ("completed", "unconfirmed"),
            "Unknown offboarding state.",
        )

    probes = document["probes"]
    require(isinstance(probes, list), "Probes must be an array.")
    require(len(probes) <= MAX_PROBES, "Too many probes.")
    require(observations or probes, "The policy must require at least one probe.")
    probe_fields = PROBE_FIELDS | {"before", "after"} if observations else PROBE_FIELDS
    indexed = {}
    for probe in probes:
        require(isinstance(probe, dict), "A probe must be an object.")
        is_worker = observations and probe.get("checkpoint") == "after_worker"
        fields = probe_fields | {"worker"} if is_worker else probe_fields
        require(keys_match(probe, fields), "Unexpected or missing probe fields.")
        require(valid_token(probe["id"]), "Invalid probe identifier.")
        require(valid_token(probe["scope"]), "Invalid scope identifier.")
        require(probe["checkpoint"] in CHECKPOINTS, "Unknown probe checkpoint.")
        require(probe["id"] not in indexed, "Duplicate probe identifier.")
        if observations:
            for phase in ("before", "after"):
                pair = probe[phase]
                require(keys_match(pair, {"subject", "control"}), "Invalid read pair.")
                require(
                    all(state in STATES for state in pair.values()),
                    "Unknown read state.",
                )
        if is_worker:
            worker = probe["worker"]
            require(
                keys_match(
                    worker,
                    {"captured_before_offboarding", "finished_after_offboarding"},
                ),
                "Invalid worker overlap fields.",
            )
            require(
                all(type(flag) is bool for flag in worker.values()),
                "Invalid worker flag.",
            )
        indexed[probe["id"]] = probe
    return indexed


def assess_probe(expected, observed, completed):
    reasons = [] if completed else ["offboarding_unconfirmed"]
    result = {**expected, "verdict": "INCONCLUSIVE", "reasons": reasons}
    if observed is None:
        reasons.append("missing_probe")
        return result
    mismatches = [
        f"{field}_mismatch"
        for field in ("scope", "checkpoint")
        if observed[field] != expected[field]
    ]
    if mismatches:
        reasons.extend(mismatches)
        if completed and observed["after"]["subject"] == "present":
            reasons.append("observed_after_subject_present_outside_policy")
        return result

    if expected["checkpoint"] == "after_worker":
        if not observed["worker"]["captured_before_offboarding"]:
            reasons.append("worker_capture_unobserved")
        if not observed["worker"]["finished_after_offboarding"]:
            reasons.append("worker_completion_unobserved")
    for phase, role, required in (
        ("before", "subject", "present"),
        ("before", "control", "present"),
        ("after", "subject", "absent"),
        ("after", "control", "present"),
    ):
        state = observed[phase][role]
        if state != required:
            reasons.append(f"{phase}_{role}_{state}")
    if completed and observed["after"]["subject"] == "present":
        result["verdict"] = "FAIL"
    elif not reasons:
        result["verdict"] = "PASS"
    return result


def technical_verdict(probes):
    verdicts = {probe["verdict"] for probe in probes}
    if "FAIL" in verdicts:
        return "FAIL"
    if "INCONCLUSIVE" in verdicts:
        return "INCONCLUSIVE"
    return "PASS"


def evaluate(policy, observations, *, expect_run=None, expect_revision=None):
    required = validate_document(policy, observations=False)
    observed = validate_document(observations, observations=True)
    require(policy["target"] == observations["target"], "Target does not match policy.")
    require(observed.keys() <= required.keys(), "An observed probe is undeclared.")
    if expect_run is not None:
        require(expect_run == observations["run_id"], "Run identifier does not match.")
    if expect_revision is not None:
        require(expect_revision == observations["revision"], "Revision does not match.")

    completed = observations["offboarding"] == "completed"
    probes = [
        assess_probe(probe, observed.get(identifier), completed)
        for identifier, probe in required.items()
    ]
    return {
        "version": 1,
        "target": policy["target"],
        "revision": observations["revision"],
        "run_id": observations["run_id"],
        "verdict": technical_verdict(probes),
        "probes": probes,
    }


def add_command(jobs):
    staging = jobs.add_parser(
        "gate", help="check staging observations against a policy"
    )
    staging.add_argument("--policy", type=Path, required=True)
    staging.add_argument("--observations", type=Path, required=True)
    staging.add_argument("--output", type=Path, help="write a content-free JSON report")
    staging.add_argument("--expect-run", help="require this observation run identifier")
    staging.add_argument(
        "--expect-revision", help="require this observed target revision"
    )


def run(args):
    try:
        policy, policy_digest = read_json(args.policy, "policy")
        observations, data_digest = read_json(args.observations, "observations")
        report = evaluate(
            policy,
            observations,
            expect_run=args.expect_run,
            expect_revision=args.expect_revision,
        )
        report["policy_sha256"] = policy_digest
        report["observations_sha256"] = data_digest
        if args.output is not None:
            if args.output.exists() and any(
                args.output.samefile(path) for path in (args.policy, args.observations)
            ):
                raise InvalidReport("Output must be separate from both inputs.")
            save(report, args.output)
    except InvalidReport as error:
        print(f"Invalid gate input. {error}", file=sys.stderr)
        return Status.ERROR.exit_code
    except OSError:
        print("Could not write gate report.", file=sys.stderr)
        return Status.ERROR.exit_code
    for probe in report["probes"]:
        print(
            Status.from_gate(probe["verdict"]).label,
            probe["id"],
            probe["scope"],
            probe["checkpoint"],
            *probe["reasons"],
        )
    print(f"Gate {Status.from_gate(report['verdict']).label}")
    return EXIT_CODES[report["verdict"]]
