"""ORM models for the controlled multi-center analysis platform."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32))  # data_admin | researcher | reviewer
    display_name: Mapped[str] = mapped_column(String(128))


class Center(Base):
    __tablename__ = "centers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # C1..C6
    name: Mapped[str] = mapped_column(String(128))
    agent_url: Mapped[str] = mapped_column(String(256))


class Dictionary(Base):
    __tablename__ = "dictionaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    variables: Mapped[list["Variable"]] = relationship(back_populates="dictionary")


class Variable(Base):
    __tablename__ = "variables"
    __table_args__ = (UniqueConstraint("dict_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dict_id: Mapped[int] = mapped_column(ForeignKey("dictionaries.id"))
    name: Mapped[str] = mapped_column(String(64))  # e.g. followup.outcome
    entity: Mapped[str] = mapped_column(String(32))  # patient|diagnosis|treatment|followup
    label: Mapped[str] = mapped_column(String(128))
    value_type: Mapped[str] = mapped_column(String(16))  # categorical|numeric|date
    allowed_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # reporting unit of a numeric variable (e.g. mg/L); equal units do NOT imply
    # a common measurement scale across centers.
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dictionary: Mapped[Dictionary] = relationship(back_populates="variables")


class CenterMapping(Base):
    """Maps a canonical dictionary variable to a center-local CSV field."""

    __tablename__ = "center_mappings"
    __table_args__ = (UniqueConstraint("center_id", "dict_id", "variable_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    dict_id: Mapped[int] = mapped_column(ForeignKey("dictionaries.id"))
    variable_name: Mapped[str] = mapped_column(String(64))
    local_field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="mapped")  # mapped|missing


class DataBatch(Base):
    """A data batch registered by a center; locked batches are immutable run inputs."""

    __tablename__ = "data_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    label: Mapped[str] = mapped_column(String(64))  # e.g. 2026Q3
    dict_id: Mapped[int] = mapped_column(ForeignKey("dictionaries.id"))
    status: Mapped[str] = mapped_column(String(16), default="registered")
    # registered -> locked -> superseded (when a newer batch is locked)
    record_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    available_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    center: Mapped[Center] = relationship()


class CohortRule(Base):
    __tablename__ = "cohort_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("cohort_rules.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    # draft -> in_review -> approved | rejected ; a newer draft supersedes for editing
    status: Mapped[str] = mapped_column(String(16), default="draft")
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    conditions_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule: Mapped[CohortRule] = relationship()


class Run(Base):
    """One orchestrated multi-center execution of an approved rule version."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"))
    request_spec: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # pending -> running -> completed | completed_with_gaps | failed
    k_threshold: Mapped[int] = mapped_column(Integer, default=5)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rule_version: Mapped[RuleVersion] = relationship()
    center_results: Mapped[list["RunCenterResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    batches: Mapped[list["RunBatch"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RunBatch(Base):
    """Immutable snapshot: which locked batch each center contributed to a run."""

    __tablename__ = "run_batches"
    __table_args__ = (UniqueConstraint("run_id", "center_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("data_batches.id"))
    batch_label: Mapped[str] = mapped_column(String(64))
    run: Mapped[Run] = relationship(back_populates="batches")
    batch: Mapped[DataBatch] = relationship()


class RunCenterResult(Base):
    __tablename__ = "run_center_results"
    __table_args__ = (UniqueConstraint("run_id", "center_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # pending | success | failed | missing_mapping
    aggregates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run: Mapped[Run] = relationship(back_populates="center_results")
    center: Mapped[Center] = relationship()


class OutputTable(Base):
    """An aggregated result table a researcher wants to export; needs review."""

    __tablename__ = "output_tables"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    title: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(24), default="pending_review")
    # pending_review | approved | rejected
    table_data: Mapped[dict] = mapped_column(JSON)  # groups + totals + coverage
    flags: Mapped[list] = mapped_column(JSON, default=list)  # small-cell findings
    k_threshold: Mapped[int] = mapped_column(Integer, default=5)
    run: Mapped[Run] = relationship()


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    output_id: Mapped[int] = mapped_column(ForeignKey("output_tables.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(16))  # approve | suppress | merge | reject
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    output: Mapped[OutputTable] = relationship()


class PublishedReport(Base):
    """Approved output frozen with full provenance for reproducibility."""

    __tablename__ = "published_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    output_id: Mapped[int] = mapped_column(ForeignKey("output_tables.id"), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"))
    decision_id: Mapped[int] = mapped_column(ForeignKey("review_decisions.id"))
    published_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    report: Mapped[dict] = mapped_column(JSON)  # frozen table + provenance + hashes
    report_hash: Mapped[str] = mapped_column(String(64))
    output: Mapped[OutputTable] = relationship()


# ---------------------------------------------------------------------------
# Cross-center assay measurement harmonization
#
# Every center measures the same biomarker on its own platform/reagent lot.
# Raw values are never pooled directly: each center registers its platform,
# reagent lot, calibration curve, reference interval, LOD/LOQ and validity
# period. Peer-reviewed calibration models version how a center's raw value is
# converted into a group-comparable standardized value (or explicitly kept as
# censored / out-of-range / non-comparable). Runs snapshot the calibration
# versions they used, so a new calibration only affects new runs.
# ---------------------------------------------------------------------------


class AssayPlatform(Base):
    """Group-level platform catalog entry (instrument + reagent family)."""

    __tablename__ = "assay_platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # e.g. P-IMMUNO-A
    name: Mapped[str] = mapped_column(String(160))
    manufacturer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    method: Mapped[str] = mapped_column(String(64))  # e.g. CLIA / ELISA / LC-MS
    # Traceability route: traceable (common reference material available) or
    # bridged (no common material; paired reference samples used instead).
    traceability: Mapped[str] = mapped_column(String(24), default="bridged")
    reference_material: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CenterAssay(Base):
    """A center's registration of how it measures a numeric dictionary variable."""

    __tablename__ = "center_assays"
    __table_args__ = (UniqueConstraint("center_id", "variable"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    platform_id: Mapped[int] = mapped_column(ForeignKey("assay_platforms.id"))
    variable: Mapped[str] = mapped_column(String(64))  # e.g. followup.lab_value
    unit: Mapped[str] = mapped_column(String(32), default="mg/L")
    reagent_lot: Mapped[str] = mapped_column(String(64))
    # Calibration curve parameters as registered by the center (y = a + b*x)
    calibration_curve: Mapped[dict] = mapped_column(JSON, default=dict)
    reference_interval: Mapped[dict] = mapped_column(JSON, default=dict)  # low/high
    # Analytical measuring range; values outside are NOT comparable.
    measuring_range: Mapped[dict] = mapped_column(JSON, default=dict)  # low/high
    # Detection limit (below -> censored_left) and quantification limit.
    detection_limit: Mapped[float] = mapped_column(Float, default=0.0)
    quantitation_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_reportable: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_from: Mapped[str] = mapped_column(String(10))  # ISO date
    valid_to: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="registered")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    center: Mapped[Center] = relationship()
    platform: Mapped[AssayPlatform] = relationship()


class QCBatch(Base):
    """Reagent-lot quality-control batch. Failed QC excludes that lot's values."""

    __tablename__ = "qc_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    assay_id: Mapped[int] = mapped_column(ForeignKey("center_assays.id"))
    lot: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[str] = mapped_column(String(10))
    period_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    level: Mapped[str] = mapped_column(String(24), default="normal")
    observed_cv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    allowed_cv_pct: Mapped[float] = mapped_column(Float, default=10.0)
    status: Mapped[str] = mapped_column(String(16), default="passed")  # passed|failed
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    assay: Mapped[CenterAssay] = relationship()


class CalibrationSample(Base):
    """Reference material measured at centers.

    kind=reference: same reference standard run on several platforms (route A;
    'reference_value' is the assigned value of the standard material).
    kind=bridge: paired patient/reference samples measured locally by two
    platforms (route B; 'partner_center_id'/'partner_value' are the paired
    measurement). No patient identifiers are stored — only assay values.
    """

    __tablename__ = "calibration_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    assay_id: Mapped[int] = mapped_column(ForeignKey("center_assays.id"))
    sample_code: Mapped[str] = mapped_column(String(64))  # de-identified code
    kind: Mapped[str] = mapped_column(String(16))  # reference|bridge
    reference_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    measured_value: Mapped[float] = mapped_column(Float)
    lot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partner_center_id: Mapped[int | None] = mapped_column(ForeignKey("centers.id"),
                                                          nullable=True)
    partner_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    measured_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # train|holdout|rejected — hold-out points validate the fitted transfer.
    role: Mapped[str] = mapped_column(String(16), default="train")
    assay: Mapped[CenterAssay] = relationship()


class CalibrationModel(Base):
    """A fitted, versioned transfer that standardizes one center's raw values.

    transform: y = a + b * x (b may be None when the center is not convertible).
    Only values inside [range_low, range_high] and inside the center's measuring
    range are comparable. Each fit supersedes the previous active model of the
    same assay; runs keep referencing the model version they snapshotted.
    """

    __tablename__ = "calibration_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    assay_id: Mapped[int] = mapped_column(ForeignKey("center_assays.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    route: Mapped[str] = mapped_column(String(16))  # traceable|bridge|none
    status: Mapped[str] = mapped_column(String(16), default="validated")
    # validated | rejected (insufficient/unstable) | superseded | active
    transform: Mapped[dict] = mapped_column(JSON, default=dict)
    # {slope, intercept, range_low, range_high, unit, detection_limit,
    #  quantitation_limit, upper_reportable, flags:[comparability limits]}
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    # {n_train, n_holdout, rmse, mae, bias, r2, max_rel_error, passed}
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    fit_params: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assay: Mapped[CenterAssay] = relationship()


class RunCalibration(Base):
    """Immutable snapshot: which calibration model version a center used in a run."""

    __tablename__ = "run_calibrations"
    __table_args__ = (UniqueConstraint("run_id", "center_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    variable: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_models.id"),
                                                 nullable=True)
    model_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    transform: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="not_applied")
    # applied | not_applied (no metric requested) | unavailable (no active model)
    run: Mapped[Run] = relationship()
