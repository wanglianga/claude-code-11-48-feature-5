"""Center-local job executor.

Reads ONLY the center's own registry.csv, applies the cohort rule and the
statistical request, and returns aggregated counts/means. Individual records
never leave this module's caller (the center agent server).

When a statistical request asks for a standardized mean of a lab variable, the
run snapshot's calibration transform is applied LOCALLY: each raw result is
classified as comparable / censored_low / below_range / above_range /
qc_failed / outside_validity / uncalibrated and only comparable values enter the
standardized mean. Counts of every class are returned (aggregate only) so the
group can audit coverage; no individual lab record is transmitted.
"""
from __future__ import annotations

import csv
from pathlib import Path

OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
}


class MissingMapping(Exception):
    def __init__(self, variable: str):
        self.variable = variable
        super().__init__(f"variable '{variable}' has no local field mapping")


def _coerce(v: str):
    if v == "":
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except (ValueError, TypeError):
            continue
    return v


def load_records(data_dir: str | Path, mapping: dict[str, dict]) -> list[dict]:
    """Load registry.csv and rename local fields to canonical variables.

    mapping: {"patient.sex": {"field": "pat_sex", "value_map": {...}|None}, ...}
    """
    csv_path = Path(data_dir) / "registry.csv"
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    header = set(rows[0].keys()) if rows else set()
    records = []
    for row in rows:
        rec = {}
        for var, spec in mapping.items():
            field = spec.get("field")
            if not field or field not in header:
                raise MissingMapping(var)
            val = row.get(field, "")
            vmap = spec.get("value_map")
            if vmap and val in vmap:
                val = vmap[val]
            rec[var] = _coerce(val)
        records.append(rec)
    return records


def _match(rec: dict, cond: dict) -> bool:
    val = rec.get(cond["variable"])
    op = OPS[cond["op"]]
    target = cond["value"]
    if val is None:
        return False
    try:
        return op(val, target)
    except TypeError:
        # e.g. comparing str with number — treat as non-match
        return False


def apply_rule(records: list[dict], conditions: list[dict]) -> list[dict]:
    includes = [c for c in conditions if c.get("kind") == "include"]
    excludes = [c for c in conditions if c.get("kind") == "exclude"]
    out = []
    for rec in records:
        if includes and not all(_match(rec, c) for c in includes):
            continue
        if any(_match(rec, c) for c in excludes):
            continue
        out.append(rec)
    return out


def _metric_name(spec: dict) -> str:
    if spec["type"] == "outcome_count":
        return f"outcome_{spec['value']}_n"
    if spec["type"] == "mean":
        return f"{spec['variable']}_mean"
    return spec["type"]


def required_variables(conditions: list[dict], request_spec: dict) -> set[str]:
    vs = {c["variable"] for c in conditions}
    vs.update(request_spec.get("group_by", []))
    for m in request_spec.get("metrics", []):
        if m["type"] in ("outcome_count", "mean", "standardized_mean"):
            vs.add(m["variable"])
    return vs


CLASSES = ("comparable", "censored_low", "below_range", "above_range",
           "qc_failed", "outside_validity", "uncalibrated")


def _classify(value: float, when: str | None, transform: dict) -> str:
    """Center-local measurement-scale classification.

    A value is only comparable when a validated transform is snapshotted for
    this run, the value lies inside the calibrated range, its reagent lot did
    not fail QC in the sample's period, and the assay was valid on that date.
    Nothing is imputed: every non-comparable value keeps its class.
    """
    if not transform or not transform.get("enabled"):
        return "uncalibrated"
    vf, vt = transform.get("valid_from"), transform.get("valid_to")
    if when:
        if vf and when < vf:
            return "outside_validity"
        if vt and when > vt:
            return "outside_validity"
    for w in transform.get("qc_failed_windows", []) or []:
        start, end = w.get("start"), w.get("end")
        if start and when and when >= start and (not end or when <= end):
            return "qc_failed"
    dl = transform.get("detection_limit")
    loq = transform.get("quantitation_limit")
    if dl is not None and value <= dl:
        return "censored_low"
    if loq is not None and value < loq:
        return "censored_low"
    lo, hi = transform.get("range_low"), transform.get("range_high")
    if lo is not None and value < lo:
        return "below_range"
    if hi is not None and value > hi:
        return "above_range"
    return "comparable"


def _standardize(transform: dict, value: float) -> float:
    return transform["intercept"] + transform["slope"] * value


def execute(data_dir: str | Path, job: dict) -> dict:
    """Run one job. Returns {"status": "success", ...} or an error payload."""
    conditions = job.get("conditions", [])
    spec = job["request_spec"]
    mapping = job.get("mapping", {})
    calibrations = job.get("calibrations", {}) or {}  # variable -> transform

    missing = [v for v in sorted(required_variables(conditions, spec))
               if v not in mapping or not mapping[v].get("field")]
    if missing:
        return {"status": "missing_mapping",
                "error": f"no mapping for variable(s): {', '.join(missing)}"}
    try:
        records = load_records(data_dir, mapping)
    except MissingMapping as e:
        return {"status": "missing_mapping", "error": str(e)}

    cohort = apply_rule(records, conditions)
    group_vars = spec.get("group_by", [])
    metric_specs = [m for m in spec.get("metrics", []) if m["type"] != "count"]

    buckets: dict[tuple, list[dict]] = {}
    for rec in cohort:
        key = tuple(str(rec.get(v)) for v in group_vars)
        buckets.setdefault(key, []).append(rec)

    # Local standardization bookkeeping per lab variable (aggregate only).
    std_summary: dict[str, dict] = {}
    date_var = "followup.date"

    groups = []
    for key, members in sorted(buckets.items()):
        g = {
            "key": dict(zip(group_vars, key)),
            "label": " / ".join(key) if key else "ALL",
            "n": len(members),
            "metrics": {},
        }
        for ms in metric_specs:
            name = _metric_name(ms)
            if ms["type"] == "outcome_count":
                cnt = sum(1 for r in members if r.get(ms["variable"]) == ms["value"])
                g["metrics"][f"{name}"] = cnt
                g["metrics"][f"outcome_{ms['value']}_rate"] = (
                    round(cnt / len(members), 4) if members else None
                )
            elif ms["type"] == "mean":
                vals = [r.get(ms["variable"]) for r in members
                        if isinstance(r.get(ms["variable"]), (int, float))]
                s = round(sum(vals), 4)
                g["metrics"][name] = round(s / len(vals), 4) if vals else None
                g["metrics"][f"_{name}_sum"] = s
                g["metrics"][f"_{name}_n"] = len(vals)
            elif ms["type"] == "standardized_mean":
                var = ms["variable"]
                transform = calibrations.get(var) or {}
                summ = std_summary.setdefault(var, {
                    "variable": var,
                    "transform_version": transform.get("model_version_no"),
                    "route": transform.get("route"),
                    "enabled": bool(transform.get("enabled")),
                    "range_low": transform.get("range_low"),
                    "range_high": transform.get("range_high"),
                    "detection_limit": transform.get("detection_limit"),
                    "counts": {c: 0 for c in CLASSES},
                    "values_total": 0,
                })
                comparable = []
                for r in members:
                    raw = r.get(var)
                    if not isinstance(raw, (int, float)):
                        continue
                    summ["values_total"] += 1
                    cls = _classify(raw, r.get(date_var), transform)
                    summ["counts"][cls] += 1
                    if cls == "comparable":
                        comparable.append(_standardize(transform, raw))
                sname = f"{var}_std_mean"
                s = round(sum(comparable), 6)
                g["metrics"][sname] = (
                    round(s / len(comparable), 4) if comparable else None
                )
                g["metrics"][f"_{sname}_sum"] = s
                g["metrics"][f"_{sname}_n"] = len(comparable)
                g["metrics"][f"{var}_std_n"] = len(comparable)
        groups.append(g)

    # Per-center harmonization view (no patient-level data).
    harmonization = {}
    for var, summ in std_summary.items():
        total = summ["values_total"]
        comp = summ["counts"]["comparable"]
        non_comp = total - comp
        harmonization[var] = {
            "transform_version": summ["transform_version"],
            "route": summ["route"],
            "enabled": summ["enabled"],
            "range_low": summ["range_low"],
            "range_high": summ["range_high"],
            "detection_limit": summ["detection_limit"],
            "counts": summ["counts"],
            "values_total": total,
            "comparable": comp,
            "non_comparable_ratio": round(non_comp / total, 4) if total else None,
        }

    return {
        "status": "success",
        "total_n": len(cohort),
        "groups": groups,
        "records_scanned": len(records),
        "harmonization": harmonization,
    }
