"""Drive several deliveries through one mutation and replay their search reports offline."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, replace
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic_ns
from typing import Protocol
from uuid import uuid4

from .adapter import Checkpoint, Publication, Receipt, RunContext, SearchResult
from .checker import Status, Verdict, evaluate
from .drill import Drill, InputError, Probe, ProbeRules, Rules, label, mapping
from .evidence import Report
from .offboarding.inputs import read_json, save
from .runner import _Execution

KINDS = ("update", "delete", "revoke")
# (message per delivery, release order). Retry also fails and requeues the
# first delivery before the retry captures the old input and crosses the change.
SCHEDULES = {
    "single": (("job-a",), (0,)),
    "retry": (("job-a",), (0,)),
    "duplicate": (("job-a", "job-a"), (0, 1)),
    "forward": (("job-a", "job-b"), (0, 1)),
    "reverse": (("job-a", "job-b"), (1, 0)),
}
FIXTURE = "setrya.scenario_fixture:Application"
FORMAT = "setrya.scenario"


class Delivery(Protocol):
    """One physical queue delivery on the shared application."""
    def start_old(self) -> str: ...
    def checkpoint(self) -> Checkpoint | None: ...
    def search(self, probe: Probe) -> SearchResult: ...
    def release(self) -> None: ...
    def receipt(self) -> Receipt | None: ...

    def message_id(self) -> str: ...
    def retryable_failure(self) -> Receipt | None: ...


class ScenarioApplication(Protocol):
    def seed(self) -> None: ...
    def delivery(self, message_id: str, attempt: str) -> Delivery: ...
    def publish(self) -> Publication | None: ...
    def close(self) -> None: ...


def preset(kind: str, mode: str = "fixed") -> Drill:
    if kind not in KINDS or mode not in ("fixed", "broken"):
        raise InputError("unknown scenario or fixture mode")
    updated = kind == "update"
    subject = ProbeRules("subject", "search", "support", 5, ("subject@A",),
                         ("subject@B",) if updated else (), ("subject@A",), absence=not updated)
    retained = ProbeRules("retained", "search", "support", 5, ("retained@A",), ("retained@A",), ())
    probes = [Probe(subject, "subject"), Probe(retained, "control")]
    if kind == "revoke":
        owner = ProbeRules("still-authorized", "search", "owner", 5, ("subject@A",), ("subject@A",), ())
        probes.append(Probe(owner, "subject"))
    rules = Rules(kind, "subject", "A", {"update": "updated", "delete": "deleted", "revoke": "revoked"}[kind],
                  "before-write", "subject", 2000, 2000, 300, 10, 100, tuple(p.rules for p in probes))
    return Drill(rules, tuple(probes), FIXTURE, {"mode": mode})


def _check_kind(kind, rules):
    if kind not in KINDS:
        raise InputError("unknown scenario")
    primary = next(p for p in rules.probes if p.id == rules.freshness_probe)
    if primary.absence != (kind != "update"):
        raise InputError("delete and revoke require an absence freshness probe; update requires a replacement")
    if kind == "revoke" and not any(
        p.scope != primary.scope and p.route == primary.route and
        set(primary.forbid) & set(p.require) & set(p.before) for p in rules.probes
    ):
        raise InputError("revocation requires another authorized caller who retains the target passage")


def _combine(verdicts):
    for status in (Status.FAIL, Status.ERROR, Status.NOT_TESTED):
        for verdict in verdicts:
            if verdict.status == status:
                return verdict
    tier = "write-confirmed"
    detail = "All scheduled deliveries passed their final caller and control probes"
    if any(verdict.evidence_tier == "observed-after-refresh" for verdict in verdicts):
        tier = "observed-after-refresh"
        detail += ". Observed after refresh (write visibility unverified)"
    return Verdict(Status.PASS, detail, evidence_tier=tier)


def _attempt(run_id, index):
    return f"{run_id}.{index}"


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _test_id(kind, schedule, index, config):
    return _digest([FORMAT, kind, schedule, index, config])


def _drive(app, lanes, manifest):
    run_id, schedule = manifest["run_id"], manifest["schedule"]
    messages, order = SCHEDULES[schedule]
    app.seed()
    if schedule == "retry":
        # The first delivery really ran and reached the worker hook. Stop it
        # before it can commit; redeliver its durable message as a new attempt.
        first = app.delivery(messages[0], _attempt(run_id, "initial"))
        prior = _Execution(lanes[0].drill, run_id)
        prior.started_ns = lanes[0].started_ns
        prior.adapter = first
        if not prior.baseline() or not prior.hold():
            return
        receipt = first.retryable_failure()
        if receipt is None:
            return
        manifest["retry"] = {"hold": next(e for e in prior.events if e["kind"] == "held"),
                             "receipt": asdict(receipt), "finished_ns": prior.time(), "message": first.message_id()}
        if not _valid_retry(manifest, prior.rules):
            return

    for i, (message, lane) in enumerate(zip(messages, lanes)):
        lane.adapter = app.delivery(message, _attempt(run_id, i))
        if not lane.baseline() or not lane.hold():
            return
        observed = lane.adapter.message_id()
        if observed != message or lane.attempt != _attempt(run_id, i):
            return
        manifest["bindings"].append(observed)
    publication = app.publish()
    publications = [lane.published(publication) for lane in lanes]
    if not all(lane.changed(event) for lane, event in zip(lanes, publications)):
        return
    for i in order:
        lanes[i].finish()


def _valid_retry(manifest, rules):
    retry = manifest["retry"]
    if not isinstance(retry, dict) or set(retry) != {"hold", "receipt", "message", "finished_ns"}:
        return False
    identity = {"run_id": manifest["run_id"], "attempt": _attempt(manifest["run_id"], "initial"),
                "document": rules.document, "revision": rules.old_revision}
    hold = retry["hold"]
    return (retry["message"] == "job-a" and isinstance(hold, dict)
            and type(retry["finished_ns"]) is int and type(hold.get("ns")) is int
            and 0 <= hold["ns"] <= retry["finished_ns"]
            and all(hold.get(k) == v for k, v in {**identity, "kind": "held", "name": rules.checkpoint}.items())
            and retry["receipt"] == {**identity, "outcome": "rejected", "generation": None, "commit": None})


def run_scenario(kind: str, schedule: str, output: str | Path, *,
                 drill: Drill | None = None, factory: Callable[[RunContext], ScenarioApplication] | None = None,
                 revision: str | None = None) -> Verdict:
    """Run a reviewed application factory in a disposable directory, then replay the collected reports."""
    drill = drill or preset(kind)
    drill.validate()
    _check_kind(kind, drill.rules)
    if schedule not in SCHEDULES:
        raise InputError("unknown delivery schedule")
    if factory is None:
        name = drill.factory
        if not name:
            raise InputError("a driven scenario requires an application factory")
        module, attr = name.split(":")
        try:
            factory = getattr(import_module(module), attr)
        except (ImportError, AttributeError):
            raise InputError("could not load the scenario application factory") from None
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    identity = getattr(factory, "__qualname__", type(factory).__qualname__)
    config = _digest([factory.__module__, identity, asdict(drill), revision])
    manifest = {"config": config, "deliveries": [], "format": FORMAT, "version": 1, "kind": kind, "schedule": schedule,
                "run_id": run_id, "bindings": [], "retry": None, "error": None}
    lanes = [_Execution(drill, run_id, test_id=_test_id(kind, schedule, i, config), revision=revision)
             for i in range(len(SCHEDULES[schedule][0]))]
    origin = monotonic_ns()
    for lane in lanes:
        lane.started_ns = origin
    with TemporaryDirectory(prefix="setrya-scenario-") as directory:
        app = None
        try:
            app = factory(RunContext(run_id, Path(directory), drill))
            _drive(app, lanes, manifest)
        except (Exception, KeyboardInterrupt) as error:
            manifest["error"] = type(error).__name__
        finally:
            if app is not None:
                try:
                    app.close()
                except (Exception, KeyboardInterrupt) as error:
                    manifest["error"] = type(error).__name__
            for i, lane in enumerate(lanes):
                report = lane.result().report
                path = output / f"delivery-{i}.json"
                report.save(path)
                manifest["deliveries"].append(sha256(path.read_bytes()).hexdigest())
            save(manifest, output / "scenario.json")
    return replay(output)


def replay(directory: str | Path) -> Verdict:
    directory = Path(directory)
    meta, _ = read_json(directory / "scenario.json", "schedule")
    if isinstance(meta, dict) and meta.get("schedule") == "all":
        mapping(meta, "suite", {"format", "version", "kind", "schedule", "members", "config"})
        if (meta["format"] != FORMAT or type(meta["version"]) is not int or meta["version"] != 1
                or meta["kind"] not in KINDS or not isinstance(meta["members"], dict)
                or set(meta["members"]) != set(SCHEDULES)):
            raise InputError("unknown or incomplete scenario suite")
        results = []
        for schedule in SCHEDULES:
            try:
                child, _ = read_json(directory / schedule / "scenario.json", "schedule")
                if (not isinstance(child, dict) or child.get("config") != meta["config"]
                        or (child.get("kind"), child.get("schedule")) != (meta["kind"], schedule)
                        or _digest(child) != meta["members"][schedule]):
                    raise InputError("wrong member of the schedule suite")
                results.append(_replay_lane_group(directory / schedule, child))
            except (ValueError, OSError, TypeError):
                results.append(Verdict(Status.ERROR, f"Missing or invalid {schedule} evidence"))
        return _combine(results)
    return _replay_lane_group(directory, meta)


def _replay_lane_group(directory, meta):
    mapping(meta, "schedule", {"format", "version", "kind", "schedule", "run_id", "bindings", "retry",
                               "error", "deliveries", "config"})
    if meta["format"] != FORMAT or type(meta["version"]) is not int or meta["version"] != 1 or meta["schedule"] not in SCHEDULES:
        raise InputError("unsupported schedule report")
    schedule = meta["schedule"]
    messages, order = SCHEDULES[schedule]
    run_id = label(meta["run_id"], "run ID")
    if not isinstance(meta["deliveries"], list) or len(meta["deliveries"]) != len(messages):
        raise InputError("wrong number of delivery reports")
    verdicts = []
    reports = {}
    for i in range(len(messages)):
        try:
            expected_digest = meta["deliveries"][i]
            if not isinstance(expected_digest, str):
                raise InputError("missing delivery report digest")
            report = Report.load(directory / f"delivery-{i}.json", expected_sha256=expected_digest)
            identity = _test_id(meta["kind"], schedule, i, meta["config"])
            expected = sha256(f"recorded-test:{identity}".encode()).hexdigest()
            if (report.run_id != run_id or not report.inputs
                    or report.inputs["adapter"] != expected):
                raise InputError("delivery report does not belong to this scenario")
            _check_kind(meta["kind"], report.rules)
            reports[i] = report
            verdicts.append(evaluate(report))
        except (ValueError, OSError, TypeError):
            verdicts.append(Verdict(Status.ERROR, f"Missing or invalid delivery-{i} evidence"))
    if len(reports) != len(messages):
        return _combine(verdicts)
    if any(r.rules != reports[0].rules or r.inputs.get("revision") != reports[0].inputs.get("revision")
           or r.inputs["questions"] != reports[0].inputs["questions"] for r in reports.values()):
        raise InputError("delivery rules or input revisions differ")
    if meta["error"] is not None:
        label(meta["error"], "error type")
        verdicts.append(Verdict(Status.ERROR, f"Adapter raised {meta['error']}"))
    # All journals use one runner clock. Check that every worker captured the old
    # input before the one shared publication and that the observed release order matches.
    if not isinstance(meta["bindings"], list) or meta["bindings"] != list(messages[:len(meta["bindings"])]):
        raise InputError("mismatched message identity")
    if schedule != "retry" and meta["retry"] is not None:
        raise InputError("unexpected retry")
    if ((schedule == "retry" and not _valid_retry(meta, reports[0].rules)) or len(meta["bindings"]) != len(messages)):
        verdicts.append(Verdict(Status.NOT_TESTED, "Missing captured message or retry evidence"))
    held, published, released, fresh, terminal = [], [], [], [], []
    for i, report in reports.items():
        for e in report.events:
            if e["kind"] in ("started", "held", "released", "receipt") and e["attempt"] != _attempt(run_id, i):
                raise InputError("mismatched delivery attempt")
            if (schedule == "retry" and e["kind"] == "started" and _valid_retry(meta, report.rules)
                    and e["ns"] < meta["retry"]["finished_ns"]):
                raise InputError("retry started before the original delivery stopped")
            if e["kind"] == "held":
                held.append(e["ns"])
            elif e["kind"] == "published":
                published.append(e["ns"])
            elif e["kind"] == "released":
                released.append((e["ns"], i))
            elif e["kind"] == "receipt":
                terminal.append((e["ns"], i))
            elif e.get("stage") == "freshness":
                fresh.append(e["ns"])
    if (held and published and max(held) > min(published)) or (released and fresh and min(released)[0] < max(fresh)):
        raise InputError("delivery events cross the declared publication boundary")
    if [i for _, i in sorted(released)] != list(order[:len(released)]):
        raise InputError("delivery release order does not match schedule")
    if not all(any(j == order[i] and ns <= release for ns, j in terminal)
               for i, (release, _) in enumerate(sorted(released)[1:])):
        verdicts.append(Verdict(Status.NOT_TESTED, "Missing terminal receipt before the next release"))
    return _combine(verdicts)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya scenario")
    sub = parser.add_subparsers(dest="kind", required=True)
    sub.add_parser("replay").add_argument("directory", type=Path)
    for kind in KINDS:
        arg = sub.add_parser(kind)
        arg.add_argument("--output", required=True, type=Path)
        arg.add_argument("--schedule", choices=(*SCHEDULES, "all"), default="single")
        arg.add_argument("--drill", type=Path)
        arg.add_argument("--adapter")
        arg.add_argument("--revision")
        arg.add_argument("--mode", choices=("fixed", "broken"), default="fixed")
    args = parser.parse_args(argv)
    try:
        if args.kind == "replay":
            verdict = replay(args.directory)
        else:
            drill = Drill.load(args.drill) if args.drill else preset(args.kind, args.mode)
            if args.adapter:
                drill = replace(drill, factory=args.adapter)
            if args.schedule == "all":
                args.output.mkdir(parents=True)
                members = {}
                config = None
                for schedule in SCHEDULES:
                    result = run_scenario(args.kind, schedule, args.output / schedule, drill=drill, revision=args.revision)
                    child, _ = read_json(args.output / schedule / "scenario.json", "schedule")
                    members[schedule] = _digest(child)
                    config = child["config"]
                    print(f"{schedule}  {result.status.label}  {result.detail}")
                save({"format": FORMAT, "version": 1, "kind": args.kind, "schedule": "all",
                      "members": members, "config": config}, args.output / "scenario.json")
                verdict = replay(args.output)
            else:
                verdict = run_scenario(args.kind, args.schedule, args.output, drill=drill, revision=args.revision)
        print(f"{verdict.status.label}  {verdict.detail}")
        return verdict.status.exit_code
    except (ValueError, OSError, TypeError) as error:
        print(f"ERROR  Invalid schedule, adapter configuration, or evidence ({type(error).__name__}).", file=sys.stderr)
        return Status.ERROR.exit_code
