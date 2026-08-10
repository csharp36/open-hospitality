from datetime import date, datetime, time
from decimal import Decimal
from typing import cast

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    column,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from usali.crypto import EncryptedBytes, EncryptedString


class Base(DeclarativeBase):
    pass


class OrgScoped:
    """The tenant key (Pillar L decision 1): every tenant-owned table carries
    `org_id` — denormalized, NOT NULL, indexed — the one column the L2 walls
    (the `do_orm_execute` criteria hook and per-table RLS) both filter on.
    Mixed into every model except `Organization` itself (keyed by org_id) and
    the two org-independent reference tables (`usali_schedule`,
    `usali_mapping_dictionary` — the judgment is recorded in `l1a0orgid`).

    The server default `1` is deliberate, and NOT the wage_jurisdiction case
    (e1f0nodefault): L1 ships schema only, no session carries an org context
    until L2/L3, so every existing write path must keep landing rows in the
    single founding tenant (org 1 by construction — A2.1 seeds exactly one
    org) unchanged. While one org exists the default cannot encode a wrong
    decision; the moment it could, L2's RLS WITH CHECK refuses any write
    whose org_id disagrees with the session org. The default is the bridge,
    RLS is the wall.

    L6a KEEPS the default rather than removing it (removing it would mean
    auditing every INSERT path that omits org_id — larger than L6a). Instead
    the write wall (`tenancy._stamp_wall`, a `before_flush` hook) now stamps
    org_id from the session context FIRST on every INSTRUMENTED session, so
    the default only ever applies where NO wall exists: un-instrumented
    owner/superuser paths — alembic data migrations, founding-bound seeds,
    Core `insert()` statements — which act within the founding org. On an
    instrumented org≠1 session the stamp BEATS the default, so the row lands
    in the session's org (pinned in `test_l3_org_resolution`), and an
    explicit cross-org org_id refuses loudly (`OrgContextMismatch`) before
    the DB wall. `Organization` is exempt (org-scoped by its own PK, not
    this mixin): its org_id is the identity provisioning creates.
    """

    # Provided by the Base subclass this is mixed into (for the typechecker;
    # declarative skips dunder annotations, so this maps nothing).
    __tablename__: str

    # declared_attr, not a plain mapped_column: the FK carries the SAME name
    # the migration gave it (fk_<table>_org), so the ORM's schema and the
    # migrated schema never disagree about what a constraint is called.
    @declared_attr
    def org_id(cls) -> Mapped[int]:
        table = getattr(cls, "__tablename__", None)
        if table is None:
            # The L2 application wall's `with_loader_criteria(OrgScoped,
            # lambda cls: cls.org_id == org)` (the documented SQLAlchemy
            # mixin recipe) evaluates its lambda ONCE against the BARE
            # mixin for structural analysis — SQLAlchemy's _WrapUserEntity
            # invokes this declared_attr with cls=OrgScoped, which has no
            # __tablename__. Hand that analysis a plain column expression
            # of the same shape; at compile time the lambda is re-invoked
            # per CONCRETE entity and takes the real mapped path below.
            return cast("Mapped[int]", column("org_id", Integer))
        return mapped_column(
            ForeignKey("organization.org_id", name=f"fk_{table}_org"),
            index=True,
            server_default=text("1"),
        )


class IngestBatch(OrgScoped, Base):
    __tablename__ = "ingest_batch"

    batch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pms_source: Mapped[str] = mapped_column(String(20))
    report_type: Mapped[str] = mapped_column(String(40))
    source_file: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="started")
    # Counts the report's PRIMARY staged rows (financial/statistic/segment). Rider rows
    # staged under the same batch (e.g. trial-balance ledger block) are not included.
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PmsDailyFinancialStage(OrgScoped, Base):
    __tablename__ = "pms_daily_financial_stage"
    # org_id discriminator (l8a0tenancyfix / L8-F4): without it the unique is
    # GLOBAL, and stage_records' on_conflict_do_nothing on it SILENTLY drops a
    # second org's row whose content hashes identically.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "pms_source", "business_date", "row_hash",
            name="uq_pms_daily_financial_stage_org_row",
        ),
    )

    stage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    report_type: Mapped[str] = mapped_column(String(40))
    business_date: Mapped[date] = mapped_column(Date)
    pms_trx_code: Mapped[str] = mapped_column(String(50))
    pms_trx_desc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_amount: Mapped[float] = mapped_column(Numeric(15, 4))
    room_count: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_file: Mapped[str] = mapped_column(String(255))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    row_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsaliSchedule(Base):
    __tablename__ = "usali_schedule"

    usali_edition: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class UsaliMappingDictionary(Base):
    __tablename__ = "usali_mapping_dictionary"
    __table_args__ = (
        UniqueConstraint("pms_source", "pms_trx_code", "usali_edition"),
    )

    mapping_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pms_source: Mapped[str] = mapped_column(String(20))
    pms_trx_code: Mapped[str] = mapped_column(String(50))
    usali_edition: Mapped[int] = mapped_column(Integer)
    usali_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usali_major_category: Mapped[str] = mapped_column(String(100))
    usali_sub_category: Mapped[str] = mapped_column(String(100))
    usali_line_item: Mapped[str] = mapped_column(String(100))
    gl_account_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    review_status: Mapped[str] = mapped_column(String(20), default="unreviewed")
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class UsaliFinancialFact(OrgScoped, Base):
    __tablename__ = "usali_financial_fact"
    __table_args__ = (UniqueConstraint("stage_id"),)

    fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    business_date: Mapped[date] = mapped_column(Date)
    usali_edition: Mapped[int] = mapped_column(Integer)
    usali_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usali_major_category: Mapped[str] = mapped_column(String(100))
    usali_sub_category: Mapped[str] = mapped_column(String(100))
    usali_line_item: Mapped[str] = mapped_column(String(100))
    amount: Mapped[float] = mapped_column(Numeric(15, 4))
    room_count: Mapped[int] = mapped_column(Integer, default=0)
    gl_account_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("pms_daily_financial_stage.stage_id"))


class MappingException(OrgScoped, Base):
    __tablename__ = "mapping_exception"
    __table_args__ = (UniqueConstraint("stage_id"),)

    exception_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pms_source: Mapped[str] = mapped_column(String(20))
    pms_trx_code: Mapped[str] = mapped_column(String(50))
    pms_trx_desc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("pms_daily_financial_stage.stage_id"))
    raw_amount: Mapped[float] = mapped_column(Numeric(15, 4))
    business_date: Mapped[date] = mapped_column(Date)
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PmsDailyStatisticStage(OrgScoped, Base):
    __tablename__ = "pms_daily_statistic_stage"
    # org_id discriminator (l8a0tenancyfix / L8-F4): see PmsDailyFinancialStage.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "pms_source", "business_date", "row_hash",
            name="uq_pms_daily_statistic_stage_org_row",
        ),
    )

    stat_stage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    report_type: Mapped[str] = mapped_column(String(40))
    business_date: Mapped[date] = mapped_column(Date)
    metric_label: Mapped[str] = mapped_column(String(200))
    period_label: Mapped[str] = mapped_column(String(20))
    is_prior_year: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[float] = mapped_column(Numeric(18, 4))
    source_file: Mapped[str] = mapped_column(String(255))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    row_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsaliStatisticFact(OrgScoped, Base):
    __tablename__ = "usali_statistic_fact"
    __table_args__ = (UniqueConstraint("stat_stage_id"),)

    stat_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    business_date: Mapped[date] = mapped_column(Date)
    metric_code: Mapped[str] = mapped_column(String(50))
    period: Mapped[str] = mapped_column(String(10))
    is_prior_year: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[float] = mapped_column(Numeric(18, 4))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    stat_stage_id: Mapped[int] = mapped_column(
        ForeignKey("pms_daily_statistic_stage.stat_stage_id")
    )


class PmsLedgerBalanceStage(OrgScoped, Base):
    __tablename__ = "pms_ledger_balance_stage"
    # org_id discriminator (l8a0tenancyfix / L8-F4): see PmsDailyFinancialStage.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "pms_source", "business_date", "row_hash",
            name="uq_pms_ledger_balance_stage_org_row",
        ),
    )

    ledger_stage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    report_type: Mapped[str] = mapped_column(String(40))
    business_date: Mapped[date] = mapped_column(Date)
    ledger_label: Mapped[str] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Numeric(15, 4))
    kind: Mapped[str] = mapped_column(String(20))  # "balance" | "activity"
    source_file: Mapped[str] = mapped_column(String(255))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    row_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsaliLedgerBalanceFact(OrgScoped, Base):
    __tablename__ = "usali_ledger_balance_fact"
    __table_args__ = (
        UniqueConstraint("ledger_stage_id"),
        # One fact per ledger per property per day. A corrected re-export of the same
        # business date re-stages under fresh row hashes (different file_hash); without
        # this constraint promote would add a second GUEST_LEDGER fact and the A/R
        # report would double-count. The conflict surfaces as an IntegrityError through
        # process_file's quarantine path — loud, never silent.
        UniqueConstraint("property_id", "pms_source", "business_date", "ledger_code"),
    )

    ledger_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    business_date: Mapped[date] = mapped_column(Date)
    ledger_code: Mapped[str] = mapped_column(String(50))
    ledger_name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Numeric(15, 4))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    ledger_stage_id: Mapped[int] = mapped_column(
        ForeignKey("pms_ledger_balance_stage.ledger_stage_id")
    )


class QboPushLedger(OrgScoped, Base):
    """One row per (property, business date) QBO journal-entry push outcome.

    `request_hash` fingerprints the JE content that was (or would be) posted;
    it doubles as the Intuit `requestid` (truncated to 50 chars) so retries of
    the same content replay instead of double-posting. Status lifecycle:
    `pushed` (JE exists in QBO), `failed` (POST attempt rejected — retryable),
    `stale` (facts changed AFTER a successful push; the posted JE no longer
    matches the data and needs manual correction — re-pushing is refused).
    """

    __tablename__ = "qbo_push_ledger"
    __table_args__ = (UniqueConstraint("property_id", "business_date"),)

    push_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    business_date: Mapped[date] = mapped_column(Date)
    request_hash: Mapped[str] = mapped_column(String(64))
    qbo_je_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # "pushed" | "failed" | "stale"
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PmsDailySegmentStage(OrgScoped, Base):
    __tablename__ = "pms_daily_segment_stage"
    # org_id discriminator (l8a0tenancyfix / L8-F4): see PmsDailyFinancialStage.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "pms_source", "business_date", "row_hash",
            name="uq_pms_daily_segment_stage_org_row",
        ),
    )

    segment_stage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    report_type: Mapped[str] = mapped_column(String(40))
    business_date: Mapped[date] = mapped_column(Date)
    segment_code: Mapped[str] = mapped_column(String(50))
    segment_desc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    measure: Mapped[str] = mapped_column(String(30))
    period_label: Mapped[str] = mapped_column(String(20))
    value: Mapped[float] = mapped_column(Numeric(18, 4))
    source_file: Mapped[str] = mapped_column(String(255))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))
    row_hash: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsaliSegmentFact(OrgScoped, Base):
    __tablename__ = "usali_segment_fact"
    __table_args__ = (
        UniqueConstraint("pms_source", "property_id", "business_date", "usali_segment", "period"),
    )

    segment_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    pms_source: Mapped[str] = mapped_column(String(20))
    business_date: Mapped[date] = mapped_column(Date)
    usali_segment: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(10))
    rooms: Mapped[float] = mapped_column(Numeric(18, 4))
    room_revenue: Mapped[float] = mapped_column(Numeric(15, 4))
    ingest_batch_id: Mapped[int] = mapped_column(ForeignKey("ingest_batch.batch_id"))


# GUARD: Organization deliberately does NOT inherit OrgScoped — its org_id
# is the PRIMARY KEY / tenant identity, not a reference to some other tenant.
# The write wall's exemption (tenancy._stamp_wall) depends on this: do NOT
# make it inherit OrgScoped, or provisioning's founding-bound insert of the
# new org≠1 row would trip OrgContextMismatch (its org_id ≠ the session org).
class Organization(Base):
    """The hotel group. The org is the top of the workforce hierarchy; a
    property belongs to exactly one org. A2.1 seeds a single default org."""

    __tablename__ = "organization"
    __table_args__ = (
        UniqueConstraint("kc_org_alias", name="uq_organization_kc_org_alias"),
    )

    org_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # The join key to the Keycloak organization alias (L1, for L3): alias ->
    # org_id resolves HERE — the DB is the source of truth; nothing numeric
    # from a token is ever an org_id. NULLABLE (no alias declared yet is the
    # honest state), UNIQUE (two orgs answering to one alias would make
    # resolution a guess, which L3 refuses), bounded (an identifier, not a
    # payload).
    kc_org_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrgSettings(OrgScoped, Base):
    """Per-org integration config (Pillar L decision 5). One row per org,
    org-scoped by its OWN primary key — `org_id` is BOTH the PK and the FK
    to `organization`, the same "org-scoped by its key" shape as
    `Organization`. Only genuinely per-TENANT config lives here; process-wide
    deployment config (DB URL, provider base URLs, encryption key) stays in
    `Settings`.

    Subclasses `OrgScoped` so BOTH L2 walls confine it automatically — the
    ORM criteria hook (`with_loader_criteria(OrgScoped, ...)`) and the RLS
    policy (`l5a0orgsettings`) — but overrides `org_id` to be the primary
    key (the mixin's default is a plain indexed FK). A read under the active
    org's session therefore returns exactly that org's row, or none.

    `crm_provider`: '' | 'delphi' | 'tripleseat' — the demand feed this org
    pulls from, EMPTY meaning the feature is OFF (the J4 loud-refuse posture,
    now PER-ORG). A DB CHECK enforces the closed set (refuse-unknown). The
    env `USALI_CRM_PROVIDER` seeds ORG 1's row only (a bridge default,
    `ensure_default_org`); at runtime the value is read from THIS row, never
    from env — a env fallback for org != 1 is the mutant L5 kills."""

    __tablename__ = "org_settings"
    # The CHECK is the SCHEMA MIRROR of usali.crm_feed.CRM_PROVIDERS (+ the
    # '' OFF sentinel) — kept literal on purpose so the DB refuses an unknown
    # provider independently of the app import. If CRM_PROVIDERS changes, this
    # and the l5a0orgsettings migration CHECK change with it.
    __table_args__ = (
        CheckConstraint(
            "crm_provider IN ('', 'delphi', 'tripleseat')",
            name="ck_org_settings_crm_provider",
        ),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.org_id", name="fk_org_settings_org"),
        primary_key=True,
    )
    crm_provider: Mapped[str] = mapped_column(String(20), server_default="")


class Property(OrgScoped, Base):
    """The DB system of record for a property. `property_id` is the SAME short
    code ("HISJ"/"SSSJ") already denormalized across every fact/stage table, so
    existing rows remain referentially meaningful without a data rewrite."""

    __tablename__ = "property"
    # (org_id, property_id) unique: the target of the composite FKs from
    # role_assignment / employee_assignment / timecard_adjustment (l1a0orgid).
    # org_id itself comes from OrgScoped since L1.
    __table_args__ = (
        UniqueConstraint("org_id", "property_id", name="uq_property_org"),
    )

    property_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    pms_source: Mapped[str] = mapped_column(String(20))
    # IANA timezone — punches are attributed to a business date in the PROPERTY'S
    # local time (see usali.attendance). Defaults to the pilot's zone.
    timezone: Mapped[str] = mapped_column(
        String(50), server_default="America/Los_Angeles"
    )
    # Which jurisdiction's WAGE rules price this property's hours. Distinct from
    # `timezone`: the same zone spans states with very different overtime law
    # (America/Los_Angeles covers California, Nevada, and Washington, whose daily
    # overtime rules differ or are absent). Resolved through
    # usali.overtime_rules, which refuses unknown values rather than defaulting.
    # NO server default: creating a property must be an explicit decision about
    # which wage rules price its hours. A lingering 'US-CA' default silently gave
    # a new out-of-state property California daily overtime and double time --
    # converting overtime_rules' deliberate refusal into a silent miscosting,
    # since that refusal fires on an UNRECOGNIZED jurisdiction, never an unset one.
    # NO DEFAULT, in the model OR the schema, and NULLABLE on purpose.
    #
    # `e1f0nodefault` dropped the server default so a new property could not
    # silently inherit California daily overtime and double time. A Python-side
    # `default="US-CA"` defeated that entirely -- the ORM is the only path that
    # creates properties, so a Texas property still came out Californian. Both
    # guarding tests asserted on `information_schema.column_default` and passed
    # while the behaviour they described was false.
    #
    # Removing the default then exposed the real gap: PMS ingestion CREATES
    # properties, and a nightly revenue file knows nothing about wage law. So
    # NULL is the honest state for "nobody has said yet" -- a property can exist
    # for revenue reporting without one. `rules_for(None)` refuses, which turns
    # an unstated jurisdiction into a blocked pay run naming the property rather
    # than into silently Californian overtime.
    wage_jurisdiction: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The CRM-side identity of this property (Pillar J, plan decision 6):
    # a Delphi hotel ref or Tripleseat location id, declared in
    # `mapping/properties.yaml` and seeded here — the wage_jurisdiction
    # pattern verbatim, including "a re-seed never blanks an operator-set
    # value". NULL is the honest state for "no CRM speaks for this
    # property"; the pull path refuses it BY NAME rather than guessing.
    crm_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PropertyDetectionAlias(OrgScoped, Base):
    """Carries what the `properties.yaml` rows held: the (property, pms_source,
    match_phrase) triple `detect.py` resolves a report's property from. Replaces
    the YAML file as the runtime detection registry."""

    __tablename__ = "property_detection_alias"
    __table_args__ = (
        UniqueConstraint("property_id", "pms_source", "match_phrase"),
    )

    alias_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    pms_source: Mapped[str] = mapped_column(String(20))
    match_phrase: Mapped[str] = mapped_column(String(200))


class Department(OrgScoped, Base):
    __tablename__ = "department"
    __table_args__ = (
        UniqueConstraint("property_id", "name"),
        # Composite-FK target: the money facts attribute dollars on this axis.
        UniqueConstraint("org_id", "department_id", name="uq_department_org"),
        # ...and a composite FK in its own right. L8 left this one single-column
        # on the reasoning that its unique is (property_id, name), so a squat
        # denies one named slot rather than a whole capability. That weighed the
        # DoS and nothing else, and it predates POST /api/departments — which
        # takes property_id from the CALLER. A single-column FK validates that
        # id with owner privileges, i.e. past RLS, so it would happily anchor an
        # org-1 department to an org-2 property. The composite makes the
        # cross-org reference unrepresentable; the endpoint's own scope check is
        # the first door, this is the wall behind it. (l9a0deptfk)
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_department_property_org",
        ),
    )

    department_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    # The labor-cost seam to USALI Schedule 14/15 (populated when Pillar B
    # produces labor cost). Nullable now — not every department maps yet.
    usali_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usali_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Position(OrgScoped, Base):
    __tablename__ = "position"
    # Composite-FK target (employee_assignment): flsa_exempt prices overtime.
    __table_args__ = (
        UniqueConstraint("org_id", "position_id", name="uq_position_org"),
    )

    position_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("department.department_id"))
    title: Mapped[str] = mapped_column(String(100))
    flsa_exempt: Mapped[bool] = mapped_column(Boolean)  # FLSA exempt vs non-exempt
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# The closed set of pay types, enforced at every door (onboarding API, roster
# seed) rather than written twice — the E1 ramp lesson: every copy of a
# predicate is an independent chance to get it wrong.
EXCLUDE_FROM_PAYROLL = "exclude_from_payroll"
PAY_TYPES = frozenset({"hourly", "salary", EXCLUDE_FROM_PAYROLL})

# Closed vocabularies for property config (property_config_api + demo seed).
# The DB CHECKs below are the literal SCHEMA MIRROR of these sets — kept literal
# on purpose so the database refuses an unknown value independently of the app
# import, the org_settings.crm_provider idiom.
OOO_REASON_CODES = frozenset({"maintenance", "renovation", "damage", "deep_clean", "other"})
CALENDAR_TYPES = frozenset({"calendar_month", "445"})


class Employee(OrgScoped, Base):
    __tablename__ = "employee"
    __table_args__ = (
        UniqueConstraint("keycloak_subject"),
        # The enum answers the STATUS question, the date answers the DATE
        # question (schedule_api compares against it), and this keeps the two
        # from ever disagreeing about whether someone is terminated. Mirrored
        # in e3a0class; drift between model and migration is a known blind
        # spot (see BACKLOG: census UniqueConstraint), mirrored anyway.
        CheckConstraint(
            "(employment_status = 'terminated') = (termination_date IS NOT NULL)",
            name="ck_employee_terminated_iff_dated",
        ),
        # The composite-FK anchor of the whole wage/PII spine (l1a0orgid):
        # every table that pays, records hours for, or holds PII about an
        # employee references (org_id, employee_id), so a cross-org reference
        # is refused by the SCHEMA — FK checks bypass RLS.
        UniqueConstraint("org_id", "employee_id", name="uq_employee_org"),
        # The manager chain stays inside the org (PII graph).
        ForeignKeyConstraint(
            ["org_id", "manager_employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_employee_manager_org",
        ),
    )

    employee_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 1:1 to a Keycloak subject id. NULL until A2.3 onboarding provisions the
    # Keycloak user; the app record is the system of record either way.
    keycloak_subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # property_id / department_id / position_id REMOVED in E1 Task 9 — they live
    # on employee_assignment now, effective-dated and many-per-employee. See
    # usali.assignments for every question they used to answer.
    full_name: Mapped[str] = mapped_column(String(200))
    # "hourly" | "salary" | "exclude_from_payroll". The third is a PAY TYPE and
    # not a boolean beside the others: hourly+excluded would express a person
    # who is simultaneously priced and unpriceable. Excluded staff produce no
    # UsaliLaborFact rows at all and are filtered from the pay-run population
    # (E3 decision 4) — four El Sendero staff carry it today.
    pay_type: Mapped[str] = mapped_column(String(20))
    # "full_time" | "part_time" | NULL (unknown — not backfillable from the
    # incumbent). No consumer gates on it yet; E4's PTO accrual will.
    full_part_time: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Compliance metadata, not documents. Storage of the artifacts themselves
    # is blocked on the I-9 retention question (BACKLOG).
    i9_submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    w4_submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # "active" | "inactive" | "leave" | "terminated". `terminated` is entered
    # ONLY through onboarding.terminate_employee — it is the one path that
    # closes assignments and refuses to strand a filed day. Python default,
    # no server default (e1f0: a new row's status must be this code's
    # decision, not the schema's).
    employment_status: Mapped[str] = mapped_column(String(10), default="active")
    # Stored, set by people, NOT derived from the I-9/W-4 dates — the real
    # definition of "complete" grows (E5 deposit accounts, documents later)
    # and a derived flag would silently flip when it does. False blocks
    # pay-run inclusion with a blocker naming the employee.
    payroll_data_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    manager_employee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # PII placeholder — encrypted at rest, Payroll-Admin-gated. Pillar C adds
    # real SSN/bank fields using this same EncryptedString pattern.
    compensation_note: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    # GM-maintained scheduling aid ("can't work Tuesdays"). Operational, not
    # PII; scheduler-editable; shown in the builder. Never money, never medical.
    availability_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmployeeAssignment(OrgScoped, Base):
    """One employee's effective-dated binding to a (property, department, position).

    An employee may hold several concurrently — at the pilot, 21 of 28 active
    staff work at both hotels and three hold two DIFFERENT jobs (front desk +
    night audit, housekeeping + laundry, breakfast + housekeeping). The
    superseded `Employee.property_id`/`department_id`/`position_id` were single
    FKs, so every hour those people worked was attributed to whichever one
    property their employee row happened to name.

    Exactly one concurrent assignment carries `is_primary`. That property's PAY
    RUN issues the employee's single paycheck — the population predicate keys on
    it precisely so one timecard cannot be picked up by two property pay runs
    and paid twice. Cost is a separate concern: it is allocated to EVERY property
    the person actually worked at, in proportion to hours recorded there. One
    paycheck, two P&Ls.

    Dating is [effective_from, effective_to) — inclusive start, EXCLUSIVE end.
    Resolve it through `usali.assignments`, never with ad-hoc date comparisons at
    call sites.

    NOTE: this does NOT replace `RoleAssignment`. That is the RBAC scope record
    keyed on a Keycloak subject; this is the employment record keyed on an
    employee. A housekeeper has an assignment and no role assignment.
    """

    __tablename__ = "employee_assignment"
    __table_args__ = (
        UniqueConstraint("employee_id", "property_id", "effective_from"),
        # Composite-FK target for assignment_rate: a rate prices a placement.
        UniqueConstraint("org_id", "assignment_id", name="uq_employee_assignment_org"),
        # All composite (l1a0orgid): the employment record decides which
        # property's run pays and what FLSA class prices the hours — every
        # reference must stay inside the org.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_employee_assignment_employee_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_employee_assignment_property_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_employee_assignment_department_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "position_id"],
            ["position.org_id", "position.position_id"],
            name="fk_employee_assignment_position_org",
        ),
    )

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    property_id: Mapped[str] = mapped_column(String(50))
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The paycheck-issuing assignment. Exactly one per employee per date range.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20))  # "active" | "inactive"
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RoleAssignment(OrgScoped, Base):
    """The DB source of truth for a subject's role + scope — since L4
    (Pillar L decision 4) THE role authority for every org-gated surface:
    realm token roles are realm-global and gate only the coarse
    operator/employee door, while these rows, read under the ACTIVE org's
    RLS, decide what the caller may actually do there. Scope nesting:
    `property_id` NULL = ORG-WIDE grant (l4a0orggrant), `department_id`
    NULL = property-wide (e.g. GM). The unique is NULLS NOT DISTINCT —
    one grant, one row: revocation is a row delete, and a duplicate
    grant would survive its own revocation."""

    __tablename__ = "role_assignment"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "keycloak_subject", "role", "property_id",
            "department_id", name="uq_role_assignment_grant",
            postgresql_nulls_not_distinct=True,
        ),
        # Composite (l1a0orgid): a grant scoped to another org's property or
        # department would be RBAC authority across the wall — decision 4
        # makes these grants THE role authority, so the schema refuses.
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_role_assignment_property_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_role_assignment_department_org",
        ),
    )

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keycloak_subject: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(30))
    # NULL = org-wide grant. The L1 composite FK onto property is MATCH
    # SIMPLE, so a NULL property reference is simply not checked — the
    # wanted semantics: an org-wide grant names no property. org_id stays
    # enforced by the mixin's FK onto organization.
    property_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuditEvent(OrgScoped, Base):
    """Who did what to which workforce/PII record, when. Written on every PII
    read (Payroll-Admin compensation gate) — the HIPAA-grade access trail."""

    __tablename__ = "audit_event"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_subject: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(50))
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KioskDevice(OrgScoped, Base):
    """An enrolled time-clock iPad. The device token is a BEARER credential —
    stored hashed (SHA-256 of a high-entropy random token), surfaced in plaintext
    exactly once at enrollment, property-scoped, and revocable."""

    __tablename__ = "kiosk_device"
    # Composite-FK target (punch): hour attribution flows through the device.
    __table_args__ = (
        UniqueConstraint("org_id", "device_id", name="uq_kiosk_device_org"),
    )

    device_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enrolled_by: Mapped[str] = mapped_column(String(64))  # actor keycloak subject
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Punch(OrgScoped, Base):
    """One immutable clock event. Corrections are NEVER edits — B2 adds
    `timecard_adjustment` rows (and `punch.timecard_id`) for that. `photo_key`
    points into the PhotoStore; the image itself never lives in the DB."""

    __tablename__ = "punch"
    # Mirrored in f1a0face; drift between model and migration is the known
    # blind spot, mirrored anyway (the e3a0class posture).
    __table_args__ = (
        CheckConstraint(
            "match_state IN ('verified', 'unverified', 'no_template')",
            name="ck_punch_match_state",
        ),
        CheckConstraint(
            "match_state IS DISTINCT FROM 'verified' OR match_score IS NOT NULL",
            name="ck_punch_verified_has_score",
        ),
        CheckConstraint(
            "match_score IS NULL OR match_state IS NOT NULL",
            name="ck_punch_score_has_state",
        ),
        CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)",
            name="ck_punch_score_range",
        ),
        # The timecard chain is composite end to end (l1a0orgid): a punch is
        # worked hours becoming money, and its device decides which
        # property's P&L carries them.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_punch_employee_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "kiosk_device_id"],
            ["kiosk_device.org_id", "kiosk_device.device_id"],
            name="fk_punch_kiosk_device_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "timecard_id"],
            ["timecard.org_id", "timecard.timecard_id"],
            name="fk_punch_timecard_org",
        ),
    )

    punch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    kiosk_device_id: Mapped[int] = mapped_column(Integer)
    # "clock_in" | "lunch_start" | "lunch_end" | "clock_out"
    punch_type: Mapped[str] = mapped_column(String(20))
    punched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    business_date: Mapped[date] = mapped_column(Date)
    photo_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Set at assembly (B2). NULL until the punch is rolled into a timecard.
    timecard_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # F1: what identification concluded, gating APPROVAL, never recording
    # (wage rule). NULL = recorded before/without matching — deliberately
    # distinct from 'no_template' (matching ran, found nobody enrolled).
    # 'verified' requires a score AND cleared the top1-top2 margin; a close
    # second-best is 'unverified', never a guess.
    match_state: Mapped[str | None] = mapped_column(String(12), nullable=True)
    match_score: Mapped[float | None] = mapped_column(
        Numeric(4, 3, asdecimal=False), nullable=True
    )


class EmployeeFaceTemplate(OrgScoped, Base):
    """One employee's face embedding for kiosk identification (F1).

    Encrypted at rest with `field_encryption_key` — server-readable, because
    matching needs the vector in memory, deliberately NOT HPKE-sealed like
    payroll PII. One row per employee: re-enrollment REPLACES; termination
    DELETES (retention posture: separation + <=30 days). Nothing FKs to this
    table, so deletion never cascades into punches or timecards.

    `notice_version`/`notified_on` record which notice-at-collection document
    the employee was shown, and when — the CA compliance artifact IS this
    data model (plan decision 7). They are a pair: half a fact is refused.
    """

    __tablename__ = "employee_face_template"
    __table_args__ = (
        UniqueConstraint("employee_id"),
        CheckConstraint(
            "(notice_version IS NULL) = (notified_on IS NULL)",
            name="ck_face_template_notice_pair",
        ),
        # Composite (l1a0orgid): biometric PII must anchor inside the org.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_employee_face_template_employee_org",
        ),
    )

    template_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    employee_id: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[bytes] = mapped_column(EncryptedBytes())
    # Which model produced the vector. Embeddings from different models are
    # not comparable — identification (F2) must refuse a template whose
    # version differs from the loaded model's rather than score garbage.
    model_version: Mapped[str] = mapped_column(String(50))
    notice_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notified_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))  # actor keycloak subject
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Timecard(OrgScoped, Base):
    """One employee's biweekly card. `open` until a GM approves it; approval
    LOCKS it (no further adjustments) and starts the photo retention clock.

    There is no `submitted` state: employees have no login, so no actor could
    ever submit. See the B2 plan's deviation note."""

    __tablename__ = "timecard"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_start"),
        # Composite-FK anchor of the timecard chain (l1a0orgid).
        UniqueConstraint("org_id", "timecard_id", name="uq_timecard_org"),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_timecard_employee_org",
        ),
    )

    timecard_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")  # "open" | "approved"
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    photos_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TimecardAdjustment(OrgScoped, Base):
    """A manager correction. Punches are IMMUTABLE — a correction is an ADDITIVE
    synthetic event merged into the timeline, never an edit. Every adjustment is
    also written to `audit_event`."""

    __tablename__ = "timecard_adjustment"
    # Composite (l1a0orgid): an adjustment is corrected minutes (money)
    # merged into a card and attributed to a property P&L.
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "timecard_id"],
            ["timecard.org_id", "timecard.timecard_id"],
            name="fk_timecard_adjustment_timecard_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_timecard_adjustment_property_org",
        ),
    )

    adjustment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timecard_id: Mapped[int] = mapped_column(Integer)
    punch_type: Mapped[str] = mapped_column(String(20))
    adjusted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    business_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(300))
    actor_subject: Mapped[str] = mapped_column(String(64))
    # WHERE the corrected work happened. A punch carries this implicitly through
    # its kiosk device; an adjustment has no device, so without this column a
    # correction's minutes cannot be attributed to a property — and for an
    # employee working two hotels that means guessing which P&L carries the cost.
    #
    # NULLABLE only for rows written before E1. Those are attributed
    # proportionally to the same day's device-derived split (see
    # usali.attribution). New adjustments should always set it.
    property_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsaliLaborFact(OrgScoped, Base):
    """Estimated labor cost + hours promoted from an APPROVED timecard.

    Deliberately has NO stage_id / ingest_batch_id: labor has no PMS file or
    stage row — its provenance is the `timecard_id`. (Every PMS-sourced fact
    requires a NOT-NULL unique stage FK; labor cannot satisfy that, which is
    exactly why it is a separate table unioned at the reporting layer rather
    than a row in usali_financial_fact.) `UsaliSegmentFact` is the precedent for
    natural-key uniqueness in place of a stage FK.

    The row is a DEPARTMENT-LEVEL aggregate. Individual pay rates are read
    server-side during promote and never stored here — only summed cost. `est_cost`
    is an ESTIMATE (meal-break premiums are not priced; Pillar C supersedes it)."""

    __tablename__ = "usali_labor_fact"
    __table_args__ = (
        # One row per (timecard, day, department) — a re-promote replaces rather
        # than double-counts.
        UniqueConstraint("property_id", "business_date", "department_id", "timecard_id"),
        # Composite (l1a0orgid): promoted wage cost — its lineage card and
        # its attribution axis both stay inside the org.
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_usali_labor_fact_department_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "timecard_id"],
            ["timecard.org_id", "timecard.timecard_id"],
            name="fk_usali_labor_fact_timecard_org",
        ),
    )

    labor_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    business_date: Mapped[date] = mapped_column(Date)
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(10, 2))       # total worked (reg+ot+dt)
    ot_hours: Mapped[float] = mapped_column(Numeric(10, 2))    # premium hours (ot+dt)
    est_cost: Mapped[float] = mapped_column(Numeric(15, 4))    # ESTIMATE
    timecard_id: Mapped[int] = mapped_column(Integer)
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmployeePayrollProfile(OrgScoped, Base):
    """Client-side-sealed PII, isolated from the hot `employee` row so the ePHI
    is separately gated/audited/rotated. Every *_sealed column stores an opaque
    HPKE envelope (see usali.pii_crypto) the server CANNOT read. No plaintext
    columns; account_type is non-sensitive."""

    __tablename__ = "employee_payroll_profile"
    __table_args__ = (
        UniqueConstraint("employee_id"),
        # Composite (l1a0orgid): sealed identity PII anchors inside the org.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_employee_payroll_profile_employee_org",
        ),
    )

    profile_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    ssn_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)
    # bank_account_sealed / bank_routing_sealed / account_type DROPPED in E5
    # (e5b0dropbank): the deposit destination is a CHAIN on `deposit_account`.
    # Identity PII (ssn, tax elections) stays here.
    tax_elections_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DepositAccount(OrgScoped, Base):
    """One sealed deposit destination in an employee's allocation chain (E5).

    Replaces the singular `bank_account_sealed`/`bank_routing_sealed` that
    lived on `employee_payroll_profile`. The chain routes NET pay: non-final
    accounts carve off a fixed `amount` (dollars) or `percent` (of what the
    amounts leave behind), and EXACTLY ONE `remainder` account — always the
    last ordinal — receives everything left. That shape is enforced three
    times on purpose (schema constraints below, `allocation_violation()` at
    the API door and at preflight): money that sums to 99% is silently lost
    and 101% is a provider rejection, so an illegal chain must be
    unrepresentable, not merely unlikely.

    Sealing: `sealed_account`/`sealed_routing` are opaque HPKE envelopes the
    server CANNOT read (C1). New envelopes bind their slot with aad
    `{employee_id}:deposit:{ordinal}:{account|routing}`, so two accounts'
    ciphertexts are not interchangeable. Backfilled rows carry the OLD
    envelopes verbatim — the migration can never re-seal — and are marked
    `legacy_sealed`, telling the open path to derive the legacy aad
    (`{employee_id}:bank_account` / `{employee_id}:bank_routing`). The aad is
    never stored: it derives from row identity, which is what makes it a
    binding. Flipping `legacy_sealed` on any other row only makes the open
    FAIL (HPKE auth), which refuses the run rather than paying wrongly.
    """

    __tablename__ = "deposit_account"
    __table_args__ = (
        UniqueConstraint("employee_id", "ordinal"),
        # remainder ⟺ no value, the E3 CHECK pattern. Mirrored in e5a0deposit.
        CheckConstraint(
            "(allocation_type = 'remainder') = (allocation_value IS NULL)",
            name="ck_deposit_remainder_iff_valueless",
        ),
        Index(
            "uq_one_remainder_per_employee",
            "employee_id",
            unique=True,
            postgresql_where=text("allocation_type = 'remainder'"),
        ),
        # Composite (l1a0orgid): a deposit chain is where net pay GOES —
        # attached across the wall it routes another org's money.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_deposit_account_employee_org",
        ),
    )

    deposit_account_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    employee_id: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)  # 1..N, deposit priority
    allocation_type: Mapped[str] = mapped_column(String(10))  # amount|percent|remainder
    allocation_value: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    # checking | savings | NULL (unknown). NULL exists ONLY on backfilled
    # rows whose pre-E5 profile never stated a type — the E5 review's finding:
    # COALESCE'ing to 'checking' invented an affirmative claim the employee
    # never made, on the column that tells the provider which account class
    # to route an ACH to. Unknown stays unknown; preflight names it, and the
    # API requires a real type on every new write.
    account_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sealed_account: Mapped[str] = mapped_column(Text)
    sealed_routing: Mapped[str] = mapped_column(Text)
    legacy_sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SickLeaveLedger(OrgScoped, Base):
    """One append-only sick-leave fact (E4). Balance, caps, and cost are all
    DERIVED from this table — never stored — so a re-promote or late approval
    reproduces the same answers from the same facts and no scalar goes stale.

    `accrual` rows (positive) are written by timecard promotion, idempotently
    keyed on `timecard_id`, UNCAPPED in `hours`: the accrual cap applies at
    read time by `sick_leave.balance_on`'s fold. Each POSITIVE entry records
    `cap_hours` — the accrual cap in force WHEN IT HAPPENED, derived from the
    facts as of its own date — and the fold clamps at each day's recorded
    cap. Recording the cap as a fact is what makes the fold non-retroactive:
    the E4 review proved that clamping history at TODAY'S cap extinguishes
    lawfully banked hours whenever an employee's day length decays (an
    unlawful denial), and that today's-D folds silently restate a quiet
    quarter's balance. NULL cap_hours = no clamp (no day-length data, or a
    jurisdiction with no mandate) — employee-favorable, lawful.

    `usage` rows (negative), `usage_void` rows (positive — they reverse a
    mistaken usage AND restore the calendar-year cap headroom the mistake
    consumed, which a plain adjustment cannot do), and `adjustment` rows
    (either sign; also the audited human path for OPENING balances — the
    incumbent's numbers are not trustworthy enough to backfill, see
    docs/reference/ca-sick-leave.md) come through the payroll-gated API.
    `note` is context for adjustments and is NEVER money, never PII.
    """

    __tablename__ = "sick_leave_ledger"
    __table_args__ = (
        # The sign IS the semantics; a positive usage row would silently
        # grant hours. Mirrored in e4a0sick.
        CheckConstraint(
            "(entry_type = 'accrual' AND hours > 0) OR "
            "(entry_type = 'usage' AND hours < 0) OR "
            "(entry_type = 'usage_void' AND hours > 0) OR "
            "(entry_type = 'adjustment' AND hours <> 0)",
            name="ck_sick_ledger_sign_matches_type",
        ),
        # A client retry must not silently double-book sick hours (the punch
        # debounce, applied to this surface): an identical usage for one
        # employee-day is refused; a genuine second same-day absence of the
        # same length is recorded via a differing figure or an adjustment.
        Index(
            "uq_sick_usage_dedup",
            "employee_id", "effective_on", "hours",
            unique=True,
            postgresql_where=text("entry_type = 'usage'"),
        ),
        # Composite (l1a0orgid): statutory paid-leave hours are money.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_sick_leave_ledger_employee_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "timecard_id"],
            ["timecard.org_id", "timecard.timecard_id"],
            name="fk_sick_leave_ledger_timecard_org",
        ),
    )

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    # accrual | usage | usage_void | adjustment
    entry_type: Mapped[str] = mapped_column(String(10))
    hours: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    # The accrual cap in force when this POSITIVE entry happened (see class
    # docstring). NULL on negative entries and where no data/mandate governs.
    cap_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    effective_on: Mapped[date] = mapped_column(Date)
    timecard_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaySchedule(OrgScoped, Base):
    """A property's pay schedule. `anchor` is display/config metadata
    constrained (at the API) to the platform payroll grid — all period math
    runs on settings.payroll_period_anchor; per-property grids are future work."""

    __tablename__ = "pay_schedule"
    __table_args__ = (
        UniqueConstraint("property_id"),
        # Composite (l8a0tenancyfix / L8-F2): the one-per-property unique is
        # GLOBAL on property_id, so a single-column FK let an org-2 session
        # SQUAT org 1's pay_schedule slot (the FK is checked with owner
        # privileges, bypassing RLS) — a cross-tenant denial of service on a
        # row RLS makes the victim unable to even see. The composite FK makes
        # the cross-org reference structurally impossible, the same wall the
        # money/PII chain uses.
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_pay_schedule_property_org",
        ),
    )

    pay_schedule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    frequency: Mapped[str] = mapped_column(String(20), default="biweekly")
    anchor: Mapped[date] = mapped_column(Date)
    check_date_offset_days: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoomInventory(OrgScoped, Base):
    """A property's sellable-room count, EFFECTIVE-DATED. The count in force
    for a date D is the row with the greatest `effective_date <= D`; to change
    the count you INSERT a new row, so a renovation never rewrites history
    (issue #8). One count per property per effective date; a re-POST for the
    same date is a correction (upsert)."""

    __tablename__ = "room_inventory"
    __table_args__ = (
        UniqueConstraint("property_id", "effective_date", name="uq_room_inventory_prop_date"),
        CheckConstraint("total_rooms > 0", name="ck_room_inventory_total_positive"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_room_inventory_property_org",
        ),
    )

    inventory_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    effective_date: Mapped[date] = mapped_column(Date)
    total_rooms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutOfOrderRoom(OrgScoped, Base):
    """A block of rooms out of order / out of service for a date range, with a
    reason (issue #8). Blocks may overlap the query window partially and may
    overlap each other; room-nights sum across blocks."""

    __tablename__ = "out_of_order_room"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_ooo_range"),
        CheckConstraint("room_count > 0", name="ck_ooo_count_positive"),
        # Literal mirror of OOO_REASON_CODES (kept in sync by test).
        CheckConstraint(
            "reason_code IN ('maintenance', 'renovation', 'damage', 'deep_clean', 'other')",
            name="ck_ooo_reason_code",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ooo_property_org",
        ),
    )

    ooo_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    room_count: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FiscalCalendar(OrgScoped, Base):
    """A property's fiscal calendar (issue #8). One row per property (the
    PaySchedule precedent). `calendar_month` runs the twelve calendar months
    from `fiscal_year_start_month`; `445` runs 4/4/5-week periods anchored on
    the first `week_start_weekday` on/after the 1st of the start month. The two
    are a pair: `week_start_weekday` is present iff the type is `445`."""

    __tablename__ = "fiscal_calendar"
    __table_args__ = (
        CheckConstraint(
            "calendar_type IN ('calendar_month', '445')", name="ck_fiscal_type"
        ),
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12", name="ck_fiscal_start_month"
        ),
        CheckConstraint(
            "week_start_weekday IS NULL OR week_start_weekday BETWEEN 0 AND 6",
            name="ck_fiscal_weekday_range",
        ),
        # Paired: 445 <=> a weekday is set (the biometric notice-pair idiom).
        CheckConstraint(
            "(calendar_type = '445') = (week_start_weekday IS NOT NULL)",
            name="ck_fiscal_weekday_pair",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_fiscal_calendar_property_org",
        ),
    )

    property_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    calendar_type: Mapped[str] = mapped_column(String(20))
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer)
    week_start_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProviderEmployeeRef(OrgScoped, Base):
    """The provider-side id for an employee, per provider — so switching
    providers re-syncs rather than clobbering the old mapping."""

    __tablename__ = "provider_employee_ref"
    __table_args__ = (
        UniqueConstraint("employee_id", "provider"),
        # Composite (l1a0orgid): the sync path ships PII payloads keyed on
        # this mapping — it must not span orgs.
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_provider_employee_ref_employee_org",
        ),
    )

    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(20))  # "gusto" | "adp"
    provider_employee_id: Mapped[str] = mapped_column(String(100))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # H2 (decision 8): SHA-256 hex over exactly the fields the sync payload
    # ships (ciphertext, never opened). Staleness = stored != current (H7);
    # NULL — every pre-H ref — reads STALE, so one re-send self-heals it.
    # `synced_at` becomes display-only once H7 lands.
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PayRun(OrgScoped, Base):
    """One pay run per property + period. status: draft -> submitted ->
    processed | failed. A failed run may be replaced by re-POSTing the period."""

    __tablename__ = "pay_run"
    __table_args__ = (
        UniqueConstraint("property_id", "period_start"),
        # Composite-FK anchor of the pay surfaces (l1a0orgid).
        UniqueConstraint("org_id", "pay_run_id", name="uq_pay_run_org"),
    )

    pay_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    check_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    provider: Mapped[str] = mapped_column(String(20))
    provider_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)


class PayRunLine(OrgScoped, Base):
    """Per-employee actual gross-to-net returned by the provider. Money fields
    are ENCRYPTED at rest via the compute-on symmetric regime (like
    `AssignmentRate.amount`) —
    the server aggregates them into department facts; C3's Payroll-Admin detail
    endpoint reads them. NOT HPKE-sealed: the server must compute on them."""

    __tablename__ = "pay_run_line"
    __table_args__ = (
        UniqueConstraint("pay_run_id", "employee_id"),
        # A negative would let a void REDUCE what a run claims it paid,
        # flipping the H6 comparison direction.
        CheckConstraint("sick_hours >= 0",
                        name="ck_pay_run_line_sick_hours_nonneg"),
        # All composite (l1a0orgid): the named FK-bypass case — a line
        # referencing another org's employee pays that person from this
        # org's run; referencing another org's run injects money into it.
        ForeignKeyConstraint(
            ["org_id", "pay_run_id"],
            ["pay_run.org_id", "pay_run.pay_run_id"],
            name="fk_pay_run_line_pay_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_pay_run_line_employee_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_pay_run_line_department_org",
        ),
    )

    line_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pay_run_id: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[int] = mapped_column(Integer)
    # Department SNAPSHOT taken at fetch time — the same live
    # Employee.department_id the department aggregation read at that moment,
    # frozen so reporting-side employee counts and the aggregated money share
    # ONE attribution (a later transfer must not re-key this line). NULL only
    # on legacy pre-C3 rows fetched before the column existed.
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(10, 2))
    # H2 (decision 5): the statutory sick hours THIS run paid, frozen at
    # execute time — a stored fact, so the §246(n) guard (H6) and the
    # apportionment split (H5) never re-derive from a ledger that kept
    # moving after submission. 0 on every pre-H line (they recorded no
    # sick) and on any insert that omits it; a negative is CHECK-refused.
    sick_hours: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("0")
    )
    gross: Mapped[str] = mapped_column(EncryptedString())          # decimal string
    employee_taxes: Mapped[str] = mapped_column(EncryptedString())
    employer_taxes: Mapped[str] = mapped_column(EncryptedString())
    net: Mapped[str] = mapped_column(EncryptedString())


class AssignmentRate(OrgScoped, Base):
    """One rate, for one PLACEMENT, for one date range.

    Rates belong to an assignment, not to a person: the same employee can earn
    one rate at the front desk and another in laundry, and both change over
    time. `Employee.pay_rate` was a single scalar with no dating, so a raise
    entered today silently re-costed already-worked hours — an April period
    costing $160 became $240 because a raise dated 1 August was entered, and a
    filed Schedule 14 no longer tied to what was filed.

    Dating is **[effective_from, effective_to)**, identical to
    `EmployeeAssignment`. Resolve through `usali.rates`, never with ad-hoc date
    comparisons — the same rule, and the same reason, as `usali.assignments`.

    ## Why all four types are STORED rather than derived

    `ot` and `dot` are computable today as `regular x 1.5` and `regular x 2`.
    That is a coincidence of the two jurisdictions currently encoded (FLSA and
    CA), not a property of the domain: states do change relationships this
    fundamental, and a stored rate is also how union and contract premiums are
    expressed. Deriving them would mean reopening the costing path later to add
    the modelling.

    **A stored `ot`/`dot` rate may exceed the statutory floor but never fall
    below it.** Above is lawful and is the whole point. Below is a data error,
    refused rather than silently lifted (which pays a figure no record shows) or
    silently honoured (which underpays against a rule we hold a citation for).
    Enforced in `usali.rates`.

    `amount` is `EncryptedString` because it is compensation, and it is stored
    as a decimal STRING for the same reason `Employee.pay_rate` was — the
    ciphertext is opaque to the database, so no SQL arithmetic can touch it and
    every consumer must go through the resolver.
    """

    __tablename__ = "assignment_rate"
    __table_args__ = (
        UniqueConstraint("assignment_id", "rate_type", "effective_from"),
        # Composite (l1a0orgid): a rate is compensation pricing a placement —
        # priced onto another org's placement it crosses the wall.
        ForeignKeyConstraint(
            ["org_id", "assignment_id"],
            ["employee_assignment.org_id", "employee_assignment.assignment_id"],
            name="fk_assignment_rate_assignment_org",
        ),
    )

    assignment_rate_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    assignment_id: Mapped[int] = mapped_column(Integer)
    # "regular" | "ot" | "dot" | "holiday"
    rate_type: Mapped[str] = mapped_column(String(20))
    amount: Mapped[str] = mapped_column(EncryptedString())
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PayRunLineProperty(OrgScoped, Base):
    """One employee's share of one pay run, PER PROPERTY. The suppression census.

    ## Why this table exists

    Since E1 a paycheck is issued by the employee's primary property but the COST
    is apportioned across every property they actually worked at. `PayRunLine`
    records the whole paycheck and has no property dimension, so it answers
    "who was paid by this run", never "whose money is in THIS property's number".

    Counting the wrong one disclosed an individual's exact pay. An employee whose
    primary is A but who worked the period at B contributes a HEADCOUNT to A and
    ZERO DOLLARS to it — so a department where one person actually worked read as
    two, escaped suppression, and published that person's gross and employer
    burden. `160.00 / 8.00h` re-derives their hourly rate. That was the sixth
    instance of the same counting bug (D1), and the first where the population
    and the money came from different tables.

    `fetch_pay_run_results` already computes this split to write the department
    aggregates; it simply threw it away. Persisting it means the census and the
    money are written from the SAME apportionment in the same transaction and
    cannot disagree — the fetch-time-snapshot principle, extended to property.

    ## Why rows and not a stored count

    A headcount column would double-count anyone appearing in two pay runs for
    one reporting window (1 + 1 = 2 falsely un-suppresses a solo department).
    Distinct `employee_id` over rows is correct by construction.

    This is a CENSUS, not a disclosure surface: nothing renders it, and `gross`
    lives here only so the gate can count the PRICED population — an employee
    whose share of this property is zero must not lift it over the floor.
    """

    __tablename__ = "pay_run_line_property"
    __table_args__ = (
        UniqueConstraint("pay_run_id", "employee_id", "property_id"),
        # All composite (l1a0orgid): the suppression census — a cross-org
        # member either discloses pay or falsely lifts the floor.
        ForeignKeyConstraint(
            ["org_id", "pay_run_id"],
            ["pay_run.org_id", "pay_run.pay_run_id"],
            name="fk_pay_run_line_property_pay_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_pay_run_line_property_employee_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_pay_run_line_property_department_org",
        ),
    )

    pay_run_line_property_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    pay_run_id: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[int] = mapped_column(Integer)
    property_id: Mapped[str] = mapped_column(String(50))
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Decimal, not float (I1): Numeric returns Decimal at runtime, and fetch
    # now ASSIGNS Decimal money onto these rows in place — the float
    # annotation was a fiction the constructor path never exposed.
    hours: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    gross: Mapped[Decimal] = mapped_column(Numeric(15, 4))
    employer_burden: Mapped[Decimal] = mapped_column(Numeric(15, 4))


class WageSettlement(OrgScoped, Base):
    """One settlement ACT: a named worked-hours delta on a submitted run,
    paid OUTSIDE the integration and acknowledged by an authorized actor.

    The missing terminal resolution for the worked-hours guard
    (`_paid_worked_problems`): a reopened card whose re-derived hours
    exceed what the run paid blocks every later run at the property
    FOREVER unless the operator either voids the extra time (wrong — it
    was worked) or pays it off-system with no record (worse). This row IS
    the record. The server computes the delta itself (derived minus
    stored minus already-settled) and stores exactly that — the caller
    acknowledges what IS, never a figure they typed, so a settlement can
    neither overshoot nor mask a later drift.

    Several rows per (run, employee) are LEGAL by design: a post-
    settlement punch re-blocks with the residual only, and that residual
    settles as its own act. The CHECK is the semantics — a negative
    settlement would UN-PAY someone, and a zero one would record an act
    that had no effect (the F6 acknowledgment rule). `note` is bounded
    operator free text like `TimecardAdjustment.reason`: audit-surface
    only, never echoed into preflight strings."""

    __tablename__ = "wage_settlement"
    __table_args__ = (
        CheckConstraint("hours > 0", name="ck_wage_settlement_hours_positive"),
        # Composite (l1a0orgid): a settlement is a money act on a run for a
        # person — both must be this org's.
        ForeignKeyConstraint(
            ["org_id", "pay_run_id"],
            ["pay_run.org_id", "pay_run.pay_run_id"],
            name="fk_wage_settlement_pay_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_wage_settlement_employee_org",
        ),
    )

    settlement_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    pay_run_id: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[int] = mapped_column(Integer)
    hours: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    actor_subject: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CrmPullBatch(OrgScoped, Base):
    """One explicit, audited CRM pull (Pillar J, plan decisions 3 and 6).

    The batch is the as-of identity for everything the pull wrote: its
    snapshot rows point here, and the pull's AuditEvent points here
    (resource_type "crm_pull_batch" — the settlement idiom: the domain
    row carries the detail, the audit row points at it). `property_id`
    lives ONLY on the batch — a snapshot row's property is its batch's
    property, one copy, so the two can never disagree. `pulled_at` is
    record-keeping (which pull is newest), never compared against
    business clocks by any guard — demand is decision-support data.
    """

    __tablename__ = "crm_pull_batch"
    __table_args__ = (
        # A pull whose horizon ends before it starts fetched nothing and
        # means nothing — refuse at the schema, not just the endpoint.
        CheckConstraint("horizon_end >= horizon_start",
                        name="ck_crm_pull_batch_horizon"),
    )

    batch_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    property_id: Mapped[str] = mapped_column(
        ForeignKey("property.property_id")
    )
    provider: Mapped[str] = mapped_column(String(30))  # delphi | tripleseat
    horizon_start: Mapped[date] = mapped_column(Date)
    horizon_end: Mapped[date] = mapped_column(Date)
    pulled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CrmDemandSnapshot(OrgScoped, Base):
    """One stay-date's demand as ONE pull saw it — append-only, forever.

    Booking pace is inherently temporal: "Aug 15 has 140 rooms on the
    books today vs 90 a week ago" compares SNAPSHOTS, so rows are never
    updated in place and no unique wider than (batch_id, stay_date)
    exists — a re-pull is a new batch carrying the same stay dates, and
    forbidding that would destroy the only data that makes pace
    computable. Within one batch a stay-date appears once (a batch that
    says two different things about one day is corrupt).

    Figures are `int | None` mirroring `CrmDemandDay`: NULL means "this
    provider does not speak that dimension", never zero, and negatives
    refuse at the schema. `labels` is the bounded, comma-joined
    block/event display string — scheduler-surface working data (a
    wedding's label is somebody's name): never logged, never on the
    kiosk (plan decision 5).
    """

    __tablename__ = "crm_demand_snapshot"
    __table_args__ = (
        UniqueConstraint("batch_id", "stay_date"),
        CheckConstraint("rooms_on_books >= 0",
                        name="ck_crm_demand_rooms_on_books_nonneg"),
        CheckConstraint("group_rooms >= 0",
                        name="ck_crm_demand_group_rooms_nonneg"),
        CheckConstraint("event_covers >= 0",
                        name="ck_crm_demand_event_covers_nonneg"),
    )

    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("crm_pull_batch.batch_id")
    )
    stay_date: Mapped[date] = mapped_column(Date)
    rooms_on_books: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_rooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_covers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    labels: Mapped[str] = mapped_column(String(300))


class UsaliActualLaborFact(OrgScoped, Base):
    """ACTUAL labor cost from a processed pay run, PERIOD-grain (the provider's
    truth — no fabricated daily spread), department AGGREGATES only. Lineage =
    pay_run_id; unique (pay_run_id, property_id, department_id) so a re-fetch
    replaces rather than double-counts (mirrors usali_labor_fact /
    UsaliSegmentFact — no stage FK).

    PROPERTY IS PART OF THE KEY since E1. One pay run now writes facts for every
    property the employee worked at — the paycheck comes from their primary
    property, but the cost belongs to each. Two properties share a department_id
    whenever the department snapshot is the primary assignment's, so keying on
    (pay_run_id, department_id) alone made the second row a duplicate-key
    violation and the multi-property write could not execute at all."""

    __tablename__ = "usali_actual_labor_fact"
    __table_args__ = (
        UniqueConstraint("pay_run_id", "property_id", "department_id"),
        # Composite (l1a0orgid): actual labor cost — its lineage run and its
        # attribution axis both stay inside the org.
        ForeignKeyConstraint(
            ["org_id", "pay_run_id"],
            ["pay_run.org_id", "pay_run.pay_run_id"],
            name="fk_usali_actual_labor_fact_pay_run_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_usali_actual_labor_fact_department_org",
        ),
    )

    actual_fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(10, 2))
    gross: Mapped[float] = mapped_column(Numeric(15, 4))
    employer_burden: Mapped[float] = mapped_column(Numeric(15, 4))
    pay_run_id: Mapped[int] = mapped_column(Integer)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShiftTemplate(OrgScoped, Base):
    """A recurring shift shape (e.g. Front Desk AM 7-15). Times are PROPERTY-LOCAL
    (the property's IANA timezone); a crosses_midnight shift ends the next
    calendar day. Templates are provenance only — editing one never rewrites
    shifts already created from it."""

    __tablename__ = "shift_template"
    __table_args__ = (UniqueConstraint("property_id", "name"),)

    template_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("department.department_id"))
    name: Mapped[str] = mapped_column(String(100))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    crosses_midnight: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Schedule(OrgScoped, Base):
    """One property's week. week_start is a Monday on the SAME grid as the
    payroll workweek (validated at the API), so weekly-40/7th-day projection is
    exact within the week. draft -> published; republish bumps version."""

    __tablename__ = "schedule"
    __table_args__ = (UniqueConstraint("property_id", "week_start"),)

    schedule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    week_start: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    version: Mapped[int] = mapped_column(Integer, default=0)  # bumped on each publish
    published_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Shift(OrgScoped, Base):
    """One shift on one business date. employee_id NULL = an OPEN shift —
    planned coverage nobody is assigned to yet (counts toward department hours,
    toward no one's OT, and produces no cost)."""

    __tablename__ = "shift"
    # Composite on the employee only (l1a0orgid): a placement of a PERSON.
    # The schedule/department/template FKs stay single-column — scheduling
    # config, no money or PII in the reference.
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employee.org_id", "employee.employee_id"],
            name="fk_shift_employee_org",
        ),
    )

    shift_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedule.schedule_id"))
    business_date: Mapped[date] = mapped_column(Date)
    department_id: Mapped[int] = mapped_column(ForeignKey("department.department_id"))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    crosses_midnight: Mapped[bool] = mapped_column(Boolean, default=False)
    employee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_template.template_id"), nullable=True
    )


class LaborStandard(OrgScoped, Base):
    """How a department's target hours derive from demand. fixed_hours_per_day
    is a constant daily target; minutes_per_occupied_room multiplies the GM's
    occupancy forecast. One standard per department."""

    __tablename__ = "labor_standard"
    __table_args__ = (
        UniqueConstraint("department_id",),
        # Composite (l8a0tenancyfix / L8-F2 re-audit): the SAME squat shape as
        # pay_schedule — a GLOBAL one-per-department unique backed by a
        # single-column FK, so an org-2 session could plant a labor_standard
        # for org 1's department and permanently deny org 1 its own. The
        # composite (org_id, department_id) FK closes it. (The property FK
        # stays single-column: no unique rides property_id here, so it carries
        # only the L1-judged provenance risk, not a squat.)
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["department.org_id", "department.department_id"],
            name="fk_labor_standard_department_org",
        ),
    )

    standard_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    department_id: Mapped[int] = mapped_column(Integer)
    basis: Mapped[str] = mapped_column(String(30))  # fixed_hours_per_day | minutes_per_occupied_room
    value: Mapped[float] = mapped_column(Numeric(10, 2))


class OccupancyForecast(OrgScoped, Base):
    """GM-entered expected occupied rooms for one property-day. The UI shows
    history HINTS beside the input (same-day-last-week, trailing average from
    our own promoted ROOMS_OCCUPIED facts) — hints inform, never dictate."""

    __tablename__ = "occupancy_forecast"
    __table_args__ = (UniqueConstraint("property_id", "business_date"),)

    forecast_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("property.property_id"))
    business_date: Mapped[date] = mapped_column(Date)
    occupied_rooms: Mapped[int] = mapped_column(Integer)
    entered_by: Mapped[str] = mapped_column(String(64))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
