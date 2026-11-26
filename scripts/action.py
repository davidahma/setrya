"""Run the installed CLI, or update a PR comment using only bounded review signals."""
from collections import Counter
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LABELS = ("PASS", "FAIL", "ERROR", "NOT TESTED")
EXPECTED = {"PASS": {"OPEN_ITEMS", "READY_FOR_REVIEW"}, "FAIL": {"RESIDUAL_FOUND"},
            "ERROR": {"OPEN_ITEMS", "UNAVAILABLE"}, "NOT TESTED": {"OPEN_ITEMS", "UNAVAILABLE"}}
MARKER = "<!-- setrya:receipt -->"
LEGACY_MARKER = "<!-- setrya:erasure-verdict:v1 -->"
COPY_MARKER = "<!-- setrya:copy-sites -->"
COPY_LABELS = {
    "file": ("file write", "file-archive"), "object": ("object storage", "object-storage"),
    "vector": ("vector store", "vector-index"), "cache": ("cache", "redis-answer-cache"),
    "memory": ("agent memory", "agent-memory"), "model-state": ("stateful model", "provider-conversation"),
    "trace": ("trace export", "trace-store"),
    "database": ("database write", "sqlite-records"),
}
COPY_WRITERS = {
    "celery": "Celery task", "rq": "RQ job", "dramatiq": "Dramatiq actor", "asyncio": "asyncio task or callback",
    "retry": "retry decorator", "temporal": "Temporal activity", "langmem": "LangMem reflection",
    "crewai": "CrewAI background memory save",
    "timer": "Node timer callback", "bullmq": "BullMQ worker",
}
COPY_ROWS = 10
COPY_SCAN_ERROR = "error"
COPY_SCAN_ERROR_DETAILS = "**Setrya copy scan · ERROR**\n\nNo copy review is available for this commit. See the workflow run."
# Keep the write-token job independent of the installed package and untrusted checkout.
WRITE_BACK = "Stale write-back: the worker turn at journal entry {entry} wrote a copy captured before deletion."
MAX_JOURNAL_OBSERVATIONS = 16384
# Only these public descriptions can cross from fixture execution into the privileged comment job.
REASONS = {
    "answer": ("The endpoint returned the erased finding", "answers"),
    "control": ("A required retained finding disappeared", "answers"),
    "store": ("A direct store read found a residual or an unexpected change", "stored"),
    "discovered": ("A discovered copy still exists", "discovered"),
    "canary": ("A scanner found the synthetic marker", "scans"),
    "failure": ("A required erasure check failed", None),
    "worker": ("A required worker observation is missing", None),
    "baseline": ("The pre-erasure baseline is incomplete", None),
    "gap": ("A required observation is missing or uncertain", None),
    "provider": ("Provider configuration needs review", None),
    "retention": ("A retention obligation needs review", None),
    "open": ("An external obligation needs review", None),
    "error": ("An adapter reported an error", None),
}
PHASES = {"baseline": "baseline", "immediate": "after erasure", "final": "final read", "case": "case"}

def summary(code, review):
    if code not in ("0", "1", "2", "3") or review not in EXPECTED[LABELS[int(code)]]:
        raise ValueError("invalid receipt summary")
    return f"**Setrya · {LABELS[int(code)]}** (exit {code})  \nCase review: `{review}`."

def signals(receipt):
    counts = Counter()
    case = receipt["case"]
    callers = set(case["callers"])
    stores = {f"{row['store']}/{row['key']}" for row in case["artifacts"]}
    for failure in receipt["failures"]:
        kind = "failure"
        if "recovered the erased finding" in failure:
            kind = "answer"
        elif "lost the retained finding" in failure:
            location = failure.rsplit(" lost the retained finding at ", 1)[0]
            if location in callers and location not in stores:
                kind = "control"
            elif location in stores and location not in callers:
                kind = "store"
        elif failure.startswith("Discovered copy ") and " still exists at " in failure:
            kind = "discovered"
        elif "Canary found in " in failure:
            kind = "canary"
        elif any(fragment in failure for fragment in ("still exists at ", "contains the erased finding", "artifact ")):
            kind = "store"
        phase = next((p for p, label in PHASES.items() if failure.endswith(f" at {label}.")), "case")
        counts[kind, phase] += 1
    for gap in receipt["gaps"]:
        kind = "worker" if "attempt through erasure" in gap else "baseline" if "positive baseline" in gap else "gap"
        counts[kind, "case"] += 1
    for item in receipt["open_items"]:
        kind = {"configuration": "provider", "retained": "retention"}.get(item["kind"], "open")
        counts[kind, "case"] += 1
    if receipt["errors"]:
        counts["error", "case"] = len(receipt["errors"])
    return [[kind, phase, min(n, 9999)] for (kind, phase), n in sorted(counts.items())]

def _details(encoded, code):
    if len(encoded) > 4096:
        raise ValueError("oversized evidence summary")
    rows = json.loads(encoded or "[]")
    if not isinstance(rows, list) or len(rows) > len(REASONS) * len(PHASES) + 1:
        raise ValueError("invalid evidence summary")
    lines, stale_entry = [], None
    for row in rows:
        if type(row) is not list or len(row) != 3:
            raise ValueError("invalid evidence row")
        kind, phase, n = row
        if kind == "write-back":
            if (stale_entry is not None or code != "1" or phase != "case"
                    or type(n) is not int or not 0 <= n < MAX_JOURNAL_OBSERVATIONS):
                raise ValueError("invalid stale write-back signal")
            stale_entry = n
            continue
        if kind not in REASONS or phase not in PHASES or type(n) is not int or not 1 <= n <= 9999:
            raise ValueError("invalid evidence summary")
        label, group = REASONS[kind]
        location = f" (`erasure-evidence.json#/{phase}/{group}`)" if group and phase != "case" else ""
        lines.append(f"- {label} · {PHASES[phase]} ({n}){location}")
    return stale_entry, "\n".join(lines)

def detail_lines(encoded):
    return _details(encoded, None)[1]

def receipt_details(code, review, encoded):
    stale_entry, details = _details(encoded, code)
    lead = WRITE_BACK.format(entry=stale_entry) if stale_entry is not None else ""
    return "\n\n".join(filter(None, (lead, summary(code, review), details)))

def append(variable, message):
    with Path(os.environ[variable]).open("a", encoding="utf-8") as out:
        print(message, file=out)

def require_version():
    import setrya
    source = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(source.read_text(encoding="utf-8"))["project"]["version"]
    if metadata.version("setrya") != expected or setrya.__version__ != expected:
        raise ValueError(f"Install setrya=={expected} for this Action.")

def run():
    folder = Path(os.environ["SETRYA_OUTPUT"]).absolute()
    review, code, rows = "UNAVAILABLE", 2, []
    try:
        require_version()
    except (ImportError, metadata.PackageNotFoundError, ValueError) as exc:
        print(f"Setrya Action requires its matching Python package. {exc}", file=sys.stderr)
    else:
        try:
            from setrya.erasure.cli import run_person_starter
            from setrya.erasure.modes import evidence_mode
            from setrya.receipts import report, stale_write_back_entry
            test_files = [Path(line.strip()) for line in os.environ.get("SETRYA_TEST_FILES", "").splitlines()
                          if line.strip()]
            mode = evidence_mode(os.environ["SETRYA_MODE"])
            code, receipt = run_person_starter(Path(os.environ["SETRYA_STARTER"]), mode, folder,
                                               test_files=test_files)
            if receipt is not None:
                report(receipt, bundle=folder)
                review = receipt["review_status"]
                rows = signals(receipt)
                if code == 1 and (entry := stale_write_back_entry(receipt, folder)) is not None:
                    rows.insert(0, ["write-back", "case", entry])
        except Exception as exc:
            print(f"Setrya action error ({type(exc).__name__}).", file=sys.stderr)
            code, review, rows = 2, "UNAVAILABLE", []
    encoded = json.dumps(rows, separators=(",", ":"))
    append("GITHUB_OUTPUT", f"exit-code={code}\nreview-status={review}\nevidence-summary={encoded}")
    append("GITHUB_STEP_SUMMARY", receipt_details(str(code), review, encoded))
    return code

def conformance():
    code, review = 2, "UNAVAILABLE"
    details = "No signed conformance evidence was produced."
    try:
        require_version()
        from setrya.receipts import exit_code
        from setrya.vendor.cli import run_and_sign
        from setrya.vendor.report import TARGETS, summary as vendor_summary

        target = os.environ["SETRYA_CONFORMANCE_TARGET"]
        key = os.environ["SETRYA_CONFORMANCE_KEY"]
        keyless = os.environ["SETRYA_CONFORMANCE_KEYLESS"]
        if target not in TARGETS or keyless not in ("true", "false") or bool(key) == (keyless == "true"):
            raise ValueError("choose a supported conformance target and one signing policy")
        receipt = run_and_sign(target, Path(os.environ["SETRYA_OUTPUT"]).absolute(),
                               key=Path(key) if key else None,
                               operator=os.getenv("SETRYA_CONFORMANCE_OPERATOR") or None)
        if receipt is not None:
            code, review = exit_code(receipt), receipt["review_status"]
            details = vendor_summary(receipt)
    except Exception as exc:
        print(f"Setrya conformance error ({type(exc).__name__}).", file=sys.stderr)
    append("GITHUB_OUTPUT", f"exit-code={code}\nreview-status={review}\nevidence-summary=[]")
    append("GITHUB_STEP_SUMMARY", "### Setrya vendor conformance\n\n```text\n" + details + "\n```")
    return code

def copy_signals(report):
    pending = [row for row in report["sites"] if row["status"] == "needs-review"]
    pending.sort(key=lambda row: "writer" not in row)
    rows = []
    for row in pending:
        path = quote(row["path"], safe="/._-")
        if len(path) <= 220 and len(rows) < COPY_ROWS:
            item = [path, row["line"], row["kind"], row["site_id"]]
            if "writer" in row:
                item.append(row["writer"])
            rows.append(item)
    signals = [len(pending), rows, report["map_changed"]]
    background = sum("writer" in row for row in pending)
    if "languages" in report:
        signals.extend((background, report["languages"]))
    elif background:
        signals.append(background)
    return signals

def copy_details(encoded):
    if encoded == COPY_SCAN_ERROR:
        return COPY_SCAN_ERROR_DETAILS
    if not encoded or len(encoded) > 4096:
        raise ValueError("invalid copy-site summary")
    data = json.loads(encoded)
    if (type(data) is not list or len(data) not in (3, 4, 5) or type(data[0]) is not int or not 0 <= data[0] <= 512
            or type(data[1]) is not list or len(data[1]) > min(data[0], COPY_ROWS) or type(data[2]) is not bool):
        raise ValueError("invalid copy-site summary")
    count, rows, map_changed = data[:3]
    background = data[3] if len(data) >= 4 else 0
    if type(background) is not int or not 0 <= background <= count:
        raise ValueError("invalid deferred writer count")
    if not count:
        lines = ["**Setrya copy scan · No new copy sites requiring review within scan scope**"]
    else:
        lines = [f"**Setrya copy scan · {count} possible copy site(s) need review**", ""]
    if len(data) == 5:
        scope = data[4]
        if scope not in (["typescript"], ["python", "typescript"]):
            raise ValueError("invalid copy-site language scope")
        lines += [f"Scanned {'Python and TypeScript' if 'python' in scope else 'TypeScript'}.", ""]
    else:
        lines += ["Scanned Python.", ""]
    if background:
        lines += ["This PR adds a potential background writer for customer data. Hold it across a deletion.", ""]
    shown_writers = 0
    for row in rows:
        if type(row) is not list or len(row) not in (4, 5):
            raise ValueError("invalid copy-site row")
        path, line, kind, site_id = row[:4]
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9./_~%+-]{1,220}", path)
                or any(part in ("", ".", "..") for part in path.split("/"))
                or type(line) is not int or not 0 < line <= 524288
                or not isinstance(kind, str) or kind not in COPY_LABELS
                or not isinstance(site_id, str) or not re.fullmatch(r"[0-9a-f]{64}", site_id)):
            raise ValueError("invalid copy-site row")
        detail = f"- `{path}:{line}` · {COPY_LABELS[kind][0]}"
        if len(row) == 5:
            writer = row[4]
            if not isinstance(writer, str) or writer not in COPY_WRITERS:
                raise ValueError("invalid deferred writer")
            shown_writers += 1
            detail += f" · deferred writer ({COPY_WRITERS[writer]})"
        lines.append(detail)
    if shown_writers > background or (count == len(rows) and shown_writers != background):
        raise ValueError("inconsistent deferred writer count")
    if count > len(rows):
        lines += ["", f"See the JSON report for all {count} sites."]
    if map_changed:
        lines += ["", "This PR changes the copy mapping. Review the proposed bindings."]
    if rows:
        test = ("setrya-case/case.yml" if len(rows[0]) == 4 and rows[0][0].endswith((".ts", ".tsx", ".mts", ".cts"))
                else "setrya-case/test_erasure.py")
        starter = {"format": "setrya.copy-site-mapping.v1", "sites": [
            {"site_id": rows[0][3], "store": COPY_LABELS[rows[0][2]][1],
             "key": "replace-with-disposable-test-key", "test": test},
        ]}
        if len(rows[0]) == 5:
            starter["sites"][0]["mode"] = "late-write"
        lines += ["", "Starter mapping for the first site", "", "```json", json.dumps(starter, indent=2), "```"]
    return "\n".join(lines)

def copy_scan():
    require_version()
    from setrya.core.inputs import publish_new
    from setrya.discovery.copy_sites import check

    base, candidate = os.environ["SETRYA_COPY_BASE"], os.environ["SETRYA_COPY_CANDIDATE"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in (base, candidate)):
        raise ValueError("copy-scan requires full base and candidate commit SHAs")
    try:
        report = check(Path(os.environ["SETRYA_COPY_REPO"]), base, candidate,
                       map_path=os.environ["SETRYA_COPY_MAP"], language=os.getenv("SETRYA_COPY_LANGUAGE", "python"))
        publish_new(report, Path(os.environ["SETRYA_COPY_OUTPUT"]))
    except Exception as exc:
        print(f"Setrya copy scan error ({type(exc).__name__}).", file=sys.stderr)
        append("GITHUB_OUTPUT", f"exit-code=2\ncopy-summary={COPY_SCAN_ERROR}")
        append("GITHUB_STEP_SUMMARY", COPY_SCAN_ERROR_DETAILS)
        return 2
    encoded = json.dumps(copy_signals(report), separators=(",", ":"))
    append("GITHUB_OUTPUT", f"exit-code={3 if report['review_required'] else 0}\ncopy-summary={encoded}")
    append("GITHUB_STEP_SUMMARY", copy_details(encoded))
    # A copy site is a review lead. The advisory check can be gated by the caller.
    return 0

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def api(method, path, body=None):
    root = os.environ["GITHUB_API_URL"].rstrip("/")
    url = root + "/" + path
    if urlsplit(root).scheme != "https":
        raise ValueError("expected an HTTPS GitHub API")
    req = Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json",
                           "Content-Type": "application/json", "User-Agent": "setrya-action"})
    with build_opener(NoRedirect()).open(req, timeout=20) as result:
        return json.load(result)

def _pull_request():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ValueError("use the comment operation only on a pull_request event")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    repo = os.environ["GITHUB_REPOSITORY"]
    number, run_id = event["number"], os.environ["GITHUB_RUN_ID"]
    pr = event["pull_request"]
    server = os.environ["GITHUB_SERVER_URL"].rstrip("/")
    if (type(number) is not int or number < 1 or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII)
            or not re.fullmatch(r"[0-9]+", run_id) or pr["base"]["repo"]["full_name"] != repo
            or not re.fullmatch(r"[0-9a-f]{40}", pr["head"]["sha"]) or urlsplit(server).scheme != "https"):
        raise ValueError("invalid pull request coordinates")
    run_url = f"{server}/{repo}/actions/runs/{run_id}"
    return repo, number, pr["head"]["sha"], run_url

def _artifact_id():
    artifact = os.environ.get("SETRYA_ARTIFACT", "")
    if artifact and not re.fullmatch(r"[1-9][0-9]{0,20}", artifact):
        raise ValueError("invalid artifact ID")
    return artifact

def comment():
    repo, number, head, run_url = _pull_request()
    review = os.environ["SETRYA_REVIEW"]
    body = MARKER + "\n" + receipt_details(os.environ["SETRYA_CODE"], review, os.environ.get("SETRYA_SUMMARY", ""))
    artifact = _artifact_id()
    body += f"\n\n[Workflow run]({run_url})"
    if review != "UNAVAILABLE":
        if artifact:
            body += f" · [Download the receipt]({run_url}/artifacts/{artifact})"
        body += "\n\nUnzip `setrya-receipt` and run `setrya replay .` inside it."
    return _post_comment(repo, number, head, body, (MARKER, LEGACY_MARKER))

def copy_comment():
    repo, number, head, run_url = _pull_request()
    encoded = os.environ.get("SETRYA_COPY_SUMMARY", "")
    body = COPY_MARKER + "\n" + copy_details(encoded)
    artifact = _artifact_id()
    body += f"\n\n[Workflow run]({run_url})"
    if artifact and encoded != COPY_SCAN_ERROR:
        body += f" · [Download the copy report]({run_url}/artifacts/{artifact})"
    return _post_comment(repo, number, head, body, (COPY_MARKER,))

def _post_comment(repo, number, head, body, markers):
    prefix = f"repos/{repo}"
    path = f"{prefix}/issues/{number}/comments"
    page = 1
    comment_id = None
    while True:
        comments = api("GET", f"{path}?per_page=100&page={page}")
        for row in comments:
            if (row["user"]["login"] == "github-actions[bot]" and row["user"]["type"] == "Bot"
                    and (row.get("body") or "").startswith(tuple(marker + "\n" for marker in markers))):
                comment_id = int(row["id"])
                break
        if comment_id or len(comments) < 100:
            break
        page += 1
    if api("GET", f"{prefix}/pulls/{number}")["head"]["sha"] != head:
        print("Skipped the verdict for a superseded PR commit.")
        return 0
    if comment_id:
        api("PATCH", f"{prefix}/issues/comments/{comment_id}", {"body": body})
    else:
        api("POST", path, {"body": body})
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit({"run": run, "conformance": conformance, "comment": comment,
                          "copy-scan": copy_scan, "copy-comment": copy_comment}[sys.argv[1]]())
    except (KeyError, TypeError, OSError, ValueError) as exc:
        print(f"Setrya action error ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(2)
