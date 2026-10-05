"""Unified fleet data model: the one picture of the fleet that the four sources are merged into.

Concepts follow ASD S5000F (in-service data feedback) loosely: product (aircraft, type),
breakdown (positions, parts, serials, installations), usage (sorties), events (defects,
health indicators), maintenance (work orders, inspections, component changes), supply (stock,
transactions, repairs). Every row keeps the identifier it had in its source system; `dq_issue`
records every inconsistency the integration layer found and what it did about it.

Times are stored as naive UTC datetimes. The same schema runs on SQLite (default) and on
PostgreSQL; with TimescaleDB available, `health_indicator` becomes a hypertable on `ts`.
"""
from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
                        create_engine, event, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ lineage and data quality
class IngestBatch(Base):
    __tablename__ = "ingest_batch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))
    started: Mapped[datetime] = mapped_column(DateTime)
    finished: Mapped[datetime | None] = mapped_column(DateTime)
    files: Mapped[int] = mapped_column(Integer, default=0)
    records_in: Mapped[int] = mapped_column(Integer, default=0)
    records_loaded: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[int] = mapped_column(Integer, default=0)


class DQIssue(Base):
    __tablename__ = "dq_issue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_batch.id"))
    source: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    record_key: Mapped[str | None] = mapped_column(String(120))
    rule: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))           # info | warning | error
    field: Mapped[str | None] = mapped_column(String(60))
    raw_value: Mapped[str | None] = mapped_column(Text)
    resolved_value: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str] = mapped_column(String(20))         # fixed | linked | merged | flagged | dropped
    detail: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------------------ fleet structure
class Location(Base):
    __tablename__ = "location"
    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))               # base | depot


class AircraftType(Base):
    __tablename__ = "aircraft_type"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))


class Unit(Base):
    __tablename__ = "unit"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    type_id: Mapped[str] = mapped_column(ForeignKey("aircraft_type.id"))
    base: Mapped[str] = mapped_column(ForeignKey("location.code"))


class Aircraft(Base):
    __tablename__ = "aircraft"
    tail: Mapped[str] = mapped_column(String(10), primary_key=True)
    msn: Mapped[str] = mapped_column(String(20), unique=True)
    type_id: Mapped[str] = mapped_column(ForeignKey("aircraft_type.id"))
    unit_id: Mapped[str] = mapped_column(ForeignKey("unit.id"))
    base: Mapped[str] = mapped_column(ForeignKey("location.code"))
    hums_unit: Mapped[str | None] = mapped_column(String(20))


class Part(Base):
    __tablename__ = "part"
    pn: Mapped[str] = mapped_column(String(30), primary_key=True)        # current (latest) part number
    description: Mapped[str] = mapped_column(String(120))
    ata: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(1))                    # R rotable | C consumable
    unit_cost_inr: Mapped[int | None] = mapped_column(Integer)
    stock_no: Mapped[str | None] = mapped_column(String(30), unique=True)


class PartAlias(Base):
    __tablename__ = "part_alias"
    alias: Mapped[str] = mapped_column(String(40), primary_key=True)     # normalised form
    pn: Mapped[str] = mapped_column(ForeignKey("part.pn"))
    kind: Mapped[str] = mapped_column(String(20))                       # self | supersession | stock_no | description
    source: Mapped[str] = mapped_column(String(20))


class Serial(Base):
    __tablename__ = "serial"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pn: Mapped[str] = mapped_column(ForeignKey("part.pn"))
    sn: Mapped[str] = mapped_column(String(30))
    __table_args__ = (UniqueConstraint("pn", "sn"),)


class Installation(Base):
    """Which serial was in which position, when (rebuilt from component changes and life cards)."""
    __tablename__ = "installation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    position: Mapped[str] = mapped_column(String(20))
    pn: Mapped[str] = mapped_column(ForeignKey("part.pn"))
    sn: Mapped[str] = mapped_column(String(30))
    installed: Mapped[datetime | None] = mapped_column(DateTime)
    removed: Mapped[datetime | None] = mapped_column(DateTime)
    removal_reason: Mapped[str | None] = mapped_column(String(20))
    tso_h_at_export: Mapped[float | None] = mapped_column(Float)   # life card: hours since overhaul, fitted units only
    life_limit: Mapped[str | None] = mapped_column(String(60))      # life card, e.g. "fh=1500;days=730"
    __table_args__ = (Index("ix_installation_tail_pos", "tail", "position"),)


class RiskRecord(Base):
    """One prediction for one monitored component at one time (airpower/health/fleet_risk.py).
    Everything downstream (planner, spares, dashboard) reads risk only from here."""
    __tablename__ = "risk_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(String(10))
    position: Mapped[str] = mapped_column(String(20))
    sn: Mapped[str | None] = mapped_column(String(30))
    pn: Mapped[str | None] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(20))               # engine_rul | gearbox_anomaly | battery_rul
    as_of: Mapped[datetime] = mapped_column(DateTime)
    age_fh: Mapped[float | None] = mapped_column(Float)
    rul_fh: Mapped[float | None] = mapped_column(Float)
    rul_fh_lo: Mapped[float | None] = mapped_column(Float)
    rul_fh_hi: Mapped[float | None] = mapped_column(Float)
    p_fail_100fh: Mapped[float | None] = mapped_column(Float)
    health_index: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(8))              # ok | watch | alert
    explanation: Mapped[str | None] = mapped_column(Text)        # JSON
    model: Mapped[str] = mapped_column(String(40))
    __table_args__ = (Index("ix_risk_tail_pos_asof", "tail", "position", "as_of"),)


# ------------------------------------------------------------------ usage and health
class Sortie(Base):
    __tablename__ = "sortie"
    ref: Mapped[str] = mapped_column(String(30), primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    takeoff: Mapped[datetime] = mapped_column(DateTime)
    landing: Mapped[datetime] = mapped_column(DateTime)
    fh: Mapped[float] = mapped_column(Float)
    landings: Mapped[int] = mapped_column(Integer)
    aborted: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (Index("ix_sortie_tail_takeoff", "tail", "takeoff"),)


class HumsRecord(Base):
    __tablename__ = "hums_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    hums_unit: Mapped[str] = mapped_column(String(20))
    sortie_ref: Mapped[str | None] = mapped_column(ForeignKey("sortie.ref"))
    flight_start: Mapped[datetime] = mapped_column(DateTime)
    flight_end: Mapped[datetime] = mapped_column(DateTime)
    downloaded: Mapped[datetime] = mapped_column(DateTime)
    source_file: Mapped[str] = mapped_column(String(200))


class HealthIndicator(Base):
    __tablename__ = "health_indicator"
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hums_record_id: Mapped[int] = mapped_column(Integer)
    tail: Mapped[str] = mapped_column(String(10))
    position: Mapped[str] = mapped_column(String(20))
    sn: Mapped[str | None] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(30))
    value: Mapped[float] = mapped_column(Float)
    __table_args__ = (Index("ix_hi_tail_pos_ch_ts", "tail", "position", "channel", "ts"),)


# ------------------------------------------------------------------ maintenance
class Agency(Base):
    __tablename__ = "agency"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    level: Mapped[str] = mapped_column(String(2))                 # O | I | D | S (shop)
    base: Mapped[str] = mapped_column(ForeignKey("location.code"))
    capacity: Mapped[int] = mapped_column(Integer)
    shift_hours: Mapped[float] = mapped_column(Float)


class Defect(Base):
    __tablename__ = "defect"
    defect_no: Mapped[str] = mapped_column(String(30), primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    raised: Mapped[datetime] = mapped_column(DateTime)
    entered: Mapped[datetime | None] = mapped_column(DateTime)
    raised_by: Mapped[str] = mapped_column(String(20))
    ata: Mapped[int | None] = mapped_column(Integer)
    ata_source: Mapped[str | None] = mapped_column(String(12))     # record | inferred
    text: Mapped[str] = mapped_column(Text)
    text_norm: Mapped[str | None] = mapped_column(Text)            # abbreviations expanded, lower case
    category: Mapped[str] = mapped_column(String(12))              # U/S | ADD | SERVICEABLE
    rectification: Mapped[str | None] = mapped_column(Text)
    closed: Mapped[datetime | None] = mapped_column(DateTime)
    man_hours: Mapped[float | None] = mapped_column(Float)
    wo_id: Mapped[str | None] = mapped_column(String(30))
    sortie_ref: Mapped[str | None] = mapped_column(String(30))
    duplicate_of: Mapped[str | None] = mapped_column(String(30))
    __table_args__ = (Index("ix_defect_tail_raised", "tail", "raised"),)


class ComponentChange(Base):
    __tablename__ = "component_change"
    ref: Mapped[str] = mapped_column(String(20), primary_key=True)
    defect_no: Mapped[str | None] = mapped_column(String(30))
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    at: Mapped[datetime] = mapped_column(DateTime)
    position: Mapped[str] = mapped_column(String(20))
    pn: Mapped[str | None] = mapped_column(ForeignKey("part.pn"))
    sn_off: Mapped[str | None] = mapped_column(String(30))
    sn_on: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(12))
    raw_pn_off: Mapped[str | None] = mapped_column(String(40))
    raw_pn_on: Mapped[str | None] = mapped_column(String(40))
    raw_sn_off: Mapped[str | None] = mapped_column(String(40))
    raw_sn_on: Mapped[str | None] = mapped_column(String(40))


class Inspection(Base):
    __tablename__ = "inspection"
    ref: Mapped[str] = mapped_column(String(20), primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    name: Mapped[str] = mapped_column(String(60))
    agency_id: Mapped[str | None] = mapped_column(ForeignKey("agency.id"))
    wo_id: Mapped[str | None] = mapped_column(String(30))
    started: Mapped[datetime] = mapped_column(DateTime)
    completed: Mapped[datetime | None] = mapped_column(DateTime)
    airframe_hours: Mapped[float | None] = mapped_column(Float)


class InspectionStatus(Base):
    """When each scheduled inspection was last done, per aircraft (records' inspection status)."""
    __tablename__ = "inspection_status"
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    last_done: Mapped[datetime | None] = mapped_column(DateTime)
    last_done_fh: Mapped[float | None] = mapped_column(Float)
    airframe_fh_at_export: Mapped[float | None] = mapped_column(Float)


class WorkOrder(Base):
    __tablename__ = "work_order"
    wo_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    agency_id: Mapped[str] = mapped_column(ForeignKey("agency.id"))
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    wo_type: Mapped[str] = mapped_column(String(12))
    opened: Mapped[datetime] = mapped_column(DateTime)
    started: Mapped[datetime | None] = mapped_column(DateTime)
    closed: Mapped[datetime | None] = mapped_column(DateTime)
    inspection_ref: Mapped[str | None] = mapped_column(String(20))
    labour_hours: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10))


class WorkOrderDefect(Base):
    __tablename__ = "work_order_defect"
    wo_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    defect_no: Mapped[str] = mapped_column(String(30), primary_key=True)
    link: Mapped[str] = mapped_column(String(10))                 # explicit | record | inferred


# ------------------------------------------------------------------ supply
class StockSnapshot(Base):
    __tablename__ = "stock_snapshot"
    date: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    location: Mapped[str] = mapped_column(String(10), primary_key=True)
    pn: Mapped[str] = mapped_column(String(30), primary_key=True)
    serviceable: Mapped[int] = mapped_column(Integer)
    dues_out: Mapped[int] = mapped_column(Integer)
    in_repair: Mapped[int] = mapped_column(Integer)
    on_order: Mapped[int] = mapped_column(Integer)


class StockTxn(Base):
    __tablename__ = "stock_txn"
    txn_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime)
    location: Mapped[str] = mapped_column(String(10))
    pn: Mapped[str | None] = mapped_column(String(30))
    sn: Mapped[str | None] = mapped_column(String(30))
    txn_type: Mapped[str] = mapped_column(String(14))
    demand_no: Mapped[str | None] = mapped_column(String(20))
    wo_id: Mapped[str | None] = mapped_column(String(30))
    tail: Mapped[str | None] = mapped_column(String(10))
    tail_source: Mapped[str | None] = mapped_column(String(10))   # record | via_wo
    priority: Mapped[str | None] = mapped_column(String(6))
    __table_args__ = (Index("ix_txn_demand", "demand_no"), Index("ix_txn_tail", "tail"))


class RepairReturn(Base):
    __tablename__ = "repair_return"
    job_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    pn: Mapped[str | None] = mapped_column(String(30))
    sn: Mapped[str] = mapped_column(String(30))
    received: Mapped[datetime] = mapped_column(DateTime)
    completed: Mapped[datetime] = mapped_column(DateTime)
    finding: Mapped[str] = mapped_column(String(12))
    cost_inr: Mapped[int | None] = mapped_column(Integer)


class ReliabilityPart(Base):
    """Per part number: removals, MTBUR / MTBF, NFF rate, Weibull fit (airpower/reliability/analyse.py)."""
    __tablename__ = "reliability_part"
    pn: Mapped[str] = mapped_column(String(30), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(120))
    ata: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(1))
    installed: Mapped[int] = mapped_column(Integer)
    component_hours: Mapped[float] = mapped_column(Float)
    removals: Mapped[int] = mapped_column(Integer)
    unscheduled: Mapped[int] = mapped_column(Integer)
    confirmed_failures: Mapped[int] = mapped_column(Integer)
    findings: Mapped[int] = mapped_column(Integer)
    nff: Mapped[int] = mapped_column(Integer)
    mtbur_h: Mapped[float | None] = mapped_column(Float)
    mtbf_h: Mapped[float | None] = mapped_column(Float)
    nff_rate: Mapped[float | None] = mapped_column(Float)
    weibull_beta: Mapped[float | None] = mapped_column(Float)
    weibull_eta_h: Mapped[float | None] = mapped_column(Float)
    weibull_beta_lo: Mapped[float | None] = mapped_column(Float)
    weibull_beta_hi: Mapped[float | None] = mapped_column(Float)
    weibull_eta_lo: Mapped[float | None] = mapped_column(Float)
    weibull_eta_hi: Mapped[float | None] = mapped_column(Float)
    weibull_n: Mapped[int | None] = mapped_column(Integer)
    weibull_failures: Mapped[int | None] = mapped_column(Integer)
    weibull_beta_landings: Mapped[float | None] = mapped_column(Float)
    weibull_eta_landings: Mapped[float | None] = mapped_column(Float)
    unit_cost_inr: Mapped[int | None] = mapped_column(Integer)


class RogueCandidate(Base):
    __tablename__ = "rogue_candidate"
    pn: Mapped[str] = mapped_column(String(30), primary_key=True)
    sn: Mapped[str] = mapped_column(String(30), primary_key=True)
    removals: Mapped[int] = mapped_column(Integer)
    component_hours: Mapped[float] = mapped_column(Float)
    expected: Mapped[float] = mapped_column(Float)
    p_value: Mapped[float] = mapped_column(Float)
    nff: Mapped[int] = mapped_column(Integer)
    tails: Mapped[str] = mapped_column(String(200))
    last_removal: Mapped[datetime | None] = mapped_column(DateTime)


class RepeatDefect(Base):
    __tablename__ = "repeat_defect"
    chain_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(String(10))
    ata: Mapped[int | None] = mapped_column(Integer)
    occurrences: Mapped[int] = mapped_column(Integer)
    first: Mapped[datetime] = mapped_column(DateTime)
    last: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str | None] = mapped_column(Text)
    defects: Mapped[str] = mapped_column(Text)


class SparesTarget(Base):
    """METRIC stock per part at today's inventory value vs what is held (airpower/spares/metric.py)."""
    __tablename__ = "spares_target"
    pn: Mapped[str] = mapped_column(String(30), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(1))
    cost_lakh: Mapped[float] = mapped_column(Float)
    demand_per_day: Mapped[float] = mapped_column(Float)
    pipeline_days: Mapped[float] = mapped_column(Float)
    pipeline_mean: Mapped[float] = mapped_column(Float)
    baseline_stock: Mapped[int] = mapped_column(Integer)
    metric_stock: Mapped[int] = mapped_column(Integer)
    held_now: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)


class AogRisk(Base):
    """Fitted units likely to need a replacement within the part's lead time while no spare is at hand."""
    __tablename__ = "aog_risk"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(String(10))
    position: Mapped[str] = mapped_column(String(20))
    pn: Mapped[str] = mapped_column(String(30))
    sn: Mapped[str | None] = mapped_column(String(30))
    p_within_lead: Mapped[float] = mapped_column(Float)
    lead_days: Mapped[float] = mapped_column(Float)
    basis: Mapped[str] = mapped_column(String(20))
    base_stock: Mapped[int] = mapped_column(Integer)
    fleet_stock: Mapped[int] = mapped_column(Integer)
    expected_demand_in_lead: Mapped[float] = mapped_column(Float)


class PlanJob(Base):
    """Maintenance plan (airpower/plan/planner.py): one row per planned job."""
    __tablename__ = "plan_job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan: Mapped[str] = mapped_column(String(12))                 # optimised | due_date
    tail: Mapped[str] = mapped_column(String(10))
    unit: Mapped[str] = mapped_column(String(20))
    job: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(12))                 # inspection | condition | blocked
    agency: Mapped[str | None] = mapped_column(String(20))
    start_day: Mapped[int | None] = mapped_column(Integer)
    end_day: Mapped[int | None] = mapped_column(Integer)
    due_day: Mapped[int | None] = mapped_column(Integer)
    bundled_into: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------------------ operational tables
# Written by the running system (users, decisions, audit), not derived from the sources: a data load
# or a rebuild never drops them (create_all(reset=True) leaves them alone; airpower/model/migrate.py
# versions them).
OPERATIONAL = {"info": {"operational": True}}


class AppUser(Base):
    """A person who can sign in. Roles: commander, engineering_officer, logistics_officer, analyst, admin."""
    __tablename__ = "app_user"
    __table_args__ = OPERATIONAL
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(24))
    password_hash: Mapped[str] = mapped_column(String(200))        # scrypt$n$r$p$salt$hash
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created: Mapped[datetime] = mapped_column(DateTime)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    """Append-only, hash-chained record of security-relevant actions (server/audit.py). Each row's hash
    covers the previous row's hash, so removing or editing a row breaks the chain from there on."""
    __tablename__ = "audit_log"
    __table_args__ = OPERATIONAL
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(60))
    role: Mapped[str | None] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)               # JSON
    ip: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(10))               # ok | denied | failed
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class SchemaInfo(Base):
    __tablename__ = "schema_info"
    __table_args__ = OPERATIONAL
    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[str] = mapped_column(String(60))


class PlanDecision(Base):
    """An engineering officer's decision on a planned job: accepted, or moved to another day."""
    __tablename__ = "plan_decision"
    __table_args__ = OPERATIONAL
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(String(10))
    job: Mapped[str] = mapped_column(String(80))
    decision: Mapped[str] = mapped_column(String(12))             # accept | override
    start_day: Mapped[int | None] = mapped_column(Integer)        # day of the plan the decision was made on
    start_date: Mapped[datetime | None] = mapped_column(DateTime) # the date it fixes for the next plans
    note: Mapped[str | None] = mapped_column(Text)
    user: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime)


class Meta(Base):
    """Small key -> JSON store: integration report, data window, load provenance."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# ------------------------------------------------------------------ derived
class AircraftStatus(Base):
    """Serviceability intervals derived from the integrated records (airpower/twin/status.py)."""
    __tablename__ = "aircraft_status"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tail: Mapped[str] = mapped_column(ForeignKey("aircraft.tail"))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12))               # SERVICEABLE | NMCM | NMCS | SCHEDULED
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_status_tail_start", "tail", "start"),)


def engine(url):
    eng = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return eng


def derived_tables():
    return [t for t in Base.metadata.sorted_tables if not t.info.get("operational")]


def create_all(eng, reset=False):
    """Create the schema. reset=True drops and recreates the derived tables only (integration output);
    operational tables (users, audit log, plan decisions) are kept."""
    if reset:
        Base.metadata.drop_all(eng, tables=derived_tables())
    Base.metadata.create_all(eng)
    if eng.dialect.name == "postgresql":
        with eng.begin() as c:
            has = c.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first()
            if has:
                c.execute(text("SELECT create_hypertable('health_indicator', 'ts', if_not_exists => TRUE, "
                               "migrate_data => TRUE)"))
