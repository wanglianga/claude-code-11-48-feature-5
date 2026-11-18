"""HTTP wrapper around the center-local executor.

One agent process serves exactly one center. The orchestrator may ask for
metadata (record counts, available fields) and submit aggregate-only jobs;
individual records are never exposed on any endpoint.

Run: python -m center_agent.server --center C1 --port 9101 --data data/centers/C1
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import executor


class Job(BaseModel):
    job_id: str | None = None
    dict_version: str | None = None
    conditions: list[dict] = []
    request_spec: dict
    mapping: dict[str, dict]
    # variable -> calibration transform snapshot (validated, versioned platform-side)
    calibrations: dict[str, dict] = {}


def create_app(center: str, data_dir: str) -> FastAPI:
    app = FastAPI(title=f"center-agent-{center}", docs_url=None, redoc_url=None)
    root = Path(data_dir)

    @app.get("/health")
    def health():
        return {"status": "ok", "center": center}

    @app.get("/meta")
    def meta():
        csv_path = root / "registry.csv"
        if not csv_path.exists():
            raise HTTPException(503, "no registry data loaded")
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            n = sum(1 for _ in reader)
        meta_file = root / "meta.json"
        extra = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        return {
            "center": center,
            "patients": n,
            "fields": fields,
            "dx_years": extra.get("dx_years", []),
            "seed": extra.get("seed"),
        }

    @app.get("/assay")
    def assay():
        """Center-registered assay metadata + de-identified calibration samples.

        Only platform/reagent/QC/calibration metadata and de-identified
        reference or bridge sample values are exposed — never patient records.
        """
        path = root / "assay.json"
        if not path.exists():
            raise HTTPException(404, "no assay metadata registered for this center")
        return json.loads(path.read_text())

    @app.post("/execute")
    def execute(job: Job):
        try:
            return executor.execute(root, job.model_dump())
        except Exception as e:  # unexpected local failure -> reported, not zeroed
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--center", required=True)
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--data", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    import uvicorn

    app = create_app(args.center, args.data)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
