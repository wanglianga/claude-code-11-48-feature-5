"""Calibration fit/validation and center-local standardization tests."""
from __future__ import annotations

import pytest

from app.calibration import build_model, evaluate, fit_line
from center_agent import executor

STD_SPEC = {
    "group_by": ["treatment.path"],
    "metrics": [
        {"type": "count"},
        {"type": "standardized_mean", "variable": "followup.lab_value"},
    ],
}


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

def test_fit_line_recovers_exact_affine_transfer():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [0.0 + 0.5 * x for x in xs]
    line = fit_line(xs, ys)
    assert line["slope"] == pytest.approx(0.5)
    assert line["intercept"] == pytest.approx(0.0)
    assert line["r2"] == pytest.approx(1.0)


def test_fit_line_rejects_unidentified_slope():
    with pytest.raises(ValueError):
        fit_line([2.0, 2.0], [1.0, 3.0])


def test_evaluate_reports_bias_and_relative_error():
    m = evaluate([(10.0, 8.0), (12.0, 10.0)])
    assert m["bias"] == pytest.approx(2.0)
    assert m["max_rel_error"] == pytest.approx(0.25)


def test_center_classification_never_imputes():
    """The center-local classifier is the single source of truth for classes."""
    t = {"enabled": True, "range_low": 1.0, "range_high": 10.0,
         "detection_limit": 0.2, "quantitation_limit": 0.5}
    assert executor._classify(5.0, "2026-03-01", t) == "comparable"
    assert executor._classify(0.1, "2026-03-01", t) == "censored_low"
    assert executor._classify(0.4, "2026-03-01", t) == "censored_low"
    assert executor._classify(0.8, "2026-03-01", t) == "below_range"
    assert executor._classify(12.0, "2026-03-01", t) == "above_range"
    assert executor._classify(5.0, "2026-03-01", {"enabled": False}) == "uncalibrated"


# ---------------------------------------------------------------------------
# Route A / B model building
# ---------------------------------------------------------------------------

class _S:
    def __init__(self, code, kind, measured, ref=None, partner=None, lot="L1",
                 role="train"):
        self.sample_code = code
        self.kind = kind
        self.measured_value = measured
        self.reference_value = ref
        self.partner_value = partner
        self.lot = lot
        self.role = role


class _Assay:
    unit = "mg/L"
    detection_limit = 0.2
    quantitation_limit = 0.2
    upper_reportable = 20.0
    measuring_range = {"low": 0.05, "high": 15.0}


class _Platform:
    traceability = "traceable"


class _BridgePlatform:
    traceability = "bridged"


class _QC:
    def __init__(self, lot, status="passed", end=None):
        self.lot = lot
        self.status = status
        self.observed_cv_pct = 4.0
        self.allowed_cv_pct = 10.0
        self.period_start = "2026-01-01"
        self.period_end = end
        self.note = None


def _traceable_samples(offset=0.8, holdout=True):
    levels = [1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 9.0, 11.0]
    out = []
    for i, truth in enumerate(levels):
        role = "holdout" if (holdout and i in (1, 5)) else "train"
        out.append(_S(f"R{i}", "reference", truth + offset, ref=truth, role=role))
    return out


def test_traceable_route_validates_and_recovers_transfer():
    res = build_model(_Assay(), _Platform(), _traceable_samples(), [_QC("L1")])
    assert res["status"] == "validated"
    assert res["transform"]["enabled"] is True
    assert res["transform"]["slope"] == pytest.approx(1.0)
    assert res["transform"]["intercept"] == pytest.approx(-0.8)
    # standardized bias shrinks vs the raw offset bias
    assert abs(res["diagnostics"]["standardized_bias"]) < abs(
        res["diagnostics"]["raw_bias"])


def test_traceable_route_without_holdout_is_rejected():
    res = build_model(_Assay(), _Platform(), _traceable_samples(holdout=False),
                      [_QC("L1")])
    assert res["status"] == "rejected"
    assert any("holdout" in r for r in res["diagnostics"]["reasons"])
    assert res["transform"]["enabled"] is False


def test_traceable_route_requires_common_reference_material():
    res = build_model(_Assay(), _BridgePlatform(),
                      _traceable_samples(), [_QC("L1")], forced_route="traceable")
    assert res["status"] == "rejected"
    assert "platform_not_traceable_to_reference_material" in res["diagnostics"]["reasons"]


def test_qc_failed_lot_samples_are_excluded_from_fit():
    samples = _traceable_samples()
    # poison one lot with wild outliers; QC marks that lot failed
    bad = [_S(f"BAD{i}", "reference", truth + 40.0, ref=truth, lot="BAD")
           for i, truth in enumerate([2.0, 4.0, 6.0, 8.0, 10.0])]
    res = build_model(_Assay(), _Platform(), samples + bad,
                      [_QC("L1"), _QC("BAD", status="failed")])
    assert res["status"] == "validated"
    assert "BAD" in res["diagnostics"]["failed_lots"]
    assert res["diagnostics"]["n_excluded_lot_samples"] == 5
    # excluded lot must not distort the fitted intercept
    assert res["transform"]["intercept"] == pytest.approx(-0.8, abs=0.01)


def test_bridge_route_requires_two_lots():
    samples = [_S(f"B{i}", "bridge", x, partner=x - 0.6, lot="ONLY")
               for i, x in enumerate([1, 2, 3, 4, 5, 6, 7, 8], start=1)]
    res = build_model(_Assay(), _BridgePlatform(), samples, [_QC("ONLY")])
    assert res["status"] == "rejected"
    assert any("bridge_lot_coverage" in r for r in res["diagnostics"]["reasons"])


def test_bridge_route_validates_with_two_lots_and_partner_standardizer():
    samples = []
    levels = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    for i, x in enumerate(levels):
        lot = "L1" if i < 5 else "L2"
        samples.append(_S(f"B{i}", "bridge", x, partner=x - 0.6, lot=lot,
                          role="holdout" if i in (3, 8) else "train"))
    partner = {"enabled": True, "slope": 1.0, "intercept": 0.0,
               "range_low": 0.0, "range_high": 20.0}
    res = build_model(_Assay(), _BridgePlatform(), samples,
                      [_QC("L1"), _QC("L2")],
                      partner_standardizer=partner, partner_code="C1")
    assert res["status"] == "validated"
    assert res["transform"]["intercept"] == pytest.approx(-0.6, abs=0.01)
    assert res["transform"]["partner_center_code"] == "C1"


def test_bridge_insufficient_samples_is_rejected_not_imputed():
    res = build_model(_Assay(), _BridgePlatform(), [], [])
    assert res["status"] == "rejected"
    assert res["diagnostics"]["reasons"]  # explicit, auditable reasons


def test_lot_drift_is_reported():
    samples = _traceable_samples()
    drifted = [_S(f"D{i}", "reference", truth + 0.8 + 1.4, ref=truth, lot="DRIFT")
               for i, truth in enumerate([2.0, 4.0, 6.0, 8.0, 10.0])]
    res = build_model(_Assay(), _Platform(), samples + drifted,
                      [_QC("L1"), _QC("DRIFT")])
    drifted_rows = [r for r in res["diagnostics"]["by_lot"] if r["drifted"]]
    assert any(r["lot"] == "DRIFT" for r in drifted_rows)


# ---------------------------------------------------------------------------
# Executor classification
# ---------------------------------------------------------------------------

def test_executor_classifies_out_of_range_and_censored(tmp_path):
    (tmp_path / "registry.csv").write_text(
        "lab,fu_date\n0.1,2026-03-01\n5.0,2026-03-01\n30.0,2026-03-01\n"
        "5.0,2026-09-01\n")
    transform = {
        "enabled": True, "slope": 1.0, "intercept": -0.8,
        "range_low": 1.0, "range_high": 12.0,
        "detection_limit": 0.2, "quantitation_limit": 0.2,
        "qc_failed_windows": [{"lot": "x", "start": "2026-08-01", "end": None}],
        "valid_from": "2026-01-01", "valid_to": None,
    }
    result = executor.execute(str(tmp_path), {
        "conditions": [],
        "request_spec": {"group_by": [], "metrics": [
            {"type": "count"},
            {"type": "standardized_mean", "variable": "followup.lab_value"}]},
        "mapping": {"followup.lab_value": {"field": "lab"},
                    "followup.date": {"field": "fu_date"}},
        "calibrations": {"followup.lab_value": transform},
    })
    group = result["groups"][0]
    counts = result["harmonization"]["followup.lab_value"]["counts"]
    assert counts["censored_low"] == 1
    assert counts["comparable"] == 1
    assert counts["above_range"] == 1
    assert counts["qc_failed"] == 1
    # standardized mean uses ONLY the single comparable value (5.0 -> 4.2)
    assert group["metrics"]["followup.lab_value_std_mean"] == pytest.approx(4.2)
    assert group["metrics"]["followup.lab_value_std_n"] == 1


def test_executor_marks_all_values_uncalibrated_without_model(tmp_path):
    (tmp_path / "registry.csv").write_text("lab,fu_date\n5.0,2026-03-01\n")
    result = executor.execute(str(tmp_path), {
        "conditions": [],
        "request_spec": {"group_by": [], "metrics": [
            {"type": "standardized_mean", "variable": "followup.lab_value"}]},
        "mapping": {"followup.lab_value": {"field": "lab"},
                    "followup.date": {"field": "fu_date"}},
        "calibrations": {},
    })
    counts = result["harmonization"]["followup.lab_value"]["counts"]
    assert counts["uncalibrated"] == 1
    assert result["groups"][0]["metrics"]["followup.lab_value_std_mean"] is None


# ---------------------------------------------------------------------------
# Integration through the API
# ---------------------------------------------------------------------------

def test_coverage_lists_comparable_and_non_comparable_centers(client, researcher1):
    r = client.get("/api/harmonization/coverage", headers=researcher1)
    assert r.status_code == 200
    body = r.json()
    by = {c["center_code"]: c for c in body["centers"]}
    # traceable / two-lot bridged centers are comparable
    for code in ("C1", "C2", "C3", "C4"):
        assert by[code]["comparable"] is True, (code, by[code])
    # C6 bridged on a single lot, C5 has no bridge samples at all
    assert by["C6"]["comparable"] is False
    assert any("bridge_lot_coverage" in x for x in by["C6"]["reasons"])
    assert by["C5"]["comparable"] is False
    assert set(body["comparable_centers"]) == {"C1", "C2", "C3", "C4"}


def test_standardized_run_reports_non_comparable_share(client, researcher1):
    r = client.post("/api/runs", headers=researcher1,
                    json={"rule_version_id": 1, "request_spec": STD_SPEC})
    assert r.status_code == 201, r.text
    run = r.json()
    harm = run["result"]["harmonization"]["followup.lab_value"]
    assert harm["totals"]["values_total"] > 0
    assert harm["totals"]["comparable"] < harm["totals"]["values_total"]
    assert harm["totals"]["counts"]["censored_low"] >= 1
    assert harm["totals"]["counts"]["qc_failed"] >= 1
    assert harm["totals"]["counts"]["above_range"] >= 1
    assert harm["totals"]["counts"]["outside_validity"] >= 1
    assert "C6" in harm["non_comparable_centers"]
    # every calibration row is versioned for this run
    assert {c["center_code"] for c in run["calibrations"]} == {
        "C1", "C2", "C3", "C4", "C5", "C6"}
    c6 = next(c for c in run["calibrations"] if c["center_code"] == "C6")
    assert c6["status"] == "rejected" and c6["enabled"] is False


def test_new_calibration_version_only_affects_new_runs(client, researcher1, admin):
    first = client.post("/api/runs", headers=researcher1,
                        json={"rule_version_id": 1, "request_spec": STD_SPEC}).json()
    c2_before = next(c for c in first["calibrations"] if c["center_code"] == "C2")
    before_hash = c2_before["transform_hash"]

    # data admin re-syncs C2 (now carrying its drifted lot) and refits version 2,
    # excluding the confirmed drifted lot from the usable transform.
    r = client.post("/api/harmonization/centers/C2/sync", headers=admin)
    assert r.status_code == 200, r.text
    r = client.post("/api/harmonization/assays/C2/fit", headers=admin,
                    json={"exclude_lots": ["B-2502"]})
    assert r.status_code == 200, r.text
    model = r.json()
    assert model["version_no"] == 2
    assert model["status"] == "validated"
    assert model["notes"] and "B-2502" in model["notes"]

    # the old run keeps its frozen transform; a new run picks up v2
    old = client.get(f"/api/runs/{first['id']}", headers=researcher1).json()
    old_c2 = next(c for c in old["calibrations"] if c["center_code"] == "C2")
    assert old_c2["model_version_no"] == 1
    assert old_c2["transform_hash"] == before_hash

    second = client.post("/api/runs", headers=researcher1,
                         json={"rule_version_id": 1, "request_spec": STD_SPEC}).json()
    new_c2 = next(c for c in second["calibrations"] if c["center_code"] == "C2")
    assert new_c2["model_version_no"] == 2
    assert new_c2["transform_hash"] != before_hash

    # v2 run excludes the drifted lot's period from comparability
    harm = second["result"]["harmonization"]["followup.lab_value"]["centers"]["C2"]
    assert harm["counts"]["qc_failed"] >= 1


def test_drift_diagnostic_exposes_lot_bias(client, admin):
    # sync C2 with both lots and refit including them -> drift is surfaced
    client.post("/api/harmonization/centers/C2/sync", headers=admin)
    r = client.post("/api/harmonization/assays/C2/fit", headers=admin, json={})
    diag = r.json()["diagnostics"]
    drift_lots = [d["lot"] for d in diag.get("lot_drift", [])]
    assert "B-2502" in drift_lots
    assert r.json()["status"] == "rejected"  # unstable lots -> not comparable


def test_rejected_calibration_cannot_be_forced_into_a_run(client, researcher1):
    run = client.post("/api/runs", headers=researcher1,
                      json={"rule_version_id": 1, "request_spec": STD_SPEC}).json()
    harm = run["result"]["harmonization"]["followup.lab_value"]
    c6 = harm["centers"]["C6"]
    assert c6["enabled"] is False
    assert c6["counts"]["uncalibrated"] > 0
    assert c6["counts"]["comparable"] == 0


def test_output_table_and_report_carry_calibration_provenance(
        client, researcher1, reviewer):
    run = client.post("/api/runs", headers=researcher1,
                      json={"rule_version_id": 1, "request_spec": STD_SPEC}).json()
    r = client.post(f"/api/runs/{run['id']}/outputs", headers=researcher1,
                    json={"title": "标准化指标输出"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["table_data"]["harmonization"]
    # resolve review by suppressing any flagged cells, then approve
    cells = [{"group": f["group"], "metric": f["metric"]} for f in out["flags"]]
    if out["flags"]:
        for group in {c["group"] for c in cells if c["group"] != "__total__"}:
            pass
        r = client.post(f"/api/outputs/{out['id']}/decision", headers=reviewer,
                        json={"action": "suppress",
                              "detail": {"cells": cells}, "comment": "抑制小样本"})
        assert r.status_code == 200, r.text
        out = r.json()
    while out["status"] == "pending_review":
        cells = [{"group": f["group"], "metric": f["metric"]} for f in out["flags"]]
        if not cells:
            r = client.post(f"/api/outputs/{out['id']}/decision", headers=reviewer,
                            json={"action": "approve", "comment": "通过"})
        else:
            r = client.post(f"/api/outputs/{out['id']}/decision", headers=reviewer,
                            json={"action": "suppress", "detail": {"cells": cells},
                                  "comment": "抑制小样本"})
        assert r.status_code == 200, r.text
        out = r.json()
    assert out["status"] == "approved"
    rep = client.get(f"/api/reports/{out['published_report_id']}",
                     headers=reviewer).json()
    prov = rep["report"]["provenance"]
    assert prov["calibrations"]
    assert prov["harmonization"]["followup.lab_value"]["totals"]["values_total"] > 0
    c6 = next(c for c in prov["calibrations"] if c["center_code"] == "C6")
    assert c6["status"] == "rejected"


def test_std_mean_with_single_comparable_value_is_flagged():
    from app.privacy import analyze_table

    table = {
        "groups": [
            {"key": {}, "label": "A", "n": 40,
             "metrics": {"followup.lab_value_std_n": 1,
                         "followup.lab_value_std_mean": 5.1},
             "suppressed": []},
            {"key": {}, "label": "B", "n": 40,
             "metrics": {"followup.lab_value_std_n": 30,
                         "followup.lab_value_std_mean": 5.0},
             "suppressed": []},
        ],
        "totals": {"n": 80, "metrics": {"followup.lab_value_std_n": 31,
                                        "followup.lab_value_std_mean": 5.0}},
        "count_metrics": ["n", "followup.lab_value_std_n"],
    }
    flags = analyze_table(table, 5)
    assert any(f["group"] == "A" and f["metric"] == "followup.lab_value_std_n"
               for f in flags)
    # the mean that rests on one patient's measurement is flagged too
    assert any(f["group"] == "A"
               and f["metric"] == "followup.lab_value_std_mean"
               and f["reason"] == "small_comparable_n" for f in flags)
