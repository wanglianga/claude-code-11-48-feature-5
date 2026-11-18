"""Merge per-center aggregate results into one table with explicit coverage.

Centers that failed or lacked mappings are reported as coverage gaps — their
patients are NEVER counted as zero in the totals.
"""
from __future__ import annotations


def merge_group_metrics(groups: list[dict]) -> dict:
    """Sum count metrics and recompute means/rates across same-label groups."""
    out: dict = {}
    for g in groups:
        for m, v in g["metrics"].items():
            if v is None:
                continue
            if m.startswith("_") and m.endswith("_sum"):
                out[m] = round(out.get(m, 0) + v, 4)
            elif m.startswith("_") and m.endswith("_n"):
                out[m] = out.get(m, 0) + v
            elif m.endswith("_rate"):
                continue  # recomputed below
            elif isinstance(v, (int, float)):
                out[m] = out.get(m, 0) + v
    for m in list(out):
        sum_key, n_key = f"_{m}_sum", f"_{m}_n"
        if m.endswith("_mean") and sum_key in out:
            out[m] = round(out[sum_key] / out[n_key], 4) if out[n_key] else None
    return out


def aggregate(center_results: list[dict], count_metrics: list[str]) -> dict:
    """center_results: [{center, status, aggregates}]. Only success rows count."""
    included, excluded = [], []
    buckets: dict[str, dict] = {}
    totals_from: list[dict] = []

    for cr in center_results:
        if cr["status"] == "success" and cr.get("aggregates"):
            included.append(cr["center"])
            totals_from.append(cr)
            for g in cr["aggregates"]["groups"]:
                lbl = g["label"]
                if lbl not in buckets:
                    buckets[lbl] = {"key": g["key"], "label": lbl, "n": 0,
                                    "metrics": {}, "_parts": []}
                buckets[lbl]["_parts"].append(g)
        else:
            excluded.append({"center": cr["center"], "status": cr["status"],
                             "reason": cr.get("error") or "center did not return results"})

    groups = []
    for lbl in sorted(buckets):
        b = buckets[lbl]
        parts = b.pop("_parts")
        b["n"] = sum(p["n"] for p in parts)
        b["metrics"] = merge_group_metrics(parts)
        # recompute rates from merged counts
        for m in list(b["metrics"]):
            if m.endswith("_n") and not m.startswith("_") and not m.endswith("_std_n"):
                base = m[:-2]
                rate_key = f"{base}_rate"
                if any(rate_key in p["metrics"] for p in parts):
                    b["metrics"][rate_key] = (
                        round(b["metrics"][m] / b["n"], 4) if b["n"] else None
                    )
        b["suppressed"] = []
        groups.append(b)

    total_n = sum(g["n"] for g in groups)
    total_metrics = merge_group_metrics(
        [{"metrics": g["metrics"]} for g in groups]
    )
    for m in list(total_metrics):
        if m.endswith("_n") and not m.startswith("_") and not m.endswith("_std_n"):
            base = m[:-2]
            total_metrics[f"{base}_rate"] = (
                round(total_metrics[m] / total_n, 4) if total_n else None
            )

    harmonization = _merge_harmonization(center_results, included)
    return {
        "groups": groups,
        "totals": {"n": total_n, "metrics": total_metrics},
        "count_metrics": count_metrics,
        "coverage": {
            "included_centers": included,
            "excluded_centers": excluded,
            "complete": not excluded,
        },
        "harmonization": harmonization,
    }


def _merge_harmonization(center_results: list[dict], included: list[str]) -> dict:
    """Roll up each center's local measurement-scale classification.

    NOTE: these are *diagnostic* counts only — how many raw results were
    comparable vs left-censored / out of calibrated range / in a QC-failed lot.
    They are never used to replace a missing value with a number.
    """
    per_var: dict[str, dict] = {}
    by_center = {cr["center"]: cr for cr in center_results}
    for code in included:
        cr = by_center[code]
        harm = (cr.get("aggregates") or {}).get("harmonization") or {}
        for var, hv in harm.items():
            v = per_var.setdefault(var, {
                "variable": var,
                "centers": {},
                "totals": {"values_total": 0, "comparable": 0,
                           "counts": {}},
            })
            v["centers"][code] = hv
            v["totals"]["values_total"] += hv.get("values_total", 0)
            v["totals"]["comparable"] += hv.get("comparable", 0)
            for cls, n in (hv.get("counts") or {}).items():
                v["totals"]["counts"][cls] = v["totals"]["counts"].get(cls, 0) + n
    for var, v in per_var.items():
        t = v["totals"]
        t["non_comparable"] = t["values_total"] - t["comparable"]
        t["non_comparable_ratio"] = (
            round(t["non_comparable"] / t["values_total"], 4)
            if t["values_total"] else None
        )
        # centers whose lab metric is NOT group-comparable at all
        v["non_comparable_centers"] = sorted(
            code for code, hv in v["centers"].items() if not hv.get("enabled"))
    return per_var


def count_metrics_for(request_spec: dict) -> list[str]:
    ms = ["n"]
    for m in request_spec.get("metrics", []):
        if m["type"] == "outcome_count":
            ms.append(f"outcome_{m['value']}_n")
        elif m["type"] == "standardized_mean":
            # comparable-value count is a count metric: protects against a
            # mean resting on a single patient's measurement
            ms.append(f"{m['variable']}_std_n")
    return ms
