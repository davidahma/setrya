"""Read local cache counters from validated answer-drill bundles."""

import argparse
from pathlib import Path
import sys
from threading import Event

from setrya.cache_metrics import CacheMetrics, serve_metrics
from setrya.drill import InputError
from setrya.offboarding.inputs import read_json

from setrya.endpoint_evidence import (
    FORMAT, MANIFEST, PRIVATE_ANSWER, _read_bundle, contract_for, matches_question,
    observation, pending_before_release, private_body, verified_capture,
)


def _late_fill_metrics(directory):
    _, _, report, manifest = _read_bundle(directory)
    contract = contract_for(manifest)
    events = {e["kind"]: e for e in report.events if e["kind"] != "probe"}
    rules = report.rules
    attempt = events.get("started", {}).get("attempt")
    expected = {"run_id": report.run_id, "document": rules.document, "revision": rules.new_revision}
    published = events.get("published", {}).get("publication") == expected
    capture = verified_capture(manifest["worker_capture"], report.run_id, attempt, contract)
    receipt = events.get("receipt", {})
    release = events.get("released", {})
    held = events.get("held", {})
    identity = dict(expected, attempt=attempt, revision=rules.old_revision, name=rules.checkpoint)
    terminal = (attempt is not None and pending_before_release(manifest) and capture and published and manifest["held_after_reads"] == identity
                and all(held.get(k) == v for k, v in identity.items()) and release.get("attempt") == attempt
                and all(receipt.get(k) == identity[k] for k in ("attempt", "run_id", "document", "revision"))
                and receipt["seq"] > release["seq"])
    stats = CacheMetrics()
    if terminal and receipt["outcome"] == "rejected":
        stats.rejected_fill()

    probes = {p.id: p for p in rules.probes}
    reads = (e for e in report.events if e["kind"] == "probe")
    for event, row in zip(reads, manifest["observations"], strict=True):
        probe = probes[event["probe_id"]]
        identified = (matches_question(row, contract)
                      and (row["route"], row["caller"]) == (probe.route, probe.scope))
        scoped = identified and row["status"] == 200
        passages = observation(row, contract).passages
        hit = row["cache_hit"] if scoped and row["cache_hit"] == (row["cache_writer"] is not None) else None
        skipped = row.get("generation_skipped") if hit is not None else None
        if skipped and not hit:
            skipped = None
        stale = None
        baseline = row["stage"] == "baseline"
        after_revoke = row["caller"] == contract.revoked and not baseline and published
        if identified and after_revoke and (PRIVATE_ANSWER in passages or contract.private_source in passages):
            stale = True
        elif scoped and None not in passages and (baseline or published):
            # A clean-looking final read can precede the old write becoming visible.
            resolved = row["stage"] != "final" or (
                terminal and event["seq"] > receipt["seq"] and event["started_ns"] >= receipt["ns"]
                and (receipt["outcome"] == "rejected" or (
                    row["generation"] == receipt["generation"] and row["commit"] is not None
                    and receipt["commit"] is not None and row["commit"] >= receipt["commit"]
                ))
            )
            if resolved and row["body"] == ("public-only" if after_revoke else private_body(contract)):
                stale = False
        stats.observe_request(hit=hit, stale=stale, generation_skipped=skipped)
    return report.run_id, stats


def bundle_metrics(directories):
    """Count every distinct, validated run once per invocation."""
    seen = set()
    snapshots = []
    for directory in directories:
        manifest, _ = read_json(Path(directory) / MANIFEST, "answer cache manifest")
        fmt = manifest.get("format") if isinstance(manifest, dict) else None
        if fmt == FORMAT:
            run_id, stats = _late_fill_metrics(directory)
        elif fmt == "setrya.selective-cache":
            from .selective import read_metrics
            run_id, stats = read_metrics(directory)
        else:
            raise InputError("unsupported cache metrics bundle")
        if run_id in seen:
            raise InputError("duplicate run ID: supply each cache run once")
        seen.add(run_id)
        snapshots.append(stats.snapshot())
    return CacheMetrics.combine(snapshots)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="setrya cache-metrics", description=__doc__)
    parser.add_argument("directory", nargs="+", type=Path, help="saved late-fill or selective cache run")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--prometheus", action="store_true", help="print Prometheus text exposition")
    output.add_argument("--listen", type=int, metavar="PORT", help="serve a run snapshot on loopback until interrupted")
    args = parser.parse_args(argv)
    try:
        metrics = bundle_metrics(args.directory)
        if args.prometheus:
            print(metrics.prometheus(), end="")
        elif args.listen is None:
            print(metrics.snapshot().summary())
        else:
            with serve_metrics(metrics, port=args.listen) as url:
                print(url, flush=True)
                try:
                    Event().wait()
                except KeyboardInterrupt:
                    pass
    except ValueError as error:
        print(f"ERROR  {error}", file=sys.stderr)
        return 2
    except OSError:
        print("ERROR  Could not read the bundle or start the local metrics server.", file=sys.stderr)
        return 2
    return 0
