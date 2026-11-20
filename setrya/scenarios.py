"""Drive several deliveries through one mutation and replay their search reports offline."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from importlib import import_module
from inspect import isclass, isfunction
import json
import re
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic_ns
from typing import Protocol
from uuid import uuid4

from .adapter import Checkpoint, Publication, Receipt, RunContext, SearchResult
from .checker import Status, Verdict, evaluate
from .drill import Drill, InputError, Probe, ProbeRules, Rules, label, mapping
from .evidence import bundle_manifest, Report, git_revision
from .offboarding.inputs import read_json, save
from .runner import _Execution
from .scenario_order import (OrderedRead, ordered_reads, shared_transition_failure, terminal_receipt,
                             validate_publication_write, validate_windows)

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


def _has_revocation_control(rules, *, historical=False):
    primary = next(p for p in rules.probes if p.id == rules.freshness_probe)
    retained = {marker for probe in rules.probes if probe.route == primary.route and probe.scope != primary.scope
                for marker in set(probe.before).intersection(probe.require)}
    if historical:
        return bool(retained.intersection(primary.forbid))
    return set(primary.before) <= retained


def _check_kind(kind, rules, *, historical=False):
    if kind not in KINDS:
        raise InputError("unknown scenario")
    primary = next(p for p in rules.probes if p.id == rules.freshness_probe)
    if primary.absence != (kind != "update"):
        raise InputError("delete and revoke require an absence freshness probe; update requires a replacement")
    if kind == "revoke" and not _has_revocation_control(rules, historical=historical):
        raise InputError("each revoked baseline marker needs a matching still-authorized control")


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
        prior = _Execution(lanes[0].drill, run_id,
                           test_id=_test_id(manifest["kind"], schedule, "initial", manifest["config"]))
        prior.started_ns = lanes[0].started_ns
        prior.adapter = first
        if not prior.baseline() or not prior.hold() or not prior.pending():
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
    if not all(lane.pending() for lane in lanes):
        return
    publication = app.publish()
    publications = [lane.published(publication) for lane in lanes]
    expected_publication = {"run_id": run_id, "document": lanes[0].rules.document, "revision": lanes[0].rules.new_revision}
    if publications[0]["publication"] == expected_publication and hasattr(app, "publication_position"):
        position = app.publication_position(publication)
        if position is not None:
            write = {**asdict(publication), **asdict(position)}
            validate_publication_write(write, run_id, lanes[0].rules)
            manifest["publication_write"] = write
    if not all(lane.changed(event) for lane, event in zip(lanes, publications)):
        return
    for turn, i in enumerate(order):
        if turn:
            # Read through the *still-held* delivery before calling its release
            # hook. A later read can miss a transient restoration by the first.
            for probe in lanes[i].drill.probes:
                lanes[i].probe(probe)
        if not all(lanes[j].pending() for j in order[turn:]):
            return
        manifest["release_starts"].append(lanes[i].time())
        lanes[i].finish()
        if turn + 1 < len(order) and not terminal_receipt(lanes[i].result().report):
            return


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


def _load_factory(name):
    if not name or name.startswith("__main__:"):
        raise InputError("a configured scenario needs an importable module:factory")
    module, attr = name.split(":")
    try:
        factory = getattr(import_module(module), attr)
    except Exception:
        raise InputError("could not load the scenario application factory") from None
    if not callable(factory):
        raise InputError("scenario application factory must be callable")
    return factory


def _config_digest(drill, name, run_id):
    # A local callback can close over state or wrap a different application each
    # run. Bind the deliveries to this run alone when the name cannot identify it.
    try:
        return (_digest([name, asdict(drill)]) if name
                else "local-" + _digest([run_id, asdict(drill)]))
    except (TypeError, ValueError):
        raise InputError("scenario options must contain finite JSON-compatible values") from None


def _injected_entrypoint(factory):
    """Only a named, importable definition identifies an SDK adapter across runs."""
    if not (isfunction(factory) or isclass(factory)) or factory.__module__ == "__main__":
        return None
    module = sys.modules.get(factory.__module__)
    if module is None or getattr(module, factory.__name__, None) is not factory:
        return None
    if factory.__qualname__ != factory.__name__:
        return None
    return f"{factory.__module__}:{factory.__name__}"


def run_scenario(kind: str, schedule: str, output: str | Path, *,
                 drill: Drill | None = None, factory: Callable[[RunContext], ScenarioApplication] | None = None,
                 revision: str | None = None) -> Verdict:
    """Run a reviewed application factory in a disposable directory, then replay the collected reports."""
    output = Path(output).absolute()
    drill = drill or preset(kind)
    drill.validate()
    _check_kind(kind, drill.rules)
    if schedule not in SCHEDULES:
        raise InputError("unknown delivery schedule")
    if revision is not None:
        git_revision(revision)
    if factory is None:
        name = drill.factory
        factory = _load_factory(name)
    else:
        if not callable(factory):
            raise InputError("scenario application factory must be callable")
        name = _injected_entrypoint(factory)
    run_id = uuid4().hex
    config = _config_digest(drill, name, run_id)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": FORMAT, "version": 3, "kind": kind, "schedule": schedule, "config": config,
        "revision": revision, "run_id": run_id, "release_starts": [], "publication_write": None,
        "bindings": [], "retry": None, "error": None, "deliveries": [],
    }
    lanes = [_Execution(drill, run_id, test_id=_test_id(kind, schedule, i, config), revision=revision)
             for i in range(len(SCHEDULES[schedule][0]))]
    origin = monotonic_ns()
    for lane in lanes:
        lane.started_ns = origin
    with TemporaryDirectory(prefix="setrya-scenario-") as directory:
        app = None
        try:
            app = factory(RunContext(run_id, Path(directory), drill, scenario_kind=kind))
            _drive(app, lanes, manifest)
        except (Exception, KeyboardInterrupt) as error:
            manifest["error"] = type(error).__name__
        finally:
            if app is not None:
                try:
                    app.close()
                except (Exception, KeyboardInterrupt) as error:
                    if manifest["error"] is None or isinstance(error, KeyboardInterrupt):
                        manifest["error"] = type(error).__name__
            for i, lane in enumerate(lanes):
                report = lane.result().report
                path = output / f"delivery-{i}.json"
                report.save(path)
                manifest["deliveries"].append(sha256(path.read_bytes()).hexdigest())
            save(manifest, output / "scenario.json")
    return replay(output)


@dataclass(frozen=True)
class ScenarioMember:
    verdict: Verdict
    reports: dict[int, Report]
    comparison_id: str | None = None
    complete: bool = False
    shared_failure: Verdict | None = None
    ordered: tuple[OrderedRead, ...] = ()


@dataclass(frozen=True)
class ScenarioEvidence:
    kind: str
    schedule: str
    verdict: Verdict
    members: dict[str, ScenarioMember]


def _manifest(meta, keys):
    if not isinstance(meta, dict) or type(meta.get("version")) is not int or meta["version"] not in (1, 2, 3):
        raise InputError("unsupported scenario manifest version")
    version = meta["version"]
    mapping(meta, "scenario", keys | {"format", "version", "kind", "schedule", "config"}
            | ({"revision"} if version >= 2 else set()))
    if meta["format"] != FORMAT or meta["kind"] not in KINDS:
        raise InputError("unsupported scenario manifest")
    if version >= 2 and meta["revision"] is not None:
        git_revision(meta["revision"])
    if not isinstance(meta["config"], str) or not re.fullmatch(
        r"(?:local-)?[0-9a-f]{64}" if version >= 2 else r"[0-9a-f]{64}", meta["config"]
    ):
        raise InputError("invalid scenario configuration fingerprint")


def inspect_scenario(directory: str | Path) -> ScenarioEvidence:
    """Check every bound report once and retain the observations used for review."""
    directory = Path(directory)
    bundle_manifest(directory)
    meta, _ = read_json(directory / "scenario.json", "schedule")
    if not isinstance(meta, dict) or meta.get("schedule") != "all":
        member = _replay_lane_group(directory, meta)
        return ScenarioEvidence(meta["kind"], meta["schedule"], member.verdict, {meta["schedule"]: member})

    _manifest(meta, {"members"})
    if not isinstance(meta["members"], dict) or set(meta["members"]) != set(SCHEDULES):
        raise InputError("unknown or incomplete scenario suite")
    members = {}
    for schedule in SCHEDULES:
        expected = meta["members"][schedule]
        if meta["version"] >= 2 and expected is None:
            members[schedule] = ScenarioMember(Verdict(Status.NOT_TESTED, f"Unfinished schedule: {schedule}"), {})
            continue
        try:
            child, _ = read_json(directory / schedule / "scenario.json", "schedule")
            if (not isinstance(child, dict) or _digest(child) != expected
                    or child.get("schedule") != schedule
                    or any(child.get(k) != meta.get(k) for k in ("kind", "version", "config", "revision"))):
                raise InputError("wrong member of the schedule suite")
            members[schedule] = _replay_lane_group(directory / schedule, child)
        except (ValueError, OSError, TypeError):
            members[schedule] = ScenarioMember(Verdict(Status.ERROR, f"Missing or invalid {schedule} evidence"), {})
    verdict = _combine([m.verdict for m in members.values()])
    if meta["version"] >= 2 and verdict.status == Status.FAIL:
        schedule = next(name for name, m in members.items() if m.verdict == verdict)
        verdict = replace(verdict, detail=f"{schedule} · {verdict.detail}")
    return ScenarioEvidence(meta["kind"], "all", verdict, members)


def replay(directory: str | Path) -> Verdict:
    return inspect_scenario(directory).verdict


def _replay_lane_group(directory, meta):
    interval_format = isinstance(meta, dict) and meta.get("version") == 3
    _manifest(meta, {"run_id", "bindings", "retry", "error", "deliveries"}
              | ({"release_starts", "publication_write"} if interval_format else set()))
    schedule = meta["schedule"]
    if not isinstance(schedule, str) or schedule not in SCHEDULES:
        raise InputError("unsupported schedule report")
    current = meta["version"] >= 2
    comparison_id = meta["config"] if current and not meta["config"].startswith("local-") else None
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
                    or report.inputs["adapter"] != expected
                    or (current and report.inputs.get("revision") != meta["revision"])):
                raise InputError("delivery report does not belong to this scenario")
            _check_kind(meta["kind"], report.rules, historical=True)
            reports[i] = report
            verdict = evaluate(report)
            if current and verdict.status == Status.FAIL:
                verdict = replace(verdict, detail=f"delivery-{i} · {verdict.detail}")
            verdicts.append(verdict)
        except (ValueError, OSError, TypeError):
            verdicts.append(Verdict(Status.ERROR, f"Missing or invalid delivery-{i} evidence"))
    if len(reports) != len(messages):
        return ScenarioMember(_combine(verdicts), reports, comparison_id)
    if any(r.rules != reports[0].rules or r.inputs.get("revision") != reports[0].inputs.get("revision")
           or r.inputs["questions"] != reports[0].inputs["questions"] for r in reports.values()):
        raise InputError("delivery rules or input revisions differ")
    if current and meta["kind"] == "revoke" and not _has_revocation_control(reports[0].rules):
        verdicts.append(Verdict(Status.NOT_TESTED,
                                "Missing still-authorized control for the revoked caller’s baseline passages"))
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
    if (held and published and max(held) > min(published)) or (not interval_format and released and fresh and min(released)[0] < max(fresh)):
        raise InputError("delivery events cross the declared publication boundary")
    if [i for _, i in sorted(released)] != list(order[:len(released)]):
        raise InputError("delivery release order does not match schedule")
    if not all(any(j == order[i] and ns <= release for ns, j in terminal)
               for i, (release, _) in enumerate(sorted(released)[1:])):
        verdicts.append(Verdict(Status.NOT_TESTED, "Missing terminal receipt before the next release"))
    starts = meta.get("release_starts")
    if interval_format:
        problem = validate_windows(reports, order, starts, meta["publication_write"])
        if problem:
            verdicts.append(problem)
    reads = ordered_reads(reports, order, starts, meta.get("publication_write")) if current else ()
    shared = (next((read.failure for read in reads if read.failure is not None), None) if interval_format
              else shared_transition_failure(reports) if current else None)
    if shared:
        verdicts.append(shared)
    complete = (all(v.status in (Status.PASS, Status.FAIL) for v in verdicts)
                and not any(e["kind"] == "adapter_error" for r in reports.values() for e in r.events)
                and len(terminal) == len(messages) and len(released) == len(messages))
    return ScenarioMember(_combine(verdicts), reports, comparison_id, complete, shared, reads)


def _run_suite(kind, output, drill, revision):
    output = Path(output).absolute()
    if revision is not None:
        git_revision(revision)
    drill.validate()
    _check_kind(kind, drill.rules)
    _load_factory(drill.factory)
    config = _config_digest(drill, drill.factory, None)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": FORMAT, "version": 3, "kind": kind, "schedule": "all",
        "revision": revision, "config": config, "members": dict.fromkeys(SCHEDULES),
    }
    path = output / "scenario.json"
    save(manifest, path)
    for schedule in SCHEDULES:
        result = run_scenario(kind, schedule, output / schedule, drill=drill, revision=revision)
        child, _ = read_json(output / schedule / "scenario.json", "schedule")
        if child["revision"] != revision or child["config"] != config:
            raise InputError("suite member has a different revision or configuration")
        manifest["members"][schedule] = _digest(child)
        save(manifest, path)
        print(f"{schedule}  {result.status.label}  {result.detail}", flush=True)
        if child["error"] == "KeyboardInterrupt":
            break
    return replay(output)


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
        arg.add_argument("--mode", choices=("fixed", "broken"), help="override the bundled SQLite worker mode")
    args = parser.parse_args(argv)
    try:
        if args.kind == "replay":
            verdict = replay(args.directory)
        else:
            drill = Drill.load(args.drill) if args.drill else preset(args.kind)
            if args.adapter:
                drill = replace(drill, factory=args.adapter)
            if args.mode is not None:
                if drill.factory != FIXTURE:
                    print("ERROR  --mode applies only to the bundled SQLite worker. "
                          "Configure your adapter in the drill options.", file=sys.stderr)
                    return Status.ERROR.exit_code
                drill = replace(drill, options={**drill.options, "mode": args.mode})
            if args.schedule == "all":
                verdict = _run_suite(args.kind, args.output, drill, args.revision)
            else:
                verdict = run_scenario(args.kind, args.schedule, args.output, drill=drill, revision=args.revision)
        print(f"{verdict.status.label}  {verdict.detail}")
        return verdict.status.exit_code
    except (ValueError, OSError, TypeError) as error:
        print(f"ERROR  Invalid schedule, adapter configuration, or evidence ({type(error).__name__}).", file=sys.stderr)
        return Status.ERROR.exit_code
