"""The order-fault scenario family (S4, S5), offline.

The Prometheus vector is the committed 2026-09-30 payment-fault fixture with payment's mean latency
replaced by a healthy value, purely to exercise the order-fault code paths. It is not experiment
data and not a live capture.
"""

import json

import pytest

from shared.evaluation import run_scenario
from shared.evaluation import scenarios as sc
from shared.investigator import build_investigator_input
from shared.investigator.deterministic import DeterministicInvestigator
from tests.test_scenarios import (
    END,
    START,
    _build,
    _evidence,
    _raw,
    _script,
    _text,
    _verify,
)


def _order_vector(drop_payment: bool = False) -> str:
    body = json.loads(_text("prometheus_mean_latency_vector.json"))
    result = []
    for r in body["data"]["result"]:
        if r["metric"]["service"] == "payment":
            if drop_payment:
                continue
            r["value"][1] = "11.5"  # healthy: far below the 500 ms threshold
        result.append(r)
    body["data"]["result"] = result
    return json.dumps(body)


def _order_raw() -> dict[str, str]:
    return {
        **_raw(),
        "prometheus_full.json": _order_vector(),
        "prometheus_restricted.json": _order_vector(drop_payment=True),
    }


def _order_build(**over):
    args = {"raw": _order_raw(), "family": "order", "model": "m", "provider": "openai"}
    args.update(over)
    return _build(**args)


def _entry(manifest, sid):
    return next(e for e in manifest["scenarios"] if e["scenario_id"] == sid)


def test_an_order_capture_has_an_identified_and_an_undetermined_scenario():
    manifest, payloads = _order_build(evidence=_evidence())
    assert manifest["capture"]["family"] == "order"
    assert [e["scenario_id"] for e in manifest["scenarios"]] == ["S4", "S5"]
    s4, s5 = _entry(manifest, "S4"), _entry(manifest, "S5")
    assert s4["expectation"]["expected_status"] == "identified"
    assert s4["expectation"]["expected_origin"] == "order"
    assert s5["expectation"]["expected_status"] == "undetermined"
    assert s5["expectation"]["expected_origin"] is None
    assert manifest["capture"]["injection"]["service"] == "order"
    assert s4["expectation"]["injected_cause"] == "order"
    assert "order's admin port" in manifest["capture"]["injection"]["attribution"]
    assert _verify(manifest, payloads, raw=_order_raw()) == []


def test_labels_never_enter_the_order_payloads():
    manifest, payloads = _order_build(evidence=_evidence())
    for payload in payloads.values():
        text = json.dumps(payload)
        for word in ("injected", "expected", "gold", "identified"):
            assert word not in text


def test_s4_gold_cites_order_and_its_measured_callee_edge():
    manifest, payloads = _order_build(evidence=_evidence())
    s4 = payloads[_entry(manifest, "S4")["payload_ref"]]
    gold = {(g["kind"], g["index"]) for g in _entry(manifest, "S4")["expectation"]["gold_evidence"]}
    assert gold == {
        ("anomaly", next(i for i, a in enumerate(s4["anomalies"]) if a["service"] == "order")),
        (
            "relationship",
            next(
                i
                for i, r in enumerate(s4["relationships"])
                if (r["caller"], r["callee"]) == ("order", "payment")
            ),
        ),
    }


def test_chain_v1_identifies_order_on_s4_and_abstains_on_s5():
    manifest, payloads = _order_build(evidence=_evidence())
    results = {}
    for sid in ("S4", "S5"):
        entry = _entry(manifest, sid)
        from shared.evaluation import ScenarioExpectation

        results[sid] = run_scenario(
            ScenarioExpectation.model_validate(entry["expectation"]),
            build_investigator_input(payloads[entry["payload_ref"]]),
            DeterministicInvestigator(),
        )
    assert results["S4"].outcome == "correct_identification"
    assert results["S4"].matches_injected_cause is True
    assert results["S5"].outcome == "appropriate_abstention"


def test_s4_is_refused_when_payment_is_also_slow_or_unobserved():
    with pytest.raises(ValueError, match="S4"):
        _build(raw=_raw(), family="order", evidence=_evidence())  # payment fault data: 3 anomalies
    raw = {**_order_raw(), "prometheus_full.json": _order_vector(drop_payment=True)}
    with pytest.raises(ValueError, match="S4"):
        _build(raw=raw, family="order", evidence=_evidence())


def test_injection_evidence_is_bound_to_the_family_service():
    evidence = _evidence()
    record, problems = sc.validate_injection(
        evidence, window_start=START, window_end=END, threshold=500.0, service="order"
    )
    assert problems == [] and record["service"] == "order"
    manifest, payloads = _order_build(evidence=evidence)
    manifest["capture"]["injection"]["service"] = "payment"
    assert any("injection record" in p for p in _verify(manifest, payloads, raw=_order_raw()))


def test_an_unknown_family_is_refused_and_old_manifests_default_to_payment():
    with pytest.raises(ValueError, match="family"):
        _build(family="gateway")
    manifest, payloads = _build()
    del manifest["capture"]["family"]  # captures made before the field existed
    assert _verify(manifest, payloads) == []


def test_order_smoke_data_can_never_be_frozen_and_tampered_scenarios_are_caught():
    manifest, payloads = _order_build(mode="offline_smoke", evidence=None)
    assert manifest["smoke_test_only"] is True
    live, live_payloads = _order_build(evidence=_evidence())
    _entry(live, "S4")["expectation"]["expected_origin"] = "payment"
    assert _verify(live, live_payloads, raw=_order_raw())


def test_the_capture_script_builds_and_verifies_an_order_capture(tmp_path, monkeypatch):
    script = _script()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    from datetime import datetime, timedelta, timezone

    for name, text in _evidence(
        armed_at=datetime.now(timezone.utc) - timedelta(seconds=30)
    ).items():
        (evidence / name).write_text(text)
    monkeypatch.setattr(script, "_record_live", lambda args, start, end: _order_raw())
    monkeypatch.setattr(script, "_fault_status", lambda args: {"active": True})
    out = tmp_path / "order-capture"
    code = script.main(
        ["capture", "--out", str(out), "--injection-evidence", str(evidence),
         "--fault-service", "order", "--model", "m", "--provider", "openai"]
    )  # fmt: skip
    assert code == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert [e["scenario_id"] for e in manifest["scenarios"]] == ["S4", "S5"]
    assert script.main(["verify", str(out)]) == 0
