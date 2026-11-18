"""Cross-center measurement-scale calibration.

Two admissible routes, both of which must yield a *validated* comparable value
or an explicit non-comparable range — matching units are never sufficient
evidence of a common measurement scale.

Route A — traceable
    The platform has an unbroken chain to a common reference material. The
    transfer is fitted on assigned reference-material values and only applies
    inside the span covered by those calibrators.

Route B — bridged
    No common reference material, but paired reference/bridge samples were
    measured on this platform and on a partner platform over a period. The
    mapping is estimated from the pairs and validated on held-out pairs; it is
    only usable while every lot it relies on passes QC and while the pairs
    cover more than one lot.

Nothing here imputes a value. Values outside the calibrated range, below the
detection/quantitation limit, in a failed QC lot, or belonging to a center
with no validated model are returned with their interval/censoring state and
excluded from the comparable metric.
"""
from __future__ import annotations

import math

# Acceptance thresholds for a fitted transfer (relative to the truth values).
MIN_TRAIN_TRACEABLE = 5
MIN_HOLDOUT_TRACEABLE = 2
MIN_TRAIN_BRIDGE = 5
MIN_HOLDOUT_BRIDGE = 2
MIN_BRIDGE_LOTS = 2
MAX_REL_ERROR = 0.15        # worst held-out relative error
MAX_ABS_BIAS_REL = 0.10     # |mean residual| / mean truth
MIN_R2 = 0.95
DRIFT_REL = 0.06            # lot bias shift (÷ mean truth) considered drift


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fit_line(xs: list[float], ys: list[float]) -> dict:
    """Ordinary least squares y = intercept + slope * x."""
    n = len(xs)
    if n < 2:
        raise ValueError("at least two points are required to fit a line")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        raise ValueError("all x values are identical; slope is not identified")
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r2": r2, "n": n}


def predict(transform: dict, x: float) -> float:
    return transform["intercept"] + transform["slope"] * x


def evaluate(pairs: list[tuple[float, float]]) -> dict:
    """Error metrics for (predicted, truth) pairs."""
    if not pairs:
        return {"n": 0, "rmse": None, "mae": None, "bias": None,
                "max_rel_error": None, "r2": None, "mean_truth": None}
    errs = [p - t for p, t in pairs]
    truths = [t for _, t in pairs]
    mean_truth = sum(truths) / len(truths)
    ss_tot = sum((t - mean_truth) ** 2 for t in truths)
    ss_res = sum(e ** 2 for e in errs)
    rel = [abs(e) / abs(t) for e, t in zip(errs, truths) if t]
    return {
        "n": len(pairs),
        "rmse": math.sqrt(sum(e ** 2 for e in errs) / len(errs)),
        "mae": sum(abs(e) for e in errs) / len(errs),
        "bias": sum(errs) / len(errs),
        "max_rel_error": max(rel) if rel else None,
        "r2": (1.0 - ss_res / ss_tot) if ss_tot > 0 else 1.0,
        "mean_truth": mean_truth,
    }


def _r(v, nd=4):
    return round(v, nd) if _finite(v) else v


def _sample_lot(s) -> str | None:
    return getattr(s, "lot", None) or None


def _qc_by_lot(qc_batches: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for q in qc_batches:
        cur = out.get(q.lot)
        # a lot registered in several QC windows is only usable if all pass
        status = "failed" if (cur and cur["status"] == "failed") or q.status == "failed" \
            else "passed"
        out[q.lot] = {
            "lot": q.lot, "status": status,
            "observed_cv_pct": q.observed_cv_pct,
            "allowed_cv_pct": q.allowed_cv_pct,
            "period_start": q.period_start, "period_end": q.period_end,
            "note": q.note,
        }
    return out


def _split(samples: list) -> tuple[list, list]:
    train = [s for s in samples if s.role != "holdout"]
    holdout = [s for s in samples if s.role == "holdout"]
    return train, holdout


def _truth_for_route_b(sample, partner_standardizer):
    """Common-scale truth of a bridge pair.

    If the partner platform has a validated transfer, its paired value is first
    standardized; otherwise the raw partner value is used and the model is
    flagged as resting on an unstandardized partner.
    """
    if sample.partner_value is None:
        return None, False
    if partner_standardizer is None:
        return sample.partner_value, False
    t = partner_standardizer
    if not t.get("enabled"):
        return sample.partner_value, False
    if not (t.get("range_low") is not None and t.get("range_high") is not None):
        return sample.partner_value, False
    if not (t["range_low"] <= sample.partner_value <= t["range_high"]):
        # partner value outside its own calibrated range -> not comparable truth
        return None, False
    return predict(t, sample.partner_value), True


def build_model(assay, platform, samples: list, qc_batches: list,
                partner_standardizer: dict | None = None,
                forced_route: str | None = None,
                partner_code: str | None = None,
                exclude_lots: list[str] | None = None) -> dict:
    """Fit + hold-out validate one center assay.

    Returns a dict with status/route/transform/validation/diagnostics/fit_params
    ready to persist as a CalibrationModel. Never raises for insufficient data:
    an insufficient fit produces status="rejected" with reasons, so the center
    is explicitly marked non-comparable rather than silently pooled.
    """
    qc = _qc_by_lot(qc_batches)
    failed_lots = sorted(lot for lot, q in qc.items() if q["status"] == "failed")
    # Lots the analyst explicitly excludes (e.g. confirmed drift) are removed
    # from the fit and marked non-comparable at run time, never imputed.
    excluded_lots = sorted(set(failed_lots) | set(exclude_lots or []))
    reasons: list[str] = []
    flags: list[str] = []

    traceable = platform.traceability == "traceable"
    route = forced_route or ("traceable" if traceable else "bridge")
    if route == "traceable" and not traceable:
        reasons.append("platform_not_traceable_to_reference_material")

    # ---- select usable samples -------------------------------------------------
    def usable(s, truth):
        if not _finite(s.measured_value) or not _finite(truth):
            return False
        lot = _sample_lot(s)
        if lot and lot in excluded_lots:
            return False  # QC-failed/excluded lot values never used to fit
        mr = assay.measuring_range or {}
        if mr.get("low") is not None and s.measured_value < mr["low"]:
            return False
        if mr.get("high") is not None and s.measured_value > mr["high"]:
            return False
        return True

    excluded_lot_samples = 0
    prepared = []  # (sample, truth, partner_standardized)
    if route == "traceable":
        for s in samples:
            if s.kind != "reference":
                continue
            if s.reference_value is None:
                continue
            if not usable(s, s.reference_value):
                if _sample_lot(s) in excluded_lots:
                    excluded_lot_samples += 1
                continue
            prepared.append((s, s.reference_value, True))
    else:
        for s in samples:
            if s.kind != "bridge":
                continue
            truth, partner_std = _truth_for_route_b(s, partner_standardizer)
            if truth is None:
                if s.partner_value is not None:
                    flags.append("bridge_truth_outside_partner_range")
                continue
            if not partner_std:
                flags.append("partner_not_standardized")
            if not usable(s, truth):
                if _sample_lot(s) in excluded_lots:
                    excluded_lot_samples += 1
                continue
            prepared.append((s, truth, partner_std))

    train = [p for p in prepared if p[0].role != "holdout"]
    holdout = [p for p in prepared if p[0].role == "holdout"]

    diagnostic_base = {
        "n_train": len(train), "n_holdout": len(holdout),
        "n_samples": len(prepared), "n_excluded_lot_samples": excluded_lot_samples,
        "failed_lots": failed_lots,
        "excluded_lots": excluded_lots,
        "qc_lots": list(qc.values()),
    }

    # ---- minimum evidence -------------------------------------------------------
    min_train = MIN_TRAIN_TRACEABLE if route == "traceable" else MIN_TRAIN_BRIDGE
    min_hold = MIN_HOLDOUT_TRACEABLE if route == "traceable" else MIN_HOLDOUT_BRIDGE
    if len(train) < min_train:
        reasons.append(f"insufficient_calibration_samples({len(train)}<{min_train})")
    if len(holdout) < min_hold:
        reasons.append(f"insufficient_holdout_samples({len(holdout)}<{min_hold})")

    if route == "bridge":
        pair_lots = {(_sample_lot(s) or "unknown") for s, _, _ in prepared}
        if len(pair_lots) < MIN_BRIDGE_LOTS:
            reasons.append(f"bridge_lot_coverage({len(pair_lots)}<{MIN_BRIDGE_LOTS})")
        if exclude_lots:
            flags.append("manual_lot_exclusion")

    # ---- fit --------------------------------------------------------------------
    transform: dict = {"enabled": False, "route": route, "unit": assay.unit}
    diagnostics: dict = dict(diagnostic_base)
    fit_params = {
        "route": route, "min_train": min_train, "min_holdout": min_hold,
        "max_rel_error": MAX_REL_ERROR, "max_abs_bias_rel": MAX_ABS_BIAS_REL,
        "min_r2": MIN_R2, "excluded_lots": excluded_lots,
    }

    if not reasons:
        xs = [s.measured_value for s, _, _ in train]
        ys = [truth for _, truth, _ in train]
        line = fit_line(xs, ys)
        hold_pairs = [(line["intercept"] + line["slope"] * s.measured_value, truth)
                      for s, truth, _ in holdout]
        hold_eval = evaluate(hold_pairs)

        if line["slope"] <= 0:
            reasons.append("non_positive_slope")
        if hold_eval["max_rel_error"] is not None \
                and hold_eval["max_rel_error"] > MAX_REL_ERROR:
            reasons.append(
                f"holdout_rel_error({hold_eval['max_rel_error']:.3f}>{MAX_REL_ERROR})")
        if hold_eval["mean_truth"] and hold_eval["bias"] is not None \
                and abs(hold_eval["bias"]) / abs(hold_eval["mean_truth"]) > MAX_ABS_BIAS_REL:
            reasons.append("holdout_bias_exceeds_tolerance")
        if hold_eval["r2"] is not None and hold_eval["r2"] < MIN_R2:
            reasons.append(f"holdout_r2({hold_eval['r2']:.3f}<{MIN_R2})")

        # Applicable range: calibrator span ∩ platform measuring range.
        mr = assay.measuring_range or {}
        low = max(min(xs), mr.get("low") if mr.get("low") is not None else min(xs))
        high = min(max(xs), mr.get("high") if mr.get("high") is not None else max(xs))
        if assay.quantitation_limit is not None:
            low = max(low, assay.quantitation_limit)
        if assay.upper_reportable is not None:
            high = min(high, assay.upper_reportable)
        if low >= high:
            reasons.append("empty_calibrated_range")

        # Bias diagnostics on all usable samples: raw (before) vs standardized.
        raw_pairs = [(s.measured_value, truth) for s, truth, _ in prepared]
        std_pairs = [(line["intercept"] + line["slope"] * s.measured_value, truth)
                     for s, truth, _ in prepared]
        raw_eval = evaluate(raw_pairs)
        std_eval = evaluate(std_pairs)

        by_lot: dict[str, dict] = {}
        for s, truth, _ in prepared:
            lot = _sample_lot(s) or "unknown"
            b = by_lot.setdefault(lot, {"lot": lot, "n": 0, "raw": [], "std": [],
                                        "truth": [],
                                        "qc_status": (qc.get(lot) or {}).get("status", "unregistered")})
            b["n"] += 1
            b["raw"].append(s.measured_value)
            b["std"].append(line["intercept"] + line["slope"] * s.measured_value)
            b["truth"].append(truth)
        lot_rows = []
        overall_truth = raw_eval["mean_truth"] or 0
        for lot, b in sorted(by_lot.items()):
            raw_bias = sum(x - t for x, t in zip(b["raw"], b["truth"])) / b["n"]
            std_bias = sum(x - t for x, t in zip(b["std"], b["truth"])) / b["n"]
            drift = bool(overall_truth) and abs(raw_bias - (raw_eval["bias"] or 0)) \
                > DRIFT_REL * abs(overall_truth)
            lot_rows.append({"lot": lot, "n": b["n"], "qc_status": b["qc_status"],
                             "raw_mean": _r(sum(b["raw"]) / b["n"]),
                             "truth_mean": _r(sum(b["truth"]) / b["n"]),
                             "raw_bias": _r(raw_bias), "standardized_bias": _r(std_bias),
                             "drifted": drift})

        diagnostics.update({
            "rmse": _r(hold_eval["rmse"]), "mae": _r(hold_eval["mae"]),
            "bias": _r(hold_eval["bias"]),
            "max_rel_error": _r(hold_eval["max_rel_error"]),
            "r2": _r(hold_eval["r2"]),
            "train_r2": _r(line["r2"]),
            "raw_bias": _r(raw_eval["bias"]), "raw_rmse": _r(raw_eval["rmse"]),
            "standardized_bias": _r(std_eval["bias"]),
            "standardized_rmse": _r(std_eval["rmse"]),
            "raw_mean": _r(raw_eval["mean_truth"] and
                           sum(s.measured_value for s, _, _ in prepared) / len(prepared)),
            "truth_mean": _r(raw_eval["mean_truth"]),
            "by_lot": lot_rows,
            "lot_drift": [{"lot": r["lot"], "drifted": r["drifted"],
                           "raw_bias": r["raw_bias"]} for r in lot_rows if r["drifted"]],
        })
        transform = {
            "enabled": False,
            "route": route,
            "slope": _r(line["slope"], 6),
            "intercept": _r(line["intercept"], 6),
            "unit": assay.unit,
            "range_low": _r(low), "range_high": _r(high),
            "detection_limit": assay.detection_limit,
            "quantitation_limit": assay.quantitation_limit,
            "upper_reportable": assay.upper_reportable,
            "failed_lots": failed_lots,
            "excluded_lots": excluded_lots,
            "flags": flags,
            "same_unit_only": True,
            "partner_center_code": partner_code,
        }

    passed = not reasons and transform.get("slope") is not None
    if passed:
        transform["enabled"] = True
    else:
        # keep the fitted transform for diagnostics, but mark it non-usable
        transform["enabled"] = False
        flags.extend(reasons)
    transform["flags"] = flags
    diagnostics["passed"] = passed
    diagnostics["reasons"] = reasons
    diagnostics["flags"] = flags

    status = "validated" if passed else "rejected"
    notes = None
    if not passed:
        notes = "校准不可用，指标不纳入跨中心比较：" + "；".join(reasons or flags)
    elif excluded_lots:
        notes = f"已排除批次（质控失败或漂移）：{', '.join(excluded_lots)}；" \
                "该批次期间的检测值不纳入跨中心比较"

    return {
        "status": status,
        "route": route,
        "transform": transform,
        "validation": {
            "route": route,
            "unit": assay.unit,
            "range_low": transform.get("range_low"),
            "range_high": transform.get("range_high"),
            "detection_limit": assay.detection_limit,
            "quantitation_limit": assay.quantitation_limit,
            "upper_reportable": assay.upper_reportable,
            "same_unit_only": True,
            "flags": flags,
            "failed_lots": failed_lots,
            "excluded_lots": excluded_lots,
            "passed": passed,
        },
        "diagnostics": diagnostics,
        "fit_params": fit_params,
        "notes": notes,
    }
