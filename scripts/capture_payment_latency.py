"""Manually invoked capture of the payment-latency scenarios (S1, S2, S3).

Not run by pytest, never run by CI, calls no model. Live mode only reads the running Compose
stack; offline mode rebuilds from raw responses already on disk. See docs/manual-evaluation.md
("Capturing scenarios") for the full manual procedure, including saving the fault-injection
evidence this script requires.

    # live capture (needs the injection evidence saved by the manual steps)
    .venv/Scripts/python.exe scripts/capture_payment_latency.py capture --out captures/<name> \\
        --injection-evidence captures/evidence --model <model> --provider anthropic

    # re-derive and verify a written capture (labels, hashes, raw rebuild, evidence, registration)
    .venv/Scripts/python.exe scripts/capture_payment_latency.py verify captures/<name>

    # mark the manifest frozen (only after verify passes and every freeze condition holds)
    .venv/Scripts/python.exe scripts/capture_payment_latency.py verify captures/<name> --freeze

    # offline smoke test from raw files (smoke_test_only; can never be frozen)
    .venv/Scripts/python.exe scripts/capture_payment_latency.py capture --out <dir> \\
        --offline-raw <dir with prometheus_full.json, prometheus_restricted.json, jaeger.json> \\
        --window-end 2026-09-30T07:16:00Z

Live mode makes exactly three GETs, the same requests `run_correlation` makes: the full latency
query and the restricted one to Prometheus (`/api/v1/query`, evaluated at the window end) and one
Jaeger `/api/traces` for service gateway. The raw response text is saved unmodified; S1 and S2 are
then built from that saved text by the unchanged `run_correlation`, so they share one Jaeger
response. It also reads payment's `GET /admin/fault` once, for the record.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from shared.correlation.fetch import fetch_jaeger_spans, fetch_prometheus_vector
from shared.correlation.queries import LATENCY_METRIC
from shared.evaluation import gitstate
from shared.evaluation import scenarios as sc

JAEGER_LIMIT = 100  # fetch_jaeger_spans' default; recorded because it silently truncates


_git_state = gitstate.git_state
_code_unchanged_since = gitstate.code_unchanged_since


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _record_live(args: argparse.Namespace, start: datetime, end: datetime) -> dict[str, str]:
    """Make the three GETs and return the raw response text keyed by `RAW_FILES` name."""
    q = sc.queries()
    records: list[tuple[str, str | None, str]] = []

    def hook(response: httpx.Response) -> None:
        response.read()
        request = response.request
        records.append((request.url.path, request.url.params.get("query"), response.text))

    hooks = {"response": [hook]}
    with (
        httpx.Client(base_url=args.prometheus, event_hooks=hooks, timeout=30) as prom,
        httpx.Client(base_url=args.jaeger, event_hooks=hooks, timeout=30) as jaeger,
    ):
        fetch_prometheus_vector(prom, q["full"], LATENCY_METRIC, at=end)
        fetch_prometheus_vector(prom, q["restricted"], LATENCY_METRIC, at=end)
        fetch_jaeger_spans(jaeger, sc.TRACE_SERVICE, start, end, limit=JAEGER_LIMIT)
    by_query = {query: text for path, query, text in records if path == "/api/v1/query"}
    traces = [text for path, _, text in records if path == "/api/traces"]
    return {
        "prometheus_full.json": by_query[q["full"]],
        "prometheus_restricted.json": by_query[q["restricted"]],
        "jaeger.json": traces[0],
    }


def _fault_status(args: argparse.Namespace) -> dict | None:
    try:
        response = httpx.get(f"{args.payment_admin}/admin/fault", timeout=10)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"unavailable": str(exc)}


ABORT_ADVICE = """\
ABORTED. No payloads or manifest were written. This output directory now holds the raw responses
of the failed attempt: do not reuse it, capture into a NEW --out directory.
What to do next depends on the cause above:
  - no anomalies / the fault is gone: the fault lasts only duration_seconds (120 s) and has
    probably expired. Re-arm it and save NEW injection evidence (docs/manual-evaluation.md,
    steps 2-3), send traffic again, then capture.
  - missing relationships: Jaeger returns at most 100 gateway traces and health checks crowd out
    the checkout traces. Capture sooner after the traffic.
  - injection evidence problems: fix or regenerate the evidence files.
Clearing the fault ('curl -X DELETE localhost:8002/admin/fault') is safe at any time and does not
depend on this script."""


def cmd_capture(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into the non-empty {out}; use a new --out directory")
        return 2
    smoke = args.offline_raw is not None
    evidence: dict[str, str] | None = None
    fault = None
    try:
        if smoke:
            if not args.window_end:
                print("--offline-raw needs --window-end")
                return 2
            end = sc.parse_iso_utc(args.window_end)
        else:
            end = datetime.now(timezone.utc)
    except ValueError as exc:
        print(f"--window-end: {exc}")
        return 2
    start = end - timedelta(minutes=int(sc.QUERY_WINDOW[:-1]))

    if smoke:
        raw_dir = Path(args.offline_raw)
        raw = {name: (raw_dir / name).read_text(encoding="utf-8") for name in sc.RAW_FILES}
    else:
        if not args.injection_evidence:
            print("a live capture needs --injection-evidence (see docs/manual-evaluation.md)")
            return 2
        evidence = sc.load_evidence(Path(args.injection_evidence))
        _, problems = sc.validate_injection(
            evidence, window_start=start, window_end=end, threshold=args.threshold
        )
        if problems:  # checked before any request so nothing is left behind
            print("injection evidence is not usable, nothing was captured:")
            for problem in problems:
                print(" -", problem)
            return 2
        fault = _fault_status(args)
        print("payment fault status now:", fault)
        raw = _record_live(args, start, end)

    commit, dirty = _git_state(out)
    for name, text in raw.items():
        _write(out / "raw" / name, text)
    try:
        jaeger_traces = len(json.loads(raw["jaeger.json"]).get("data", []))
    except ValueError:
        jaeger_traces = None
    try:
        manifest, payloads = sc.build_capture(
            raw=raw,
            window_start=start,
            window_end=end,
            threshold=args.threshold,
            mode="offline_smoke" if smoke else "live",
            evidence=evidence,
            model=args.model,
            provider=args.provider,
            transport_sha256=args.transport_config_sha256,
            git_commit=commit,
            git_dirty=dirty,
            endpoints=None if smoke else {"prometheus": args.prometheus, "jaeger": args.jaeger},
            fault_status_at_capture=fault,
            jaeger_traces=jaeger_traces,
        )
    except ValueError as exc:
        print("capture failed:", exc)
        print(ABORT_ADVICE)
        return 1

    for ref, payload in payloads.items():
        _write(out / ref, sc.canonical_json(payload))
    for name, text in (evidence or {}).items():
        _write(out / "evidence" / name, text)
    _write(out / "manifest.json", sc.canonical_json(manifest))
    for entry in manifest["scenarios"]:
        print(entry["scenario_id"], entry["payload_ref"], entry["expectation"]["expected_status"])
    print(f"wrote {out}/manifest.json (frozen=false). Next: verify it, then freeze.")
    if dirty:
        print("note: the working tree has uncommitted changes, so this capture can never be frozen")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    manifest, payloads, raw, evidence, problems = sc.load_capture(root)
    if manifest is not None:
        problems += sc.verify_manifest(manifest, payloads=payloads, raw=raw, evidence=evidence)
    if problems:
        print("NOT VERIFIED:")
        for problem in problems:
            print(" -", problem)
        return 1
    print("verified: labels, flags, hashes, raw rebuild, injection evidence, registration")
    transport = manifest["registration"]["transport"]
    if transport["provider"] and not transport["verified_by_repo"]:
        print(f"note: transport {transport['provider']!r} cannot be verified by this repository")
    if not args.freeze:
        return 0

    commit, dirty = _git_state(root)
    unchanged = _code_unchanged_since(manifest["registration"]["git_commit"])
    blockers = sc.freeze_blockers(
        manifest, git_commit=commit, git_dirty=dirty, code_unchanged_since_capture=unchanged
    )
    if blockers:
        print("FREEZE REFUSED:")
        for blocker in blockers:
            print(" -", blocker)
        return 1
    manifest["frozen"] = True
    manifest["frozen_at"] = datetime.now(timezone.utc).isoformat()
    manifest["manifest_sha256"] = sc.manifest_digest(manifest)
    _write(root / "manifest.json", sc.canonical_json(manifest))
    print("frozen; manifest_sha256", manifest["manifest_sha256"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--out", required=True)
    cap.add_argument("--prometheus", default="http://localhost:9090")
    cap.add_argument("--jaeger", default="http://localhost:16686")
    cap.add_argument("--payment-admin", default="http://localhost:8002")
    cap.add_argument("--threshold", type=float, default=sc.DEMO_THRESHOLD_MS)
    cap.add_argument("--injection-evidence", help="folder with the saved fault evidence files")
    cap.add_argument("--model", help="model the comparison will use (needed to freeze)")
    cap.add_argument(
        "--provider", help="'anthropic' or 'openai' (hash verified here) or another adapter"
    )
    cap.add_argument(
        "--transport-config-sha256",
        help="for a non-anthropic provider: hash of that adapter's request/schema configuration",
    )
    cap.add_argument("--offline-raw")
    cap.add_argument("--window-end")
    cap.set_defaults(run=cmd_capture)
    ver = sub.add_parser("verify")
    ver.add_argument("dir")
    ver.add_argument("--freeze", action="store_true")
    ver.set_defaults(run=cmd_verify)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
