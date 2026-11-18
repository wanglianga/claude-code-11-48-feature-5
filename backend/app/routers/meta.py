"""Read-only metadata for every authenticated user (variable dictionary etc.)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Dictionary, User

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/dictionary")
def active_dictionary(db: Session = Depends(get_db), _: User = Depends(current_user)):
    d = db.query(Dictionary).filter(Dictionary.status == "active").first()
    if not d:
        return {"version": None, "variables": []}
    return {
        "id": d.id,
        "version": d.version,
        "variables": [
            {"name": v.name, "entity": v.entity, "label": v.label,
             "value_type": v.value_type, "allowed_values": v.allowed_values,
             "unit": v.unit}
            for v in d.variables
        ],
    }
