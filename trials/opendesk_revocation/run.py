"""Remove one agent from an active OpenDesk team while its summary job is in flight."""

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from setrya.adapter import Checkpoint, Publication, ReadView, Receipt, SearchResult
from setrya.checker import Status
from setrya.drill import Drill
from setrya.examples.opendesk.run import API, HERE as ASSETS, READ_ERRORS, Stack, account, start_verified, verified_owner
from setrya.recorder import Recorder
from setrya.review import review
from setrya.store_signals import postgres_wal

from .policy import policies
from .provider import MARKER, SEED, UPDATED

HERE = Path(__file__).resolve().parent
QUERY = "setryahandoff"
OBSERVER = "trial_observer"
PROVIDER_CLIENT = """
import json, sys, urllib.request
action = sys.argv[1]
request = urllib.request.Request("http://localhost:8000/trial/" + action,
                                 method="POST" if action == "release" else "GET")
with urllib.request.urlopen(request, timeout=3) as response:
    print(response.read().decode())
"""


def control(stack, action):
    return json.loads(stack.compose("exec", "-T", "model", "python", "-c", PROVIDER_CLIENT, action, capture=True))


def setup(stack, api, private):
    owner = verified_owner(api)
    workspace = str(UUID(api.mutate("/workspaces", {"name": "Copper desk"}, owner)["id"]))
    actors, members = {}, {}
    for role in ("departing", "colleague"):
        email = role + "@example.test"
        token, _ = account(api, email, role.title())
        invitation = api.mutate("/invitations", {"email": email, "role": "agent"}, owner, workspace)
        link = api.mutate("/invitations/" + invitation["id"] + "/link", {}, owner, workspace)
        invite = parse_qs(urlsplit(link["url"]).query)["token"][0]
        api.mutate("/invitations/accept", {"token": invite}, token)
        actors[role] = token
        user_id = str(UUID(stack.sql("opendesk", f"SELECT id FROM users WHERE email='{email}';")))
        members[role] = {"user_id": user_id, "role": "lead" if role == "colleague" else "member"}
    team = api.mutate("/teams", {"name": "Support", "private": private, "members": list(members.values())},
                      owner, workspace)
    team_id = str(UUID(team["id"]))
    if team["private"] != private or set(team["member_ids"]) != {row["user_id"] for row in members.values()}:
        raise RuntimeError("OpenDesk did not apply the requested team policy and membership")
    tickets = {}
    for name in ("team", "public"):
        data = {"subject": f"{QUERY} {name}", "channel": "api", "kind": "reply", "direction": "inbound",
                "contact": {"name": "Demo", "email": name + "@example.test"},
                "body_html": "<p>Track the copper compass.</p>"}
        if name == "team":
            data["team_id"] = team_id
        ticket = api.mutate("/tickets", data, owner, workspace)
        tickets[name] = str(UUID(ticket["id"]))
        if ticket.get("team_id") != data.get("team_id"):
            raise RuntimeError("OpenDesk did not apply the requested ticket team")
    summary = api.mutate("/tickets/" + tickets["team"] + "/ai/summary", {}, actors["colleague"], workspace)
    if summary.get("summary") != SEED:
        raise RuntimeError("OpenDesk did not generate the initial summary")
    return owner, workspace, team_id, actors, members, tickets


def classify(status, body, team, public, witness, *, retained=False):
    """Only a complete native ticket result with recognized fixture identities can prove absence."""
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("tickets"), list):
        return (None,), "unknown", False
    markers, summaries = [], []
    seen = False
    # The native search and drill both have a depth of five. Preserve the rank
    # of known hits even when another row has an unexpected shape or identity.
    for row in body["tickets"][:5]:
        identity = row.get("id") if isinstance(row, dict) else None
        marker = None
        if identity == public:
            marker = "public-control"
            seen |= witness is not None and row.get("subject") == witness
        elif identity == team:
            custom = row.get("custom")
            field = custom.get("ai_summary") if isinstance(custom, dict) else None
            text = field.get("text") if isinstance(field, dict) else None
            summary = {SEED: "seeded", UPDATED: "recomputed"}.get(text) if isinstance(text, str) else None
            if summary is not None:
                summaries.append(summary)
                marker = "team-summary"
                if retained:
                    expected = "recomputed" if witness is not None else "seeded"
                    marker = "retained-expected-summary" if summary == expected else f"unexpected-{summary}-summary"
        markers.append(marker if marker not in markers else None)
    if summaries:
        summary = summaries[0] if all(item == summaries[0] for item in summaries) else "unknown"
    else:
        summary = "unknown" if None in markers else "absent"
    return tuple(markers), summary, seen


def wait_for(predicate, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.15)
    return False


def exercise(stack, rec, drill, manifest, log):
    api = API(stack)
    owner, ws, team, actors, members, ids = setup(stack, api, manifest["private"])
    target, public = ids["team"], ids["public"]
    stack.sql("opendesk", f"""CREATE ROLE {OBSERVER} LOGIN;
        GRANT SELECT (id, custom) ON tickets TO {OBSERVER};
        GRANT SELECT (id, state, attempt, kind, args) ON river_job TO {OBSERVER};""")
    rec.seeded()
    position = witness = None
    phase = "baseline"

    def search(probe):
        scope = probe.rules.scope
        status, value = api.request("GET", "/search?" + urlencode({"q": probe.question}),
                                    token=actors[scope], workspace=ws, allowed=None)
        markers, summary, seen = classify(status, value, target, public, witness, retained=scope == "colleague")
        log["searches"].append({"phase": phase, "caller": scope, "http": status, "summary": summary,
                                "public_control": "public-control" in markers, "write_witness": seen})
        view = ReadView(rec.run_id, position, probe.rules.route, scope) if seen else None
        return SearchResult(markers, view=view, route=probe.rules.route, scope=scope)

    def all_searches():
        for probe in drill.probes:
            rec.search(probe.rules.id, search)

    all_searches()
    if any(row["summary"] != "seeded" or not row["public_control"] for row in log["searches"]):
        return
    # Create the native worker's input via the application. Seed a single River delivery
    # directly to avoid the periodic scheduler's two-minute debounce and five-minute tick.
    for note in ("Receipt attached.", "Inspection booked.", "Checklist complete.", MARKER):
        api.mutate("/tickets/" + target + "/messages", {"kind": "reply", "body_html": f"<p>{note}</p>"}, actors["colleague"], ws)
    stack.sql("opendesk", f"""UPDATE workspaces SET settings = jsonb_set(settings, '{{ai}}',
        COALESCE(settings->'ai','{{}}') || '{{"enabled":true,"auto_summarize":true}}'::jsonb)
        WHERE id = '{ws}';""")
    args = json.dumps({"workspace_id": ws, "ticket_id": target})
    job = int(stack.sql("opendesk", f"""INSERT INTO river_job (kind, args, max_attempts)
        VALUES ('ai.summarize', '{args}'::jsonb, 1) RETURNING id;"""))
    attempt = f"river-{job}"
    rec.started(attempt)

    def job_in(state):
        # Require exactly one matching delivery. A second job would make the provider call ambiguous.
        return stack.sql(OBSERVER, f"""SELECT count(*) = 1 AND
            bool_and(id={job} AND state='{state}' AND attempt=1) FROM river_job
            WHERE kind='ai.summarize' AND args='{args}'::jsonb;""") == "t"

    try:
        def held():
            return control(stack, "state") == {"calls": 1, "released": False} and job_in("running")

        if not wait_for(held, 35):
            return
        log.update(job_id=job, worker_held=True)
        rules = drill.rules
        rec.held(Checkpoint(rec.run_id, attempt, rules.document, rules.old_revision, rules.checkpoint))
        team_state = api.request("PATCH", "/teams/" + team, {"members": [members["colleague"]]}, owner, ws)[1]
        if team_state.get("member_ids") != [members["colleague"]["user_id"]] or team_state["private"] != manifest["private"]:
            return
        if not held():
            return
        rec.published(Publication(rec.run_id, rules.document, rules.new_revision))
        phase = "membership-removed-worker-held"
        deadline = time.monotonic() + rules.freshness_ms / 1000
        while True:
            started = time.monotonic()
            result = rec.search(rules.freshness_probe, search)
            if result.passages == ("public-control",) or started > deadline:
                break
            time.sleep(rules.sample_ms / 1000)
        all_searches()
        if not held():
            return
        rec.released(attempt)
    finally:
        control(stack, "release")

    if not wait_for(lambda: job_in("completed"), 35) or control(stack, "state") != {"calls": 1, "released": True}:
        return
    raw = stack.sql(OBSERVER, f"""SELECT custom->'ai_summary'->>'text' FROM tickets WHERE id='{target}';""")
    if raw != UPDATED:
        return
    log["worker_completed_with_new_summary"] = True
    # A later public write acts as a causal marker inside the very same ticket SELECT
    # that returns the caller's filtered results, including a successful negative.
    lsn = stack.sql(OBSERVER, "SELECT pg_current_wal_insert_lsn();")
    position = postgres_wal(lsn, generation=rec.run_id).commit
    rec.receipt(Receipt(rec.run_id, attempt, rules.document, rules.old_revision, "committed", rec.run_id, position))
    witness = f"{QUERY} public {rec.run_id}"
    api.request("PATCH", "/tickets/" + public, {"subject": witness}, owner, ws)
    phase = "worker-completed"
    all_searches()


def run_lane(out, name, manifest, rev):
    drill = Drill.load(HERE / "drill.toml")
    log = {"deployment_revision": rev, "manifest": manifest, "worker_held": False,
           "worker_completed_with_new_summary": False, "searches": []}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for filename in ("Dockerfile.trial", "compose.yaml", "transport.py"):
            shutil.copyfile(ASSETS / filename, root / filename)
        shutil.copyfile(HERE / "provider.py", root / "provider.py")
        stack = Stack(root)
        try:
            with Recorder(drill, test_id="opendesk-team-revocation", revision=rev, report=out / f"{name}.json") as rec:
                try:
                    log["target"] = start_verified(stack)
                    exercise(stack, rec, drill, manifest, log)
                finally:
                    try:
                        stack.compose("down", "--volumes", "--remove-orphans", "--rmi", "local")
                    except READ_ERRORS:
                        log["cleanup_project"] = stack.project
                        raise
        except READ_ERRORS as error:
            # The recorder saved the partial journal; the other deployment runs independently.
            log["trial_error"] = type(error).__name__
        finally:
            (out / f"{name}-observations.json").write_text(json.dumps(log, indent=2) + "\n")
    return rec.result, log


def native_result(log):
    """Judge the actual caller responses, including a permission that regresses after removal."""
    if not log["worker_held"] or not log["worker_completed_with_new_summary"]:
        return 3, "Missing confirmed worker overlap or completion."
    held = [r for r in log["searches"] if r["phase"] == "membership-removed-worker-held"]
    final = [r for r in log["searches"] if r["phase"] == "worker-completed"]

    def readable(row):
        return row["http"] == 200 and row["public_control"] and row["summary"] in {"seeded", "recomputed", "absent"}

    # A witnessed protected result from the removed caller is decisive even if
    # another caller's request failed or the policy worked before the old write.
    for row in final:
        if (row["caller"] == "departing" and readable(row) and row["write_witness"]
                and row["summary"] != "absent"):
            return 1, "The departing agent retrieved the team summary after the old write."

    callers = {"departing", "colleague"}
    if ({r["caller"] for r in held} != callers or {r["caller"] for r in final} != callers
            or not all(readable(r) for r in held)
            or not all(readable(r) and r["write_witness"] for r in final)):
        return 3, "Missing caller, public control, or write visibility."

    removed = False
    for row in held:
        if row["caller"] == "colleague":
            if row["summary"] != "seeded":
                return 1, "The retained colleague lost the initial summary while the worker was held."
        elif row["summary"] == "absent":
            removed = True
        elif removed:
            return 1, "The team summary reappeared to the departing agent while the worker was held."
    if not removed:
        return 1, "The departing agent could still retrieve the team summary before release."
    if any(r["caller"] == "colleague" and r["summary"] != "recomputed" for r in final):
        return 1, "The retained colleague could not retrieve the recomputed summary after the old write."
    return 0, "Both callers observed the intended membership policy."


def decision(base, base_log, candidate, candidate_log, revisions):
    before_exit, before_detail = native_result(base_log)
    after_exit, after_detail = native_result(candidate_log)
    errors = [f"{name} ({log['trial_error']})" for name, log in (("base", base_log), ("candidate", candidate_log))
              if "trial_error" in log]
    if after_exit == 1 and (candidate.verdict.status is not Status.FAIL or before_exit == 3 or errors):
        return 1, f"FAILURE OBSERVED  Candidate: {after_detail}"
    if errors or 3 in (before_exit, after_exit):
        if candidate.verdict.status is Status.FAIL:
            return 1, f"FAILURE OBSERVED  Candidate event {candidate.verdict.event}: {candidate.verdict.detail}"
        if errors:
            return 2, f"ERROR  Trial operation failed in {', '.join(errors)}. Inspect the saved lane observations."
        detail = before_detail if before_exit == 3 else after_detail
        return 3, f"NOT TESTED  {detail}"
    code, result = review(base.report, candidate.report, base_commit=revisions["base"][0],
                          candidate_commit=revisions["candidate"][0], base_exit=before_exit, candidate_exit=after_exit)
    if code == 0 and not result.startswith("FIX VERIFIED"):
        return 3, "INCONCLUSIVE  Demonstrating this deployment fix requires a failing base and a passing candidate."
    return code, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    out = parser.parse_args().output
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as repo:
        revisions = policies(repo)
    results = {}
    for name, (revision, manifest) in revisions.items():
        results[name] = run_lane(out, name, manifest, revision)
    base, base_log = results["base"]
    candidate, candidate_log = results["candidate"]
    code, result = decision(base, base_log, candidate, candidate_log, revisions)
    (out / "decision.txt").write_text(result + "\n")
    print(result, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
