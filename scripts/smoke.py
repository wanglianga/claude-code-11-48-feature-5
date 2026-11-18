#!/usr/bin/env python
"""End-to-end smoke verification against a RUNNING platform (real PostgreSQL +
HTTP center agents). Exercises every acceptance scenario:

  1. seeded users / centers / locked batches
  2. run over 6 centers; missing-mapping center is a coverage gap, not a zero
  3. batch replacement mid-stream: old run keeps its snapshot, new run picks up
  4. concurrent rule editing: 409 + condition-level diff + confirmed merge;
     in-review restrictions cannot be dropped silently
  5. small-sample output: review blocks, merge+suppress releases, published
     report carries full provenance and hash-verifies
  6. run comparison endpoint

Usage: .venv/bin/python scripts/smoke.py  [BASE_URL]
"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(1)


def login(username: str, password: str) -> dict:
    r = httpx.post(f"{BASE}/api/auth/login",
                   json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main() -> None:
    admin = login("admin", "admin123")
    r1 = login("researcher1", "research123")
    r2 = login("researcher2", "research123")
    rev = login("reviewer", "review123")
    check("四类账号登录", True)

    centers = httpx.get(f"{BASE}/api/admin/centers", headers=admin).json()
    check("六家中心均已注册并锁定批次",
          len(centers) == 6 and all(c["locked_batch"] for c in centers))

    # ---- 2. runs & coverage gaps ------------------------------------------------
    spec = {"group_by": ["treatment.path"],
            "metrics": [{"type": "count"},
                        {"type": "outcome_count", "variable": "followup.outcome",
                         "value": "remission"}]}
    run_full = httpx.post(f"{BASE}/api/runs", headers=r1,
                          json={"rule_version_id": 1, "request_spec": spec}).json()
    check("全量运行完成（6/6 中心成功）",
          run_full["status"] == "completed" and run_full["centers_failed"] == 0)
    c5_n = next(c for c in run_full["center_results"]
                if c["center_code"] == "C5")["total_n"]

    spec_lab = dict(spec, metrics=spec["metrics"] + [
        {"type": "mean", "variable": "followup.lab_value"}])
    run_gap = httpx.post(f"{BASE}/api/runs", headers=r1,
                         json={"rule_version_id": 1, "request_spec": spec_lab}).json()
    c5 = next(c for c in run_gap["center_results"] if c["center_code"] == "C5")
    cov = run_gap["result"]["coverage"]
    check("C5 缺 lab 字段 → 标记 missing_mapping 而非零例",
          run_gap["status"] == "completed_with_gaps"
          and c5["status"] == "missing_mapping" and c5["total_n"] is None
          and [e["center"] for e in cov["excluded_centers"]] == ["C5"])
    check("覆盖缺口不计入总计",
          run_gap["result"]["totals"]["n"] == run_full["result"]["totals"]["n"] - c5_n,
          f"{run_gap['result']['totals']['n']} = "
          f"{run_full['result']['totals']['n']} - {c5_n}")

    # ---- 2b. cross-center assay calibration -------------------------------------
    # Establish a deterministic C2 baseline first (drifted lot excluded) so this
    # smoke is idempotent when re-run against the same database.
    httpx.post(f"{BASE}/api/harmonization/centers/C2/sync", headers=admin)
    base = httpx.post(f"{BASE}/api/harmonization/assays/C2/fit", headers=admin,
                      json={"exclude_lots": ["B-2502"]}).json()
    base_v = base["version_no"]
    check("C2 基线校准（排除漂移批次）为可比", base["status"] == "validated")

    cov = httpx.get(f"{BASE}/api/harmonization/coverage", headers=r1).json()
    by = {c["center_code"]: c for c in cov["centers"]}
    check("校准覆盖：可溯源/双批次桥接中心可比，单批次桥接与无桥接样本中心不可比",
          by["C1"]["comparable"] and by["C2"]["comparable"]
          and by["C3"]["comparable"] and by["C4"]["comparable"]
          and not by["C6"]["comparable"] and not by["C5"]["comparable"]
          and any("bridge_lot_coverage" in x for x in by["C6"]["reasons"]))
    bias = httpx.get(f"{BASE}/api/harmonization/bias/C1", headers=r1).json()
    check("偏差诊断：原始偏差经标准化后收敛",
          abs(bias["diagnostics"]["standardized_bias"])
          < abs(bias["diagnostics"]["raw_bias"]) + 1e-9
          and "by_lot" in bias["diagnostics"])

    std_spec = {"group_by": ["treatment.path"],
                "metrics": [{"type": "count"},
                            {"type": "standardized_mean",
                             "variable": "followup.lab_value"}]}
    run_std = httpx.post(f"{BASE}/api/runs", headers=r1,
                         json={"rule_version_id": 1,
                               "request_spec": std_spec}).json()
    hv = run_std["result"]["harmonization"]["followup.lab_value"]
    check("标准化运行输出可比范围与无法比较比例（不插补为零）",
          hv["totals"]["values_total"] > 0
          and hv["totals"]["comparable"] < hv["totals"]["values_total"]
          and hv["totals"]["counts"]["censored_low"] >= 1
          and hv["totals"]["counts"]["qc_failed"] >= 1
          and hv["totals"]["counts"]["above_range"] >= 1
          and hv["totals"]["counts"]["outside_validity"] >= 1
          and "C6" in hv["non_comparable_centers"])
    c2_snap = next(c for c in run_std["calibrations"] if c["center_code"] == "C2")
    check("运行快照记录每家中心的校准版本与转换哈希",
          c2_snap["model_version_no"] == base_v and c2_snap["transform_hash"]
          and len(run_std["calibrations"]) == 6)

    # ---- 2c. reagent-lot drift + new calibration only affects new runs ---------
    drift = httpx.post(f"{BASE}/api/harmonization/assays/C2/fit", headers=admin,
                       json={}).json()
    check("试剂批次漂移诊断：含漂移批次的拟合被拒绝为不可比",
          drift["status"] == "rejected"
          and any(d["lot"] == "B-2502" for d in drift["diagnostics"].get("lot_drift", [])))
    refit = httpx.post(f"{BASE}/api/harmonization/assays/C2/fit", headers=admin,
                       json={"exclude_lots": ["B-2502"]}).json()
    check("C2 重新拟合生成新校准版本（排除漂移批次）",
          refit["version_no"] == drift["version_no"] + 1
          and refit["status"] == "validated"
          and "B-2502" in (refit["notes"] or ""))
    old_c2 = next(c for c in httpx.get(
        f"{BASE}/api/runs/{run_std['id']}", headers=r1).json()["calibrations"]
        if c["center_code"] == "C2")
    check("历史运行保留当时转换函数（版本复现）",
          old_c2["model_version_no"] == base_v
          and old_c2["transform_hash"] == c2_snap["transform_hash"])
    run_std2 = httpx.post(f"{BASE}/api/runs", headers=r1,
                          json={"rule_version_id": 1,
                                "request_spec": std_spec}).json()
    new_c2 = next(c for c in run_std2["calibrations"] if c["center_code"] == "C2")
    hv2 = run_std2["result"]["harmonization"]["followup.lab_value"]["centers"]["C2"]
    check("新校准仅用于新运行，且漂移批次样本不再可比",
          new_c2["model_version_no"] == refit["version_no"]
          and new_c2["transform_hash"] != c2_snap["transform_hash"]
          and hv2["counts"]["qc_failed"] >= 1)

    # ---- 3. batch replacement keeps run snapshots -------------------------------
    batches_a = {b["center_code"]: b["batch_id"] for b in run_full["batches"]}
    new_batch = httpx.post(f"{BASE}/api/admin/centers/C2/batches/sync",
                           headers=admin, json={"label": "2026Q4"}).json()
    httpx.post(f"{BASE}/api/admin/batches/{new_batch['id']}/lock", headers=admin)
    again = httpx.get(f"{BASE}/api/runs/{run_full['id']}", headers=r1).json()
    check("批次替换后旧运行仍引用启动时锁定的批次",
          {b["center_code"]: b["batch_id"] for b in again["batches"]} == batches_a
          and again["result"]["totals"]["n"] == run_full["result"]["totals"]["n"])
    run_new = httpx.post(f"{BASE}/api/runs", headers=r1,
                         json={"rule_version_id": 1, "request_spec": spec}).json()
    batches_b = {b["center_code"]: b["batch_id"] for b in run_new["batches"]}
    check("新运行使用替换后的批次", batches_b["C2"] == new_batch["id"]
          and batches_b["C2"] != batches_a["C2"])

    cmp_res = httpx.get(f"{BASE}/api/runs/compare?a={run_full['id']}&b={run_new['id']}",
                        headers=r1).json()
    check("运行对比接口返回两次运行的批次与结果",
          cmp_res["a"]["batches"] and cmp_res["b"]["batches"])

    # ---- 4. concurrent rule editing ---------------------------------------------
    conds = [
        {"cid": "inc-dx", "kind": "include", "variable": "diagnosis.code",
         "op": "in", "value": ["RD01", "RD02"]},
        {"cid": "exc-kid", "kind": "exclude", "variable": "patient.birth_year",
         "op": "gt", "value": 2015},
    ]
    rule = httpx.post(f"{BASE}/api/rules", headers=r1,
                      json={"name": "并发演示规则", "conditions": conds}).json()
    v1 = rule["versions"][0]
    httpx.post(f"{BASE}/api/rules/{rule['id']}/versions/{v1['id']}/submit", headers=r1)

    conds_r1 = conds + [{"cid": "inc-sex", "kind": "include",
                         "variable": "patient.sex", "op": "eq", "value": "F"}]
    saved = httpx.put(f"{BASE}/api/rules/{rule['id']}/save", headers=r1,
                      json={"base_version_id": v1["id"], "conditions": conds_r1}).json()
    v2 = next(v for v in saved["rule"]["versions"] if v["version_no"] == 2)

    conflict = httpx.put(f"{BASE}/api/rules/{rule['id']}/save", headers=r2,
                         json={"base_version_id": v1["id"],
                               "conditions": [conds[0]]})  # drops the exclusion
    check("并发保存返回 409 与条件级差异", conflict.status_code == 409
          and conflict.json()["error"] == "concurrent_modification"
          and conflict.json()["their_diff"]["added"])
    merged = conflict.json()["merged"]
    # merged drops the in-review exclusion -> must also be acknowledged
    refused = httpx.put(f"{BASE}/api/rules/{rule['id']}/save", headers=r2,
                        json={"base_version_id": v2["id"], "conditions": merged,
                              "confirm_merge": True})
    check("合并结果删除在审排除条件 → 需显式确认",
          refused.status_code == 409
          and refused.json()["error"] == "restriction_removal")
    confirmed = httpx.put(f"{BASE}/api/rules/{rule['id']}/save", headers=r2,
                          json={"base_version_id": v2["id"], "conditions": merged,
                                "confirm_merge": True,
                                "acknowledge_restriction_removal": True})
    check("确认合并后保存成功且在审版本未被抹掉",
          confirmed.status_code == 200
          and any(v["status"] == "in_review"
                  for v in confirmed.json()["rule"]["versions"]))

    # ---- 5. small-sample review flow --------------------------------------------
    small_conds = [
        {"cid": "inc-dx", "kind": "include", "variable": "diagnosis.code",
         "op": "eq", "value": "RD03"},
        {"cid": "inc-yob", "kind": "include", "variable": "patient.birth_year",
         "op": "ge", "value": 2005},
    ]
    srule = httpx.post(f"{BASE}/api/rules", headers=r1,
                       json={"name": "小样本队列", "conditions": small_conds}).json()
    sv1 = srule["versions"][0]
    httpx.post(f"{BASE}/api/rules/{srule['id']}/versions/{sv1['id']}/submit", headers=r1)
    httpx.post(f"{BASE}/api/rules/versions/{sv1['id']}/approve",
               headers=rev, json={"comment": "同意"})
    srun = httpx.post(f"{BASE}/api/runs", headers=r1, json={
        "rule_version_id": sv1["id"],
        "request_spec": {"group_by": ["treatment.path", "patient.sex"],
                         "metrics": [{"type": "count"},
                                     {"type": "outcome_count",
                                      "variable": "followup.outcome",
                                      "value": "remission"}]}}).json()
    out = httpx.post(f"{BASE}/api/runs/{srun['id']}/outputs", headers=r1,
                     json={"title": "小样本输出（冒烟）"}).json()
    check("小样本输出被标记并进入审查队列", out["flag_count"] > 0
          and out["status"] == "pending_review")
    blocked = httpx.post(f"{BASE}/api/outputs/{out['id']}/decision",
                         headers=rev, json={"action": "approve"})
    check("存在未处理标记时批准被拒绝", blocked.status_code == 409)
    step = httpx.post(f"{BASE}/api/outputs/{out['id']}/decision", headers=rev,
                      json={"action": "merge",
                            "detail": {"groups": ["C / F", "C / M"],
                                       "into": "C（合并）"}}).json()
    cells = [{"group": f["group"], "metric": f["metric"]} for f in step["flags"]]
    done = httpx.post(f"{BASE}/api/outputs/{out['id']}/decision", headers=rev,
                      json={"action": "suppress", "detail": {"cells": cells},
                            "comment": "合并+抑制后放行"}).json()
    check("合并 + 抑制后输出获批并发布",
          done["status"] == "approved" and done.get("published_report_id"))
    rep = httpx.get(f"{BASE}/api/reports/{done['published_report_id']}",
                    headers=rev).json()
    prov = rep["report"]["provenance"]
    check("发布报告引用完整运行版本（规则/批次/审查）",
          prov["run_id"] == srun["id"] and len(prov["batches"]) == 6
          and prov["conditions_hash"] and prov["review"]["action"] == "suppress")
    ver = httpx.get(f"{BASE}/api/reports/{done['published_report_id']}/verify",
                    headers=rev).json()
    check("报告哈希可复现校验", ver["intact"] is True)

    print(f"\n全部 {len(CHECKS)} 项冒烟检查通过")


if __name__ == "__main__":
    main()
