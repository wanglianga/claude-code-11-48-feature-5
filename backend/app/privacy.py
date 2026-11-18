"""Small-cell suppression analysis for aggregated output tables.

Table shape (table_data):
{
  "groups": [{"key": {...}, "label": "...", "n": 42,
              "metrics": {"remission_n": 30, "remission_rate": 0.71,
                          "followup.lab_value_mean": 5.2},
              "suppressed": ["remission_n"]}],
  "totals": {"n": 100, "metrics": {...}},
  "count_metrics": ["n", "remission_n"],   # metrics subject to k-threshold
  "coverage": {...}
}

Rules:
- 0 < count < k            -> cell must be suppressed (small_count)
- 0 < group n < k          -> every metric of the group is hidden (small_group)
- exactly one hidden cell in a count metric while the column total is visible
                             -> derivable by differencing (differencing)
"""
from __future__ import annotations


def _hidden(group: dict, metric: str, k: int) -> bool:
    if metric in group.get("suppressed", []):
        return True
    v = group["metrics"].get(metric, group.get("n") if metric == "n" else None)
    if v is None:
        return True
    if metric == "n" and isinstance(v, (int, float)):
        return 0 < v < k
    return False


def analyze_table(table: dict, k: int) -> list[dict]:
    """Return a list of findings; empty list means the table is releasable."""
    flags: list[dict] = []
    groups = table.get("groups", [])
    count_metrics = table.get("count_metrics", ["n"])
    totals = table.get("totals", {})

    for g in groups:
        n = g.get("n", 0)
        small_group = 0 < n < k
        for m in count_metrics:
            v = g.get("n") if m == "n" else g["metrics"].get(m)
            if v is None or m in g.get("suppressed", []):
                continue
            if isinstance(v, (int, float)) and 0 < v < k:
                flags.append({"group": g["label"], "metric": m, "value": v,
                              "reason": "small_group" if small_group and m == "n" else "small_count"})
        if small_group:
            for m, v in g["metrics"].items():
                if m in count_metrics or m in g.get("suppressed", []):
                    continue
                if v is not None:
                    flags.append({"group": g["label"], "metric": m, "value": v,
                                  "reason": "small_group"})

    # A standardized mean resting on a small number of comparable values can
    # expose an individual reading (n=1 == that patient's measurement), so the
    # mean travels with its comparable-count flag.
    for g in groups:
        for m in count_metrics:
            if not m.endswith("_std_n"):
                continue
            v = g["metrics"].get(m)
            if isinstance(v, (int, float)) and 0 < v < k:
                mean_m = m[: -len("_std_n")] + "_std_mean"
                if mean_m in g["metrics"] and mean_m not in g.get("suppressed", []):
                    if not any(f["group"] == g["label"] and f["metric"] == mean_m
                               for f in flags):
                        flags.append({"group": g["label"], "metric": mean_m,
                                      "value": g["metrics"][mean_m],
                                      "reason": "small_comparable_n"})

    # Differencing: a single hidden cell in a count column is derivable from the total.
    for m in count_metrics:
        total = totals.get("n") if m == "n" else totals.get("metrics", {}).get(m)
        if total is None:
            continue
        hidden = [g for g in groups if _hidden(g, m, k)]
        if len(hidden) == 1 and isinstance(total, (int, float)):
            g = hidden[0]
            if not any(f["group"] == g["label"] and f["metric"] == m
                       and f["reason"] == "differencing" for f in flags):
                flags.append({"group": g["label"], "metric": m,
                              "value": g.get("n") if m == "n" else g["metrics"].get(m),
                              "reason": "differencing"})
        if isinstance(total, (int, float)) and 0 < total < k:
            flags.append({"group": "__total__", "metric": m, "value": total,
                          "reason": "small_count"})
    # de-duplicate identical findings
    seen, out = set(), []
    for f in flags:
        key = (f["group"], f["metric"], f["reason"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def apply_suppression(table: dict, cells: list[dict]) -> dict:
    """Hide the given [{group, metric}] cells. group "__total__" hides the
    column total (needed when a single hidden cell would be derivable)."""
    import copy

    t = copy.deepcopy(table)
    by_label = {g["label"]: g for g in t["groups"]}
    for cell in cells:
        m = cell["metric"]
        if cell["group"] == "__total__":
            if m == "n":
                t["totals"]["n"] = None
            else:
                t["totals"].get("metrics", {}).pop(m, None)
            t["totals"].setdefault("suppressed", [])
            if m not in t["totals"]["suppressed"]:
                t["totals"]["suppressed"].append(m)
            continue
        g = by_label.get(cell["group"])
        if not g:
            continue
        g.setdefault("suppressed", [])
        if m not in g["suppressed"]:
            g["suppressed"].append(m)
    return t


def apply_merge(table: dict, group_labels: list[str], into_label: str) -> dict:
    """Merge several small groups into one combined group (exact for counts,
    weighted for means via the hidden _sum/_n companions)."""
    import copy

    t = copy.deepcopy(table)
    picked = [g for g in t["groups"] if g["label"] in group_labels]
    rest = [g for g in t["groups"] if g["label"] not in group_labels]
    if len(picked) < 2:
        raise ValueError("merge requires at least two groups")

    merged = {"key": {"merged": sorted(group_labels)}, "label": into_label,
              "n": sum(g["n"] for g in picked), "metrics": {}, "suppressed": []}
    metric_names = {m for g in picked for m in g["metrics"]}
    for m in metric_names:
        sum_key, n_key = f"_{m}_sum", f"_{m}_n"
        if any(sum_key in g["metrics"] for g in picked):
            s = sum(g["metrics"].get(sum_key, 0) for g in picked)
            c = sum(g["metrics"].get(n_key, 0) for g in picked)
            merged["metrics"][m] = round(s / c, 4) if c else None
            merged["metrics"][sum_key] = s
            merged["metrics"][n_key] = c
        elif m.endswith("_rate"):
            base = m[: -len("_rate")]
            nums = sum(g["metrics"].get(f"{base}_n", 0) or 0 for g in picked)
            merged["metrics"][m] = round(nums / merged["n"], 4) if merged["n"] else None
        else:
            vals = [g["metrics"].get(m) for g in picked]
            merged["metrics"][m] = sum(v for v in vals if isinstance(v, (int, float)))
    t["groups"] = rest + [merged]
    return t
