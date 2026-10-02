"""Scenario building, verification, freezing and the capture script, offline.

Real backend fixtures from 2026-09-30 are an offline smoke input only: they are NOT the
Evaluation 7 payloads (those were never saved) and NOT a live capture. The restricted Prometheus
vector is derived from the real one inside these tests, purely to exercise the S2 path. Tests that
build a "live"-mode capture do so from those fixtures plus synthetic injection evidence in a temp
directory, to exercise the code paths; nothing here is, or becomes, experiment data.
"""

import copy
import importlib.util
import json
import re
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared.correlation.queries import mean_latency_query, restricted_mean_latency_query
from shared.evaluation import ScenarioExpectation, gitstate, run_scenario
from shared.evaluation import scenarios as sc
from shared.investigator import build_investigator_input
from shared.investigator.deterministic import DeterministicInvestigator

FIXTURES = Path(__file__).parent / "fixtures" / "backends"
END = datetime(2026, 9, 30, 7, 16, tzinfo=timezone.utc)
START = END - timedelta(minutes=5)
FULL_Q = mean_latency_query("5m")
RESTRICTED_Q = restricted_mean_latency_query("5m", ["gateway", "order"])


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _restricted_vector() -> str:
    body = json.loads(_text("prometheus_mean_latency_vector.json"))
    body["data"]["result"] = [
        r for r in body["data"]["result"] if r["metric"]["service"] in ("gateway", "order")
    ]
    return json.dumps(body)


def _raw() -> dict[str, str]:
    return {
        "prometheus_full.json": _text("prometheus_mean_latency_vector.json"),
        "prometheus_restricted.json": _restricted_vector(),
        "jaeger.json": _text("jaeger_checkout_trace_ok.json"),
    }


def _payload(prometheus_body: str, query: str) -> dict:
    return sc.replay_context(
        prometheus_body=prometheus_body,
        jaeger_body=_text("jaeger_checkout_trace_ok.json"),
        query=query,
        window_start=START,
        window_end=END,
    )


@pytest.fixture
def s1() -> dict:
    return _payload(_text("prometheus_mean_latency_vector.json"), FULL_Q)


@pytest.fixture
def s2() -> dict:
    return _payload(_restricted_vector(), RESTRICTED_Q)


def _evidence(armed_at: datetime = END - timedelta(seconds=90), **over) -> dict[str, str]:
    files = {
        "fault-request.json": {"mode": "latency", "duration_seconds": 120, "latency_ms": 1500},
        "fault-response.json": {"active": True, "mode": "latency", "seconds_remaining": 119.9},
        "fault-readback-before-traffic.json": {
            "active": True,
            "mode": "latency",
            "seconds_remaining": 118.0,
        },
    }
    for name, value in over.items():
        files[name.replace("_", "-") + ".json"] = value
    out = {n: json.dumps(v) if not isinstance(v, str) else v for n, v in files.items()}
    out["fault-armed-at.txt"] = armed_at.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    return out


def _build(mode="live", **over):
    args = {
        "raw": _raw(),
        "window_start": START,
        "window_end": END,
        "threshold": sc.DEMO_THRESHOLD_MS,
        "mode": mode,
        "evidence": _evidence() if mode == "live" else None,
        "model": "test-model",
        "provider": "anthropic",
        "transport_sha256": None,
        "git_commit": "abc123",
        "git_dirty": False,
        "endpoints": None,
        "fault_status_at_capture": None,
        "jaeger_traces": 1,
    }
    args.update(over)
    return sc.build_capture(**args)


def _verify(manifest, payloads, raw=None, evidence=None):
    return sc.verify_manifest(
        manifest,
        payloads=payloads,
        raw=raw if raw is not None else _raw(),
        evidence=evidence if evidence is not None else _evidence(),
    )


# ------------------------------------------------------------------ queries and scenarios


def test_restricted_query_adds_the_service_matcher_to_every_selector_sum_and_count():
    assert RESTRICTED_Q.count('service=~"gateway|order"') == FULL_Q.count("{http_target") == 2
    assert RESTRICTED_Q.replace('service=~"gateway|order",', "") == FULL_Q


def test_s1_is_verified_as_full_telemetry(s1):
    assert sc.verify_s1(s1) == []
    assert sc.verify_s2(s1) != []  # the full context is not a restricted one


def test_s2_is_verified_only_when_payment_is_unobserved(s1, s2):
    assert sc.verify_s2(s2) == []
    assert sc.verify_s1(s2) != []
    assert s2["unobserved_dependencies"][0]["callee"] == "payment"


def test_s3_ablation_removes_only_the_relationships(s1):
    s3 = sc.ablate_relationships(s1)
    assert s3["relationships"] == [] and s1["relationships"] != []  # input not mutated
    assert {k: v for k, v in s3.items() if k != "relationships"} == {
        k: v for k, v in s1.items() if k != "relationships"
    }
    assert sc.verify_s3(s3) == []


def test_ablation_is_refused_when_it_would_not_be_ambiguous(s1):
    one = {**s1, "anomalies": [a for a in s1["anomalies"] if a["service"] == "payment"]}
    assert any("fewer than two" in p for p in sc.verify_s3(sc.ablate_relationships(one)))
    assert any("relationships remain" in p for p in sc.verify_s3(s1))


def test_gold_evidence_is_looked_up_in_the_payload_not_assumed(s2):
    gold = {(r.kind, r.index) for r in sc.gold_s2(s2)}
    order = next(i for i, a in enumerate(s2["anomalies"]) if a["service"] == "order")
    edge = next(i for i, r in enumerate(s2["relationships"]) if r["caller"] == "order")
    assert gold == {("anomaly", order), ("relationship", edge), ("unobserved_dependency", 0)}
    reordered = {**s2, "anomalies": list(reversed(s2["anomalies"]))}
    assert ("anomaly", 0) in {(r.kind, r.index) for r in sc.gold_s2(reordered)}
    with pytest.raises(ValueError, match="no anomaly"):
        sc.gold_s2({**s2, "anomalies": []})


def test_register_builds_three_entries_with_the_decided_labels(s1, s2):
    entries = sc.register(s1, s2, smoke=False, injected_cause="payment")
    s1e, s2e, s3e = (ScenarioExpectation.model_validate(e["expectation"]) for e in entries)
    assert (s1e.injected_cause, s1e.expected_status, s1e.expected_origin) == (
        "payment",
        "unscored",
        None,
    )
    assert s1e.gold_evidence is None
    assert (s2e.expected_status, s2e.expected_origin) == ("undetermined", None)
    assert (s3e.expected_status, s3e.expected_origin) == ("undetermined", None)
    assert [e["primary_comparison"] for e in entries] == [True, True, True]


def test_smoke_entries_never_join_the_primary_comparison_and_have_no_injected_cause(s1, s2):
    for entry in sc.register(s1, s2, smoke=True):
        assert entry["primary_comparison"] is False and entry["smoke_test_only"] is True
        assert entry["kind"].startswith("offline_smoke_")
        assert entry["expectation"]["injected_cause"] is None


def test_register_refuses_contexts_that_do_not_justify_their_labels(s1, s2):
    with pytest.raises(ValueError, match="S2"):
        sc.register(s1, s1, smoke=False)  # S2 not restricted
    with pytest.raises(ValueError, match="S1"):
        sc.register(s2, s2, smoke=False)  # S1 not full telemetry


def test_labels_and_scenario_names_stay_out_of_payloads_and_case_ids(s1, s2):
    entries = sc.register(s1, s2, smoke=False, injected_cause="payment")
    for entry in entries:
        case_id = entry["expectation"]["case_id"]
        assert re.fullmatch(r"c-[0-9a-f]{10}", case_id)
        assert entry["payload_ref"] == f"payloads/{case_id}.json"
    payload_text = " ".join(sc.canonical_json(p) for p in (s1, s2, sc.ablate_relationships(s1)))
    for leaked in ("injected", "expected", "gold", "unscored", "S1", "S2", "S3", "ablat"):
        assert leaked not in payload_text


def test_deterministic_investigator_on_smoke_payloads_matches_the_registered_labels(s1, s2):
    """Offline smoke only: checks the pieces fit together, says nothing about the LLM."""
    entries = sc.register(s1, s2, smoke=True)
    payloads = [s1, s2, sc.ablate_relationships(s1)]
    results = [
        run_scenario(
            ScenarioExpectation.model_validate(e["expectation"]),
            build_investigator_input(p),
            DeterministicInvestigator(),
        )
        for e, p in zip(entries, payloads, strict=True)
    ]
    assert [r.outcome for r in results] == [
        "unscored",
        "appropriate_abstention",
        "appropriate_abstention",
    ]
    assert results[1].evidence_recall == 1.0


# ---------------------------------------------------------------- S1/S2 comparability


def test_fixture_derived_s1_and_s2_are_comparable(s1, s2):
    assert sc.verify_comparable(s1, s2) == []


def test_comparability_tolerates_a_tiny_difference_but_not_a_real_one(s1, s2):
    near, far = copy.deepcopy(s2), copy.deepcopy(s2)
    near["anomalies"][0]["value"] *= 1.005  # within 1%
    far["anomalies"][0]["value"] *= 1.05  # 5%
    assert sc.verify_comparable(s1, near) == []
    assert any("beyond tolerance" in p for p in sc.verify_comparable(s1, far))
    with pytest.raises(ValueError, match="values differ"):
        sc.register(s1, far, smoke=True)


def test_comparability_rejects_other_windows_thresholds_edges_and_extra_anomalies(s1, s2):
    other = {**s2, "window_end": "2030-01-01T00:00:00Z"}
    assert any("window_end" in p for p in sc.verify_comparable(s1, other))
    assert any("relationships" in p for p in sc.verify_comparable(s1, {**s2, "relationships": []}))
    bad_threshold = copy.deepcopy(s2)
    bad_threshold["anomalies"][0]["threshold"] = 1.0
    assert any("thresholds differ" in p for p in sc.verify_comparable(s1, bad_threshold))
    only_in_s2 = {**s1, "anomalies": s1["anomalies"][:1]}
    assert any("S2 only" in p for p in sc.verify_comparable(only_in_s2, s2))


# ---------------------------------------------------------------- timestamps and injection


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-30T07:16:00Z",
        "2026-09-30T07:16:00z",
        "2026-09-30T07:16:00+00:00",
        " 2026-09-30T07:16:00Z\n",
    ],
)
def test_iso_timestamps_with_z_or_offset_parse_on_python_310(text):
    assert sc.parse_iso_utc(text) == END


@pytest.mark.parametrize("text", ["2026-09-30T07:16:00", "yesterday", "", "2026-13-45T00:00:00Z"])
def test_bad_timestamps_give_an_actionable_error(text):
    with pytest.raises(ValueError, match="ISO-8601|UTC offset"):
        sc.parse_iso_utc(text)


def _inject(evidence):
    return sc.validate_injection(
        evidence, window_start=START, window_end=END, threshold=sc.DEMO_THRESHOLD_MS
    )


def test_valid_injection_evidence_yields_a_hashed_record_with_honest_attribution():
    record, problems = _inject(_evidence())
    assert problems == []
    assert (record["service"], record["mode"], record["latency_ms"]) == ("payment", "latency", 1500)
    assert set(record["evidence_sha256"]) == {
        "fault-request.json",
        "fault-response.json",
        "fault-armed-at.txt",
        "fault-readback-before-traffic.json",
    }
    assert (
        "operator-declared" in record["attribution"]
        and "not independent proof" in record["attribution"]
    )


@pytest.mark.parametrize(
    ("evidence", "needle"),
    [
        ({k: v for k, v in _evidence().items() if k != "fault-response.json"}, "missing"),
        (
            _evidence(fault_request={"mode": "error", "duration_seconds": 120, "latency_ms": 1500}),
            "mode",
        ),
        (
            _evidence(
                fault_request={"mode": "latency", "duration_seconds": 120, "latency_ms": 100}
            ),
            "exceed",
        ),
        (
            _evidence(fault_request={"mode": "latency", "duration_seconds": 0, "latency_ms": 1500}),
            "duration",
        ),
        (
            _evidence(fault_response={"active": False, "mode": "latency", "seconds_remaining": 1}),
            "active",
        ),
        (_evidence(fault_response={"active": True, "mode": "latency"}), "seconds_remaining"),
        (_evidence(fault_response="not json"), "not valid JSON"),
        (_evidence(fault_response="[1]"), "not a JSON object"),
        (_evidence(fault_readback_before_traffic={"active": False}), "readback"),
        (_evidence(armed_at=END + timedelta(minutes=1)), "not armed during"),
        (_evidence(armed_at=START - timedelta(minutes=10)), "not armed during"),
        ({**_evidence(), "fault-armed-at.txt": "tomorrow"}, "fault-armed-at"),
    ],
)
def test_unusable_injection_evidence_is_rejected(evidence, needle):
    record, problems = _inject(evidence)
    assert record is None and any(needle in p for p in problems)


# ------------------------------------------------------ build, verify and tamper evidence


def test_a_live_build_verifies_and_derives_the_label_from_the_evidence():
    manifest, payloads = _build()
    assert _verify(manifest, payloads) == []
    assert manifest["capture"]["mode"] == "live" and manifest["smoke_test_only"] is False
    assert {e["expectation"]["injected_cause"] for e in manifest["scenarios"]} == {"payment"}
    assert all(e["primary_comparison"] for e in manifest["scenarios"])
    assert manifest["capture"]["injection"]["armed_at"].startswith("2026-09-30T07:14:30")


def test_a_smoke_build_verifies_but_is_marked_smoke_with_no_injection():
    manifest, payloads = _build("offline_smoke")
    assert _verify(manifest, payloads) == []
    assert manifest["smoke_test_only"] is True and manifest["capture"]["injection"] is None
    assert not any(e["primary_comparison"] for e in manifest["scenarios"])


def test_a_live_build_needs_valid_injection_evidence():
    with pytest.raises(ValueError, match="needs injection evidence"):
        _build(evidence=None)
    with pytest.raises(ValueError, match="injection evidence"):
        _build(
            evidence=_evidence(
                fault_request={"mode": "latency", "duration_seconds": 5, "latency_ms": 1}
            )
        )


def test_a_build_refuses_data_that_does_not_support_the_labels():
    raw = _raw()
    raw["prometheus_full.json"] = _text("prometheus_empty_vector.json")  # no anomalies: no fault
    with pytest.raises(ValueError, match="S1"):
        _build(raw=raw)


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (
            lambda m: m["scenarios"][0]["expectation"].update(
                expected_status="identified", expected_origin="payment"
            ),
            "expectation.expected_status",
        ),
        (
            lambda m: m["scenarios"][1]["expectation"].update(expected_status="unscored"),
            "expectation.expected_status",
        ),
        (
            lambda m: m["scenarios"][2]["expectation"].update(injected_cause="gateway"),
            "expectation.injected_cause",
        ),
        (lambda m: m["scenarios"][1].update(primary_comparison=False), "primary_comparison"),
        (lambda m: m["scenarios"][2].update(kind="live_capture"), "kind"),
        (lambda m: m["scenarios"][0].update(smoke_test_only=True), "smoke_test_only"),
        (
            lambda m: m["scenarios"][1]["expectation"].update(
                gold_evidence=[{"kind": "anomaly", "index": 0}]
            ),
            "gold_evidence",
        ),
        (
            lambda m: m["scenarios"][1].update(rendered_prompt_sha256="0" * 64),
            "rendered_prompt_sha256",
        ),
        (lambda m: m["scenarios"][0].update(payload_sha256="0" * 64), "payload_sha256"),
        (lambda m: m["scenarios"].pop(), "scenarios list"),
        (lambda m: m["capture"]["injection"].update(latency_ms=9999), "injection record"),
        (lambda m: m["capture"]["raw_sha256"].update({"jaeger.json": "0" * 64}), "hash mismatch"),
        (lambda m: m["registration"].update(system_prompt_sha256="0" * 64), "system_prompt_sha256"),
        (
            lambda m: m["registration"].update(deterministic_source_sha256="0" * 64),
            "deterministic_source_sha256",
        ),
        (
            lambda m: m["capture"].update(queries={"full": "x", "restricted": "y"}),
            "registered queries",
        ),
        (lambda m: m.update(smoke_test_only=True), "smoke_test_only disagrees"),
        (lambda m: m["capture"].update(mode="offline_smoke"), "smoke_test_only disagrees"),
        (lambda m: m.update(manifest_version=1), "manifest_version"),
    ],
)
def test_verify_rederives_labels_flags_and_registration_and_rejects_edits(edit, needle):
    manifest, payloads = _build()
    edit(manifest)
    problems = _verify(manifest, payloads)
    assert problems and any(needle in p for p in problems), problems


def test_flipping_the_smoke_flag_cannot_launder_smoke_data():
    manifest, payloads = _build("offline_smoke")
    manifest["smoke_test_only"] = False
    assert any("disagrees with capture.mode" in p for p in _verify(manifest, payloads))
    manifest["capture"]["mode"] = "live"  # now also claims to be live: injection is missing
    problems = _verify(manifest, payloads)
    assert any("no injection record" in p for p in problems)
    assert any("kind" in p or "primary_comparison" in p for p in problems)


def test_verify_detects_payload_raw_and_evidence_tampering():
    manifest, payloads = _build()
    ref = manifest["scenarios"][1]["payload_ref"]
    swapped = {**payloads, ref: {**payloads[ref], "affected_services": ["gateway"]}}
    assert any("not reproducible" in p for p in _verify(manifest, swapped))
    leaked = {**payloads, ref: {**payloads[ref], "expected_status": "undetermined"}}
    assert any("leaked" in p for p in _verify(manifest, leaked))
    assert any("payload file is missing" in p for p in _verify(manifest, {}))

    raw = _raw()
    raw["jaeger.json"] = "{}"
    assert any("jaeger.json: hash mismatch" in p for p in _verify(manifest, payloads, raw))
    assert any("raw/jaeger.json is missing" in p for p in _verify(manifest, payloads, {}))

    forged = _evidence()
    forged["fault-request.json"] = json.dumps(
        {"mode": "latency", "duration_seconds": 120, "latency_ms": 2000}
    )
    assert any("injection" in p for p in _verify(manifest, payloads, evidence=forged))
    assert any("injection evidence missing" in p for p in _verify(manifest, payloads, evidence={}))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("capture"),
        lambda m: m["capture"].pop("raw_sha256"),
        lambda m: m.update(scenarios="nope"),
        lambda m: m["scenarios"][0].pop("expectation"),
        lambda m: m["scenarios"][1]["expectation"].update(expected_status="bogus"),
        lambda m: m["registration"].pop("transport"),
        lambda m: m["capture"].update(window_start="not a time"),
        lambda m: m["capture"].update(threshold_ms="high"),
        lambda m: m.update(scenarios=[None, None, None]),
    ],
)
def test_malformed_manifests_become_problems_not_exceptions(mutate):
    manifest, payloads = _build()
    mutate(manifest)
    problems = _verify(manifest, payloads)
    assert problems and all(isinstance(p, str) for p in problems)


# ------------------------------------------------------------------------- freezing rules


def _freezable():
    manifest, payloads = _build()
    return manifest, payloads


def test_freeze_blockers_cover_smoke_dirty_tree_head_and_unset_registration():
    manifest, _ = _freezable()
    assert sc.freeze_blockers(manifest, git_commit="abc123", git_dirty=False) == []

    smoke, _ = _build("offline_smoke")
    assert any(
        "smoke" in b for b in sc.freeze_blockers(smoke, git_commit="abc123", git_dirty=False)
    )
    assert any(
        "uncommitted" in b
        for b in sc.freeze_blockers(manifest, git_commit="abc123", git_dirty=True)
    )
    assert any(
        "HEAD" in b for b in sc.freeze_blockers(manifest, git_commit="other", git_dirty=False)
    )
    assert any(
        "HEAD" in b for b in sc.freeze_blockers(manifest, git_commit="unavailable", git_dirty=False)
    )

    captured_dirty, _ = _build(git_dirty=True)
    assert any(
        "capture was made with uncommitted" in b
        for b in sc.freeze_blockers(captured_dirty, git_commit="abc123", git_dirty=False)
    )
    no_model, _ = _build(model=None)
    assert any(
        "model" in b for b in sc.freeze_blockers(no_model, git_commit="abc123", git_dirty=False)
    )
    no_transport, _ = _build(provider=None)
    blockers = sc.freeze_blockers(no_transport, git_commit="abc123", git_dirty=False)
    assert any("provider" in b for b in blockers) and any("config_sha256" in b for b in blockers)


def test_manifest_digest_ignores_only_the_freeze_metadata():
    manifest, _ = _freezable()
    base = sc.manifest_digest(manifest)
    frozen = {**manifest, "frozen": True, "frozen_at": "now", "manifest_sha256": "x"}
    assert sc.manifest_digest(frozen) == base
    edited = copy.deepcopy(manifest)
    edited["registration"]["model"] = "other"
    assert sc.manifest_digest(edited) != base


def test_a_frozen_manifest_is_tamper_evident():
    manifest, payloads = _freezable()
    manifest.update(frozen=True, frozen_at="t", manifest_sha256=sc.manifest_digest(manifest))
    assert _verify(manifest, payloads) == []

    for edit in (
        lambda m: m["registration"].update(model="other-model"),
        lambda m: m["scenarios"][1]["expectation"].update(expected_status="unscored"),
        lambda m: m["capture"].update(jaeger_traces_returned=99),
        lambda m: m["scenarios"][0].update(notes="edited"),
    ):
        tampered = copy.deepcopy(manifest)
        edit(tampered)
        assert any("changed after it was frozen" in p for p in _verify(tampered, payloads))

    no_digest = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    assert any("changed after it was frozen" in p for p in _verify(no_digest, payloads))
    unfrozen_with_metadata = {**manifest, "frozen": False}
    assert any("freeze metadata" in p for p in _verify(unfrozen_with_metadata, payloads))


def test_a_smoke_manifest_marked_frozen_is_rejected():
    manifest, payloads = _build("offline_smoke")
    manifest.update(frozen=True, frozen_at="t", manifest_sha256=sc.manifest_digest(manifest))
    assert any("smoke-test manifest is marked frozen" in p for p in _verify(manifest, payloads))


# ------------------------------------------------------------ identity hashes bind artifacts


def _module(tmp_path: Path, source: str, newline: str = "\n") -> types.ModuleType:
    path = tmp_path / "fake_rules.py"
    path.write_bytes(source.replace("\n", newline).encode())
    spec = importlib.util.spec_from_file_location("fake_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(path)
    import linecache

    linecache.checkcache(str(path))
    return module


def test_source_hash_changes_with_the_code_and_not_with_line_endings(tmp_path):
    source = 'RULESET_VERSION = "chain-v1"\n\ndef rule():\n    return 1\n'
    lf = sc.normalized_source_sha256(_module(tmp_path, source))
    crlf = sc.normalized_source_sha256(_module(tmp_path, source, "\r\n"))
    changed = sc.normalized_source_sha256(_module(tmp_path, source.replace("return 1", "return 2")))
    assert lf == crlf and lf != changed


def test_a_rule_code_change_changes_the_registered_hash_even_with_the_same_version(
    tmp_path, monkeypatch
):
    before = sc.registration_hashes()
    source = 'RULESET_VERSION = "chain-v1"\nTHRESHOLD = 1\n'
    original = _module(tmp_path, source)
    monkeypatch.setattr(sc, "deterministic_rules", original)
    base = sc.registration_hashes()
    edited = _module(tmp_path, source.replace("= 1", "= 2"))
    monkeypatch.setattr(sc, "deterministic_rules", edited)
    after = sc.registration_hashes()
    assert base["deterministic_ruleset"] == after["deterministic_ruleset"] == "chain-v1"
    assert base["deterministic_source_sha256"] != after["deterministic_source_sha256"]
    assert before["deterministic_source_sha256"] != base["deterministic_source_sha256"]


def test_the_system_prompt_and_response_schema_are_registered(monkeypatch):
    base = sc.registration_hashes()
    monkeypatch.setattr(sc, "SYSTEM_PROMPT", sc.SYSTEM_PROMPT + " changed")
    assert sc.registration_hashes()["system_prompt_sha256"] != base["system_prompt_sha256"]
    monkeypatch.undo()
    monkeypatch.setattr(sc, "response_schema", lambda: {"type": "object"})
    assert sc.registration_hashes()["response_schema_sha256"] != base["response_schema_sha256"]


def test_a_rendered_prompt_change_changes_the_hash_and_is_caught_by_verify(s1, monkeypatch):
    manifest, payloads = _build()
    base = sc.rendered_prompt_sha256(s1)
    original = sc.build_prompt

    def other_rendering(investigator_input):
        prompt = original(investigator_input)
        return type(prompt)(system=prompt.system, user=prompt.user + "\nextra")

    monkeypatch.setattr(sc, "build_prompt", other_rendering)
    assert sc.rendered_prompt_sha256(s1) != base
    assert any("rendered_prompt_sha256" in p for p in _verify(manifest, payloads))


def test_each_entry_hashes_the_prompt_actually_rendered_for_its_payload(s1, s2):
    entries = sc.register(s1, s2, smoke=True)
    payloads = [s1, s2, sc.ablate_relationships(s1)]
    hashes = [e["rendered_prompt_sha256"] for e in entries]
    assert hashes == [sc.rendered_prompt_sha256(p) for p in payloads]
    assert len(set(hashes)) == 3


def test_a_transport_or_schema_configuration_change_changes_the_fingerprint(monkeypatch):
    from shared.investigator import anthropic as transport

    base = sc.transport_config_sha256(sc.anthropic_transport_fingerprint())
    for name, value in (
        ("MAX_TOKENS", 77),
        ("API_VERSION", "2099-01-01"),
        ("TIMEOUT_SECONDS", 1.0),
    ):
        monkeypatch.setattr(transport, name, value)
        assert sc.transport_config_sha256(sc.anthropic_transport_fingerprint()) != base
        monkeypatch.undo()
    monkeypatch.setattr(transport, "wire_schema", lambda schema: {"type": "object"})
    assert sc.transport_config_sha256(sc.anthropic_transport_fingerprint()) != base


def test_the_adapter_source_is_part_of_the_transport_fingerprint(monkeypatch):
    base = sc.transport_config_sha256(sc.anthropic_transport_fingerprint())
    monkeypatch.setattr(sc.inspect, "getsource", lambda module: "a different adapter source")
    assert sc.transport_config_sha256(sc.anthropic_transport_fingerprint()) != base


def test_a_stale_anthropic_transport_registration_is_caught_by_verify(monkeypatch):
    from shared.investigator import anthropic as transport

    manifest, payloads = _build()
    assert _verify(manifest, payloads) == []
    monkeypatch.setattr(transport, "MAX_TOKENS", 99)
    assert any(
        "Anthropic transport configuration differs" in p for p in _verify(manifest, payloads)
    )


def test_only_the_shipped_adapters_can_be_verified_and_another_provider_is_not_mislabeled():
    anthropic = sc.transport_block("anthropic", None)
    assert anthropic["verified_by_repo"] is True and anthropic["config_sha256"]
    openai = sc.transport_block("openai", None)
    assert openai["verified_by_repo"] is True and openai["config_sha256"]
    assert openai["config_sha256"] != anthropic["config_sha256"]
    other = sc.transport_block("mistral", "f" * 64)
    assert other == {"provider": "mistral", "config_sha256": "f" * 64, "verified_by_repo": False}
    assert sc.transport_block(None, None)["config_sha256"] is None

    manifest, payloads = _build(provider="mistral", transport_sha256="f" * 64)
    assert manifest["registration"]["transport"]["verified_by_repo"] is False
    assert _verify(manifest, payloads) == []
    manifest["registration"]["transport"]["verified_by_repo"] = True
    assert any("only the shipped Anthropic adapter" in p for p in _verify(manifest, payloads))


def test_the_openai_transport_is_registered_verified_and_a_stale_one_is_caught(monkeypatch):
    from shared.investigator import openai as transport

    manifest, payloads = _build(provider="openai", model="gpt-5.6-luna")
    registered = manifest["registration"]["transport"]
    assert registered == sc.transport_block("openai", None)
    assert "hypothesis" in json.dumps(sc.openai_transport_fingerprint())
    assert _verify(manifest, payloads) == []
    monkeypatch.setattr(transport, "MAX_OUTPUT_TOKENS", 99)
    assert any("OpenAI transport configuration differs" in p for p in _verify(manifest, payloads))


def test_the_openai_adapter_source_is_part_of_its_fingerprint(monkeypatch):
    base = sc.transport_config_sha256(sc.openai_transport_fingerprint())
    monkeypatch.setattr(sc.inspect, "getsource", lambda module: "a different adapter source")
    assert sc.transport_config_sha256(sc.openai_transport_fingerprint()) != base


# ------------------------------------------------------------------------- capture script


def _script():
    path = Path(__file__).parents[1] / "scripts" / "capture_payment_latency.py"
    spec = importlib.util.spec_from_file_location("capture_payment_latency", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "raw_in"
    raw.mkdir()
    for name, text in _raw().items():
        (raw / name).write_text(text)
    return raw


def _evidence_dir(tmp_path: Path, armed_at: datetime) -> Path:
    folder = tmp_path / "evidence_in"
    folder.mkdir()
    for name, text in _evidence(armed_at=armed_at).items():
        (folder / name).write_text(text)
    return folder


def test_offline_smoke_capture_verify_and_refused_freeze(tmp_path, capsys):
    script = _script()
    out = tmp_path / "out"
    args = ["capture", "--out", str(out), "--offline-raw", str(_raw_dir(tmp_path))]
    assert script.main([*args, "--window-end", "2026-09-30T07:16:00Z"]) == 0  # Z suffix is fine

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["frozen"] is False and manifest["smoke_test_only"] is True
    assert manifest["capture"]["mode"] == "offline_smoke"
    assert len(list((out / "payloads").glob("c-*.json"))) == 3
    assert script.main(["verify", str(out)]) == 0
    capsys.readouterr()
    assert script.main(["verify", str(out), "--freeze"]) == 1
    assert "smoke-test data can never be frozen" in capsys.readouterr().out
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["frozen"] is False


def test_script_verify_rejects_edited_smoke_labels_and_flag(tmp_path, capsys):
    script = _script()
    out = tmp_path / "out"
    args = ["capture", "--out", str(out), "--offline-raw", str(_raw_dir(tmp_path)), "--window-end"]
    assert script.main([*args, "2026-09-30T07:16:00Z"]) == 0
    path = out / "manifest.json"
    original = json.loads(path.read_text(encoding="utf-8"))

    edited = copy.deepcopy(original)
    edited["scenarios"][0]["expectation"].update(
        expected_status="identified", expected_origin="payment"
    )
    path.write_text(json.dumps(edited))
    assert script.main(["verify", str(out)]) == 1
    assert "expectation.expected_status" in capsys.readouterr().out

    flipped = copy.deepcopy(original)
    flipped["smoke_test_only"] = False
    path.write_text(json.dumps(flipped))
    assert script.main(["verify", str(out), "--freeze"]) == 1
    assert "disagrees with capture.mode" in capsys.readouterr().out
    assert json.loads(path.read_text(encoding="utf-8"))["frozen"] is False

    path.write_text("{not json")
    assert script.main(["verify", str(out)]) == 1
    assert "malformed" in capsys.readouterr().out
    path.write_text(json.dumps({"capture": {"mode": "live"}}))
    assert script.main(["verify", str(out)]) == 1
    assert "malformed manifest" in capsys.readouterr().out
    assert script.main(["verify", str(tmp_path / "nowhere")]) == 1


def test_capture_refuses_a_non_empty_output_and_bad_timestamps(tmp_path, capsys):
    script = _script()
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    refused = ["capture", "--out", str(busy), "--offline-raw", "r", "--window-end", "x"]
    assert script.main(refused) == 2
    assert "new --out" in capsys.readouterr().out

    raw = _raw_dir(tmp_path)
    for bad in ("tomorrow", "2026-09-30T07:16:00"):
        out = tmp_path / ("o" + str(len(bad)))
        code = script.main(
            ["capture", "--out", str(out), "--offline-raw", str(raw), "--window-end", bad]
        )
        assert code == 2
        assert "--window-end" in capsys.readouterr().out
        assert not out.exists()


def test_aborted_capture_keeps_only_raw_and_says_what_to_do(tmp_path, capsys):
    script = _script()
    raw = _raw_dir(tmp_path)
    (raw / "prometheus_full.json").write_text(_text("prometheus_empty_vector.json"))  # no fault
    out = tmp_path / "out2"
    code = script.main(
        ["capture", "--out", str(out), "--offline-raw", str(raw), "--window-end", END.isoformat()]
    )
    text = capsys.readouterr().out
    assert code == 1
    assert not (out / "manifest.json").exists() and not (out / "payloads").exists()
    assert (out / "raw" / "jaeger.json").exists()
    for needle in ("ABORTED", "NEW --out", "Re-arm", "probably expired", "DELETE"):
        assert needle in text


def _live_run(tmp_path, monkeypatch, *, armed_at=None, git=("abc123", False), extra=()):
    """A live-mode capture through the real CLI with the three GETs and Git mocked out."""
    script = _script()
    now = datetime.now(timezone.utc)
    folder = _evidence_dir(tmp_path, armed_at or now - timedelta(seconds=30))
    calls = {"record": 0}

    def fake_record(args, start, end):
        calls["record"] += 1
        return _raw()

    monkeypatch.setattr(script, "_record_live", fake_record)
    monkeypatch.setattr(script, "_fault_status", lambda args: {"active": False})
    monkeypatch.setattr(script, "_git_state", lambda exclude: git)
    out = tmp_path / "live_out"
    code = script.main(
        [
            "capture",
            "--out",
            str(out),
            "--injection-evidence",
            str(folder),
            "--provider",
            "anthropic",
            *extra,
        ]
    )
    return script, out, code, calls


def test_mocked_live_capture_end_to_end_then_freeze_and_tamper_detection(
    tmp_path, monkeypatch, capsys
):
    script, out, code, calls = _live_run(tmp_path, monkeypatch, extra=("--model", "test-model"))
    assert code == 0 and calls["record"] == 1
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capture"]["mode"] == "live" and manifest["capture"]["injection"]
    assert {p.name for p in (out / "evidence").iterdir()} >= set(sc.INJECTION_FILES)
    assert script.main(["verify", str(out)]) == 0

    # Freeze is refused if the tree is dirty or HEAD moved, and leaves the manifest unfrozen.
    for state, needle in ((("abc123", True), "uncommitted"), (("zzz", False), "HEAD is not")):
        monkeypatch.setattr(script, "_git_state", lambda exclude, s=state: s)
        assert script.main(["verify", str(out), "--freeze"]) == 1
        assert needle in capsys.readouterr().out
        assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["frozen"] is False

    monkeypatch.setattr(script, "_git_state", lambda exclude: ("abc123", False))
    assert script.main(["verify", str(out), "--freeze"]) == 0
    frozen = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert frozen["frozen"] is True and frozen["manifest_sha256"]
    assert script.main(["verify", str(out)]) == 0

    tampered = copy.deepcopy(frozen)
    tampered["scenarios"][1]["expectation"]["expected_status"] = "unscored"
    (out / "manifest.json").write_text(json.dumps(tampered))
    assert script.main(["verify", str(out)]) == 1
    assert "changed after it was frozen" in capsys.readouterr().out


def test_a_live_capture_on_a_dirty_tree_is_recorded_as_such_and_can_never_freeze(
    tmp_path, monkeypatch, capsys
):
    script, out, code, _ = _live_run(
        tmp_path, monkeypatch, git=("abc123", True), extra=("--model", "m")
    )
    assert code == 0 and "can never be frozen" in capsys.readouterr().out
    monkeypatch.setattr(script, "_git_state", lambda exclude: ("abc123", False))
    assert script.main(["verify", str(out), "--freeze"]) == 1
    assert "capture was made with uncommitted changes" in capsys.readouterr().out


def test_freeze_needs_the_model_and_transport_set(tmp_path, monkeypatch, capsys):
    script, out, code, _ = _live_run(tmp_path, monkeypatch)  # no --model
    assert code == 0
    assert script.main(["verify", str(out), "--freeze"]) == 1
    assert "registration.model is not set" in capsys.readouterr().out


def test_live_capture_needs_valid_injection_evidence_before_any_request(
    tmp_path, monkeypatch, capsys
):
    script = _script()
    monkeypatch.setattr(script, "_record_live", lambda *a: pytest.fail("no request may be made"))
    monkeypatch.setattr(script, "_fault_status", lambda a: pytest.fail("no request may be made"))
    out = tmp_path / "o"
    assert script.main(["capture", "--out", str(out)]) == 2
    assert "--injection-evidence" in capsys.readouterr().out

    stale = _evidence_dir(tmp_path, datetime.now(timezone.utc) - timedelta(hours=2))
    assert script.main(["capture", "--out", str(out), "--injection-evidence", str(stale)]) == 2
    assert "not armed during" in capsys.readouterr().out
    assert not out.exists()  # nothing was written


def test_git_state_ignores_the_capture_folder_and_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "elsewhere" / "c1").mkdir(parents=True)
    outputs = {
        ("rev-parse", "--show-toplevel"): str(root) + "\n",
        ("rev-parse", "HEAD"): "deadbeef\n",
        ("status", "--porcelain", "--untracked-files=all"): "?? elsewhere/c1/manifest.json\n",
    }
    monkeypatch.setattr(gitstate, "_git_output", lambda *a: outputs[a])
    assert gitstate.git_state(root / "elsewhere" / "c1") == ("deadbeef", False)
    assert gitstate.git_state(None) == ("deadbeef", True)
    outputs[("status", "--porcelain", "--untracked-files=all")] += " M shared/pipeline.py\n"
    assert gitstate.git_state(root / "elsewhere" / "c1") == ("deadbeef", True)

    def unavailable(*args):
        raise OSError("no git")

    monkeypatch.setattr(gitstate, "_git_output", unavailable)
    assert gitstate.git_state(None) == ("unavailable", True)


def test_git_state_does_not_count_the_saved_evidence_folder_as_uncommitted_code(
    tmp_path, monkeypatch
):
    """The manual procedure saves evidence under captures/ before capturing into another folder
    there; that untracked data must not make every capture look dirty (and so unfreezable)."""
    root = tmp_path / "repo"
    (root / "captures" / "evidence-1").mkdir(parents=True)
    (root / "captures" / "payment-latency-1").mkdir(parents=True)
    outputs = {
        ("rev-parse", "--show-toplevel"): str(root) + "\n",
        ("rev-parse", "HEAD"): "deadbeef\n",
        ("status", "--porcelain", "--untracked-files=all"): (
            "?? captures/evidence-1/fault-request.json\n"
            "?? captures/payment-latency-1/manifest.json\n"
        ),
    }
    monkeypatch.setattr(gitstate, "_git_output", lambda *a: outputs[a])
    assert gitstate.git_state(root / "captures" / "payment-latency-1") == ("deadbeef", False)
    # Untracked source code is still dirty, wherever else it lives.
    outputs[("status", "--porcelain", "--untracked-files=all")] += "?? shared/evaluation/new.py\n"
    assert gitstate.git_state(root / "captures" / "payment-latency-1") == ("deadbeef", True)
    outputs[("status", "--porcelain", "--untracked-files=all")] = (
        "?? scripts/capture_payment_latency.py\n"
    )
    assert gitstate.git_state(None) == ("deadbeef", True)
    outputs[("status", "--porcelain", "--untracked-files=all")] = " M captures_not/x.py\n"
    assert gitstate.git_state(None) == ("deadbeef", True)  # only the captures/ directory itself


def test_the_real_adapter_request_matches_the_registered_prompt_and_transport_hashes(s1):
    """The hashes must describe the bytes actually sent, not a parallel reconstruction."""
    import httpx

    from shared.investigator import investigate
    from shared.investigator.anthropic import anthropic_complete
    from shared.investigator.llm import LLMInvestigator

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["headers"], sent["body"] = request.headers, json.loads(request.content)
        answer = {
            "status": "undetermined",
            "origin_service": None,
            "root_cause": "x",
            "confidence": 0.5,
            "supporting_evidence": [{"kind": "anomaly", "index": 0}],
        }
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(answer)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    investigate(s1, LLMInvestigator(anthropic_complete(api_key="k", model="m", client=client)))

    body = sent["body"]
    rendered = sc.sha256_text(body["system"] + "\x00" + body["messages"][0]["content"])
    assert rendered == sc.rendered_prompt_sha256(s1)
    assert sc.sha256_text(body["system"]) == sc.registration_hashes()["system_prompt_sha256"]
    fingerprint = sc.anthropic_transport_fingerprint()
    assert body["output_config"]["format"]["schema"] == fingerprint["wire_schema"]
    assert body["max_tokens"] == fingerprint["max_tokens"]
    assert sent["headers"]["anthropic-version"] == fingerprint["api_version"]
    assert set(body) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "output_config",
    }  # no hidden params
    assert body["messages"] == [{"role": "user", "content": body["messages"][0]["content"]}]
