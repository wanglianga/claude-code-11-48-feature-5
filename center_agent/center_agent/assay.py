"""Deterministic fictional assay metadata for the six demo centers.

Mirrors datagen.py's philosophy: every center measures the SAME biomarker but
on its own platform, with its own reagent lot, calibration curve, reference
interval, detection limit and validity period. Raw values therefore sit on
different measurement scales even though every center reports "mg/L".

Only aggregate/versioned metadata and de-identified reference samples leave the
center; patient-level records never do.

Usage:
    python -m center_agent.assay --root data/centers            # all centers
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# center -> platform assignment and measurement characteristics.
# `true_bias`: additive offset of the raw value from the common scale.
# `true_scale`: multiplicative factor of the raw value.
# `lod`: detection limit (mg/L); `uloq`: upper reportable limit.
CENTER_ASSAYS = {
    "C1": {
        "platform": "P-CLIA-A", "unit": "mg/L", "lot": "A-2401",
        "true_bias": 0.0, "true_scale": 1.00, "lod": 0.20, "uloq": 14.0,
        "measuring_range": {"low": 0.05, "high": 14.0},
        "reference_interval": {"low": 1.8, "high": 6.5},
        "calibration_curve": {"a": 0.02, "b": 1.003, "r2": 0.9994},
        "valid_from": "2026-01-01", "valid_to": None,
        "qc": [
            {"lot": "A-2401", "period_start": "2026-01-01", "period_end": "2026-06-30",
             "observed_cv_pct": 3.8, "allowed_cv_pct": 10.0, "status": "passed"},
        ],
    },
    "C2": {
        "platform": "P-CLIA-B", "unit": "mg/L", "lot": "B-2401",
        "true_bias": 0.85, "true_scale": 1.00, "lod": 0.25, "uloq": 12.0,
        "measuring_range": {"low": 0.08, "high": 12.0},
        "reference_interval": {"low": 2.4, "high": 7.4},
        "calibration_curve": {"a": 0.88, "b": 0.996, "r2": 0.9988},
        "valid_from": "2026-01-01", "valid_to": None,
        "qc": [
            {"lot": "B-2401", "period_start": "2026-01-01", "period_end": "2026-05-31",
             "observed_cv_pct": 5.4, "allowed_cv_pct": 10.0, "status": "passed"},
            # second lot shows a systematic shift; QC initially passes
            {"lot": "B-2502", "period_start": "2026-06-01", "period_end": None,
             "observed_cv_pct": 8.9, "allowed_cv_pct": 10.0, "status": "passed",
             "note": "新批次，偏差待观察"},
        ],
        "drift_lot": "B-2502",
    },
    "C3": {
        "platform": "P-ELISA-C", "unit": "mg/L", "lot": "C-2401",
        "true_bias": -0.55, "true_scale": 1.00, "lod": 0.50, "uloq": 11.0,
        "measuring_range": {"low": 0.10, "high": 11.0},
        "reference_interval": {"low": 1.2, "high": 5.6},
        "calibration_curve": {"a": -0.58, "b": 1.012, "r2": 0.9979},
        "valid_from": "2026-01-01", "valid_to": None,
        "qc": [
            {"lot": "C-2401", "period_start": "2026-01-01", "period_end": "2026-02-28",
             "observed_cv_pct": 6.9, "allowed_cv_pct": 10.0, "status": "passed"},
            {"lot": "C-2601", "period_start": "2026-05-31", "period_end": None,
             "observed_cv_pct": 7.4, "allowed_cv_pct": 10.0, "status": "passed"},
            # a mid-year replacement lot drifted and failed QC -> never pooled
            {"lot": "C-2502", "period_start": "2026-03-01", "period_end": "2026-05-30",
             "observed_cv_pct": 18.7, "allowed_cv_pct": 10.0, "status": "failed",
             "note": "质控失控：校准曲线斜率漂移"},
        ],
        "bridge_to": "C1",
    },
    "C4": {
        "platform": "P-LCMS-D", "unit": "mg/L", "lot": "D-2401",
        "true_bias": 1.60, "true_scale": 1.00, "lod": 0.10, "uloq": 16.0,
        "measuring_range": {"low": 0.02, "high": 16.0},
        "reference_interval": {"low": 3.1, "high": 8.2},
        "calibration_curve": {"a": 1.55, "b": 1.008, "r2": 0.9997},
        # registration lapsed mid-year: later follow-ups are outside validity
        "valid_from": "2026-01-01", "valid_to": "2026-05-31",
        "qc": [
            {"lot": "D-2401", "period_start": "2026-01-01", "period_end": "2026-06-30",
             "observed_cv_pct": 2.6, "allowed_cv_pct": 10.0, "status": "passed"},
        ],
    },
    # C5 has NO lab field and, on its bridged platform, has no paired bridge
    # samples at all -> calibration must be rejected as non-comparable.
    "C5": {
        "platform": "P-ELISA-C", "unit": "mg/L", "lot": "E-2401",
        "true_bias": -0.40, "true_scale": 1.00, "lod": 0.50, "uloq": 11.0,
        "measuring_range": {"low": 0.10, "high": 11.0},
        "reference_interval": {"low": 1.2, "high": 5.6},
        "calibration_curve": {"a": -0.45, "b": 1.010, "r2": 0.9971},
        "valid_from": "2026-01-01", "valid_to": None,
        "qc": [],
        "bridge_to": "C1",
        "no_lab_field": True,
        "no_bridge_samples": True,
    },
    # C6 is on a bridged platform and only has paired samples from ONE lot ->
    # bridge coverage is insufficient, so its lab values are NOT comparable.
    "C6": {
        "platform": "P-ELISA-C", "unit": "mg/L", "lot": "F-2401",
        "true_bias": -0.40, "true_scale": 1.00, "lod": 0.50, "uloq": 11.0,
        "measuring_range": {"low": 0.10, "high": 11.0},
        "reference_interval": {"low": 1.2, "high": 5.6},
        "calibration_curve": {"a": -0.45, "b": 1.010, "r2": 0.9971},
        "valid_from": "2026-01-01", "valid_to": None,
        "qc": [
            {"lot": "F-2401", "period_start": "2026-01-01", "period_end": None,
             "observed_cv_pct": 7.1, "allowed_cv_pct": 10.0, "status": "passed"},
        ],
        "bridge_to": "C1",
        "bridge_single_lot": True,
    },
}

# C1 is traced to a certified reference material; C2/C4 are also traceable.
# C3 has NO common reference material -> must use bridge samples, but its
# bridge pairs only cover a single lot -> its calibration is rejected.
PLATFORMS = {
    "P-CLIA-A": {"name": "化学发光免疫平台 A", "manufacturer": "Demo Diagnostics",
                 "method": "CLIA", "traceability": "traceable",
                 "reference_material": "ERM-DA470k/IFCC（赋值 5.00 mg/L）"},
    "P-CLIA-B": {"name": "化学发光免疫平台 B", "manufacturer": "Demo Diagnostics",
                 "method": "CLIA", "traceability": "traceable",
                 "reference_material": "ERM-DA470k/IFCC（赋值 5.00 mg/L）"},
    "P-ELISA-C": {"name": "酶联免疫平台 C", "manufacturer": "LocalBio",
                  "method": "ELISA", "traceability": "bridged",
                  "reference_material": None},
    "P-LCMS-D": {"name": "液相色谱-质谱平台 D", "manufacturer": "Analytik",
                 "method": "LC-MS", "traceability": "traceable",
                 "reference_material": "NIST SRM 1950（赋值 5.00 mg/L）"},
}

# Assigned values of the common standard (mg/L of the common scale).
REFERENCE_TRUE = [0.8, 2.0, 3.5, 4.5, 5.0, 7.5, 10.0, 11.0]
# Bridge material levels (common scale) shared by the bridged platform and its
# traceable partner platform.
BRIDGE_LEVELS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.5, 7.5, 8.5, 9.5, 10.5]

DEFAULT_SEED = 7101


def _raw(center: str, truth: float) -> float:
    spec = CENTER_ASSAYS[center]
    return round(truth * spec["true_scale"] + spec["true_bias"], 3)


def _reference_samples(center: str, rng_like: float = 0.0) -> list[dict]:
    """Traceable platforms: same reference material, assigned value known."""
    spec = CENTER_ASSAYS[center]
    out = []
    for i, truth in enumerate(REFERENCE_TRUE):
        role = "holdout" if i in (1, 4, 7) else "train"
        out.append({
            "sample_code": f"RM-{center}-{i + 1:02d}",
            "kind": "reference",
            "reference_value": truth,
            "measured_value": _raw(center, truth),
            "lot": spec["lot"],
            "role": role,
            "measured_on": "2026-03-15",
        })
    return out


def _bridge_samples(center: str, partner: str, single_lot: bool = False) -> list[dict]:
    """Paired bridge samples measured on this platform and a partner platform.

    De-identified reference/bridge material values only; the partner value is
    the same material measured on the partner platform during the same period.
    One lot here failed QC and must be excluded from any fit.
    """
    if single_lot:
        lots = [CENTER_ASSAYS[center]["lot"]] * len(BRIDGE_LEVELS)
        dates = ["2026-05-10"] * len(BRIDGE_LEVELS)
    else:
        lots = ["C-2401"] * 4 + ["C-2502"] * 2 + ["C-2601"] * 4
        dates = ["2026-02-10"] * 4 + ["2026-04-10"] * 2 + ["2026-07-10"] * 4
    out = []
    for i, truth in enumerate(BRIDGE_LEVELS):
        out.append({
            "sample_code": f"BR-{center}-{i + 1:02d}",
            "kind": "bridge",
            "reference_value": None,
            "measured_value": _raw(center, truth),
            "partner_value": _raw(partner, truth),
            "lot": lots[i],
            "partner_center": partner,
            "role": "holdout" if i in (2, 8) else "train",
            "measured_on": dates[i],
        })
    return out


def _scale_shift_samples(center: str) -> list[dict]:
    """Extra samples demonstrating a drifted lot (C2 seeding scenario)."""
    out = []
    for i, truth in enumerate([1.5, 3.0, 4.5, 6.0, 8.0, 9.5]):
        # drifted lot carries an extra +0.75 offset (~12% of the mean level)
        raw = round(_raw(center, truth) + 0.75, 3)
        out.append({
            "sample_code": f"RM-{center}-L2-{i + 1:02d}",
            "kind": "reference",
            "reference_value": truth,
            "measured_value": raw,
            "lot": "B-2502",
            "role": "holdout" if i == 3 else "train",
            "measured_on": "2026-06-15",
        })
    return out


def build_assay(center: str) -> dict:
    """Return the center's assay.json payload (metadata + de-identified samples)."""
    if center not in CENTER_ASSAYS:
        return {}
    spec = CENTER_ASSAYS[center]
    samples: list[dict] = []
    if _platform(center)["traceability"] == "traceable":
        samples = _reference_samples(center)
        if center == "C2":
            samples += _scale_shift_samples(center)
    elif not spec.get("no_bridge_samples"):
        samples = _bridge_samples(center, spec["bridge_to"],
                                  single_lot=bool(spec.get("bridge_single_lot")))
    return {
        "center": center,
        "variable": "followup.lab_value",
        "has_lab_field": not spec.get("no_lab_field", False),
        "unit": spec["unit"],
        "platform": dict(PLATFORMS[spec["platform"]], code=spec["platform"]),
        "assay": {
            "reagent_lot": spec["lot"],
            "calibration_curve": spec["calibration_curve"],
            "reference_interval": spec["reference_interval"],
            "measuring_range": spec["measuring_range"],
            "detection_limit": spec["lod"],
            "quantitation_limit": spec["lod"],
            "upper_reportable": spec["uloq"],
            "valid_from": spec["valid_from"],
            "valid_to": spec["valid_to"],
        },
        "qc_batches": spec["qc"],
        "samples": samples,
    }


def _platform(center: str) -> dict:
    return PLATFORMS[CENTER_ASSAYS[center]["platform"]]


def raw_value(center: str, truth: float) -> float:
    """Platform-raw value for a given common-scale truth (used by datagen)."""
    return _raw(center, truth)


def detect_limit(center: str) -> float:
    return CENTER_ASSAYS[center]["lod"]


def upper_limit(center: str) -> float:
    return CENTER_ASSAYS[center]["uloq"]


def write_assay(center: str, root: Path) -> Path | None:
    payload = build_assay(center)
    if not payload:
        return None
    cdir = root / center
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "assay.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/centers")
    ap.add_argument("--center", choices=list(CENTER_ASSAYS), default=None)
    args = ap.parse_args()
    root = Path(args.root)
    centers = [args.center] if args.center else list(CENTER_ASSAYS)
    for c in centers:
        path = write_assay(c, root)
        print(f"{c}: assay metadata -> {path}" if path else f"{c}: no assay metadata")


if __name__ == "__main__":
    main()
