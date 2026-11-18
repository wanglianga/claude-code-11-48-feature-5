"""Seed demo metadata: users, centers, dictionary v1, variable mappings and
locked data batches (batch metadata is pulled from the running center agents,
mirroring the real "center submits, data admin registers and locks" flow)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .agents import get_agent_client
from .harmonize import fit_assay, sync_assay
from .models import (
    CalibrationSample,
    Center,
    CenterAssay,
    CenterMapping,
    CohortRule,
    DataBatch,
    Dictionary,
    RuleVersion,
    User,
    Variable,
)
from .security import content_hash, hash_password

# Keep in sync with center_agent/center_agent/datagen.py CENTER_FIELDS.
CENTER_FIELDS = {
    "C1": {"patient.sex": "pat_sex", "patient.birth_year": "pat_byear",
           "diagnosis.code": "dx_code", "diagnosis.date": "dx_date",
           "treatment.path": "tx_path", "treatment.start_date": "tx_start",
           "followup.outcome": "fu_outcome", "followup.date": "fu_date",
           "followup.lab_value": "fu_lab"},
    "C2": {"patient.sex": "pat_sex", "patient.birth_year": "pat_byear",
           "diagnosis.code": "dx_code", "diagnosis.date": "dx_date",
           "treatment.path": "tx_path", "treatment.start_date": "tx_start",
           "followup.outcome": "fu_outcome", "followup.date": "fu_date",
           "followup.lab_value": "fu_lab"},
    "C3": {"patient.sex": "gender", "patient.birth_year": "yob",
           "diagnosis.code": "diag_code", "diagnosis.date": "diag_date",
           "treatment.path": "treat_path", "treatment.start_date": "treat_start",
           "followup.outcome": "outcome", "followup.date": "visit_date",
           "followup.lab_value": "lab"},
    "C4": {"patient.sex": "sex", "patient.birth_year": "birth_year",
           "diagnosis.code": "dx_code", "diagnosis.date": "dx_date",
           "treatment.path": "path", "treatment.start_date": "start_date",
           "followup.outcome": "outcome", "followup.date": "fu_date",
           "followup.lab_value": "lab_value"},
    "C5": {"patient.sex": "pat_sex", "patient.birth_year": "pat_byear",
           "diagnosis.code": "dx_code", "diagnosis.date": "dx_date",
           "treatment.path": "tx_path", "treatment.start_date": "tx_start",
           "followup.outcome": "fu_outcome", "followup.date": "fu_date"},
    "C6": {"patient.sex": "SEX", "patient.birth_year": "BYEAR",
           "diagnosis.code": "DX", "diagnosis.date": "DXD",
           "treatment.path": "PATH", "treatment.start_date": "TXD",
           "followup.outcome": "OUTCOME", "followup.date": "FUD",
           "followup.lab_value": "LAB"},
}

C2_OUTCOME_VALUE_MAP = {"R": "remission", "S": "stable", "P": "progression", "D": "death"}

VARIABLES = [
    ("patient.sex", "patient", "性别", "categorical", ["F", "M"]),
    ("patient.birth_year", "patient", "出生年份", "numeric", None),
    ("diagnosis.code", "diagnosis", "诊断编码", "categorical", ["RD01", "RD02", "RD03", "RD04"]),
    ("diagnosis.date", "diagnosis", "诊断日期", "date", None),
    ("treatment.path", "treatment", "治疗路径", "categorical", ["A", "B", "C"]),
    ("treatment.start_date", "treatment", "治疗开始日期", "date", None),
    ("followup.outcome", "followup", "随访结局", "categorical",
     ["remission", "stable", "progression", "death"]),
    ("followup.date", "followup", "随访日期", "date", None),
    ("followup.lab_value", "followup", "关键实验室指标", "numeric", None),
]

CENTER_NAMES = {
    "C1": "华东罕见病中心", "C2": "北方儿童医院", "C3": "华南综合医院",
    "C4": "西部医学中心", "C5": "中部专科医院", "C6": "边疆协作诊所",
}

USERS = [
    ("admin", "admin123", "data_admin", "数据管理员"),
    ("researcher1", "research123", "researcher", "研究者·甲"),
    ("researcher2", "research123", "researcher", "研究者·乙"),
    ("reviewer", "review123", "reviewer", "结果审查员"),
]

DEMO_RULE_CONDITIONS = [
    {"cid": "inc-dx", "kind": "include", "variable": "diagnosis.code",
     "op": "in", "value": ["RD01", "RD02", "RD03"]},
    {"cid": "exc-young", "kind": "exclude", "variable": "patient.birth_year",
     "op": "gt", "value": 2015},
]


def wait_for_agents(client, centers: list[str], timeout_s: int = 45) -> None:
    deadline = time.time() + timeout_s
    pending = set(centers)
    while pending and time.time() < deadline:
        for code in list(pending):
            try:
                client.meta(code)
                pending.discard(code)
            except Exception:
                pass
        if pending:
            time.sleep(1.5)
    if pending:
        raise RuntimeError(f"center agents not reachable: {sorted(pending)}")


def _seed_calibration(db: Session, client, centers: dict[str, Center]) -> None:
    """Register assay metadata from the agents and fit demo calibration models.

    C2 is seeded with a first model (v1) fitted on its original lot only; a
    later re-fit is what demonstrates that a *new* calibration version only
    affects new runs. Centers whose calibration cannot be validated (C3 single
    lot bridge, C5 no bridge pairs) get an explicit rejected version instead of
    being silently pooled.
    """
    for code, center in centers.items():
        try:
            payload = client.assay(code)
        except Exception:
            continue
        sync_assay(db, center, payload)
    db.commit()

    admin_id = db.query(User).filter(User.username == "admin").first().id
    order = ["C1", "C2", "C4", "C6", "C3", "C5"]
    by_code = {c.code: c for c in db.query(Center)}
    for code in order:
        center = by_code.get(code)
        if not center:
            continue
        assay = (db.query(CenterAssay)
                 .filter(CenterAssay.center_id == center.id).first())
        if not assay:
            continue
        if code == "C2":
            # Fit v1 using only the original lot, then restore the drifted
            # lot's samples so a later refit can include them.
            drifted = [s for s in db.query(CalibrationSample)
                       .filter(CalibrationSample.assay_id == assay.id,
                               CalibrationSample.lot != assay.reagent_lot)]
            for s in drifted:
                db.delete(s)
            db.commit()
            fit_assay(db, assay, admin_id)
        else:
            fit_assay(db, assay, admin_id)
    db.commit()


def seed(db: Session, client=None, wait: bool = True) -> None:
    if db.query(User).count() > 0:
        return
    client = client or get_agent_client()

    for username, pwd, role, display in USERS:
        db.add(User(username=username, password_hash=hash_password(pwd),
                    role=role, display_name=display))

    centers: dict[str, Center] = {}
    from .config import settings

    for code, name in CENTER_NAMES.items():
        c = Center(code=code, name=name,
                   agent_url=settings.center_agents.get(code, ""))
        db.add(c)
        centers[code] = c
    db.flush()

    dictionary = Dictionary(version="v1.0", status="active")
    db.add(dictionary)
    db.flush()
    for name, entity, label, vtype, allowed in VARIABLES:
        db.add(Variable(dict_id=dictionary.id, name=name, entity=entity,
                        label=label, value_type=vtype, allowed_values=allowed,
                        unit="mg/L" if name == "followup.lab_value" else None))
    db.flush()

    for code, fields in CENTER_FIELDS.items():
        for var in [v[0] for v in VARIABLES]:
            local = fields.get(var)
            value_map = C2_OUTCOME_VALUE_MAP if (code == "C2" and var == "followup.outcome") else None
            db.add(CenterMapping(
                center_id=centers[code].id, dict_id=dictionary.id,
                variable_name=var, local_field=local,
                value_map=value_map,
                status="mapped" if local else "missing",
            ))
    db.flush()

    if wait:
        wait_for_agents(client, list(CENTER_NAMES))
    for code, center in centers.items():
        meta = client.meta(code)
        db.add(DataBatch(
            center_id=center.id, label="2026Q3", dict_id=dictionary.id,
            status="locked", locked_at=datetime.now(timezone.utc),
            record_counts={"patients": meta["patients"]},
            available_scope={"dx_years": meta.get("dx_years", []),
                             "fields": meta.get("fields", [])},
        ))
    db.flush()

    _seed_calibration(db, client, centers)

    # Demo cohort rule, already approved so a first run can start immediately.
    rule = CohortRule(name="罕见病治疗路径结局对比", owner_id=1)
    db.add(rule)
    db.flush()
    version = RuleVersion(
        rule_id=rule.id, version_no=1, status="approved",
        conditions=DEMO_RULE_CONDITIONS,
        conditions_hash=content_hash(DEMO_RULE_CONDITIONS),
        created_by=2, submitted_at=datetime.now(timezone.utc),
        reviewed_by=4, reviewed_at=datetime.now(timezone.utc),
        review_comment="初始版本，审查通过",
    )
    db.add(version)
    db.flush()
    rule.current_version_id = version.id
    db.commit()
