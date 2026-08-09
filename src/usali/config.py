from datetime import date
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_DEFAULT_FIELD_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEE="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="USALI_", env_file=".env")

    # Deployment environment. "prod" turns on fail-fast guards that refuse to run
    # under committed dev-default secrets. Set via USALI_ENV.
    env: str = "dev"

    db_url: str = "postgresql+psycopg://usali:usali@localhost:5433/usali"
    inbox_dir: Path = Path("inbox")
    processed_dir: Path = Path("processed")
    failed_dir: Path = Path("failed")

    # QuickBooks Online (P8). Defaults target the local mock (`usali qbo-mock`);
    # pointing at real Intuit is a base-URL + credentials change via USALI_QBO_* env.
    qbo_base_url: str = "http://127.0.0.1:9200"
    qbo_client_id: str = "mock"
    qbo_client_secret: str = "mock"
    qbo_realm_id: str = "mock"
    qbo_refresh_token: str = "mock"

    # Payroll provider (Pillar C2). Which adapter the app uses; defaults target
    # the local mocks (`usali gusto-mock` / `usali adp-mock`). Go-live points
    # these at the real sandbox/production endpoints via USALI_* env.
    payroll_provider: str = "gusto"  # "gusto" | "adp"
    gusto_base_url: str = "http://127.0.0.1:9300"
    gusto_api_token: str = "mock"
    gusto_company_id: str = "mock"
    adp_base_url: str = "http://127.0.0.1:9301"
    adp_client_id: str = "mock"
    adp_client_secret: str = "mock"

    # CRM demand feed (Pillar J). Which adapter serves forward-looking
    # demand; EMPTY means the feature is off (the feed factory returns
    # None and the pull surface refuses loudly). Defaults target the
    # local mocks (`usali delphi-mock` / `usali tripleseat-mock`);
    # go-live points these at real endpoints via USALI_* env.
    crm_provider: str = ""  # "delphi" | "tripleseat" | "" = off
    delphi_base_url: str = "http://127.0.0.1:9400"
    delphi_subscription_key: str = "mock"
    tripleseat_base_url: str = "http://127.0.0.1:9401"
    tripleseat_api_key: str = "mock"

    # A labor-variance line is flagged when |actual - estimate| exceeds this
    # percentage of the estimate (Pillar C3). A flag on the statement, not a
    # notification pipeline.
    labor_variance_alert_pct: int = 10

    # Scheduling (Pillar D1). A shift longer than the threshold projects with an
    # assumed unpaid meal (mirroring how such a day actually punches); rest
    # between consecutive shifts under the floor raises a clopening warning.
    schedule_meal_threshold_hours: int = 6
    schedule_meal_deduction_minutes: int = 30
    schedule_min_rest_hours: int = 10

    # Adherence (Pillar D3): the |punched - scheduled| threshold, in minutes,
    # at or above which an elapsed day flags a `deviation` exception.
    schedule_adherence_deviation_minutes: int = 60

    # OIDC / Keycloak (Pillar A1). Defaults target the local `docker compose`
    # Keycloak realm; production points these at the deployed issuer via
    # USALI_OIDC_* env. `oidc_audience` is the JWT `aud` the resource server
    # requires. It is a DEDICATED audience emitted by the operator-portal
    # client's `usali-api-audience` protocol mapper (keycloak/realm-usali.json).
    # Do NOT assume Keycloak supplies "account" — a realm client with no
    # audience mapper issues tokens with NO `aud` claim at all, which the
    # verifier rejects (401) and the SPA turns into a login loop.
    # tests/test_oidc_realm_contract.py pins realm mapper == this value.
    oidc_issuer: str = "http://localhost:9080/realms/usali"
    oidc_jwks_url: str = (
        "http://localhost:9080/realms/usali/protocol/openid-connect/certs"
    )
    oidc_audience: str = "usali-api"

    # Field-level encryption (A2.2). AES-256-GCM key as base64(32 bytes). The
    # default is DEV/TEST ONLY — production MUST set USALI_FIELD_ENCRYPTION_KEY
    # (AWS Secrets Manager). Rotating the key makes existing ciphertext
    # undecryptable (no envelope/versioning yet — that is a Pillar C concern).
    field_encryption_key: str = _DEV_DEFAULT_FIELD_ENCRYPTION_KEY

    # Keycloak admin API (Pillar A2.3 onboarding). The app provisions/disables
    # operator users via a confidential service-account client (`usali-admin`).
    # Dev defaults target local Keycloak; prod overrides via USALI_KC_ADMIN_*.
    kc_admin_base_url: str = "http://localhost:9080"
    kc_admin_realm: str = "usali"
    kc_admin_client_id: str = "usali-admin"
    kc_admin_client_secret: str = "dev-admin-secret"

    # Time & attendance (Pillar B1). A punch before this hour (property-local)
    # is attributed to the PRIOR business date, matching the PMS night audit.
    punch_business_day_cutoff_hour: int = 4

    # Punch photos: encrypted on disk in dev; S3 + SSE-KMS in prod. Purged this
    # many days after the timecard is approved (the purge job lands in B2).
    photo_store_dir: Path = Path("punch-photos")
    punch_photo_retention_days: int = 90
    # Cloud demo (K1): a non-empty bucket name selects the GCS-backed
    # store (app-side ciphertext; Cloud Run disks are ephemeral). Empty
    # keeps the local encrypted-file store — the dev/pilot default.
    photo_store_gcs_bucket: str = ""

    # Face-verified clock-in (Pillar F). Ships DARK: matching is off until
    # explicitly enabled, and the kiosk falls back to the search-only flow.
    # Thresholds are model-calibrated BEFORE enabling (plan decision 8):
    # `verified` requires score >= threshold AND a top1-top2 margin — a close
    # second-best is grey, never a guess. Model files live outside the repo
    # (sha256-pinned fetch script, F2); the dir is where they land.
    biometric_matching_enabled: bool = False
    face_model_dir: Path = Path("models/face")
    face_match_threshold: float = 0.60
    face_match_margin: float = 0.10
    # The version label of the employee notice-at-collection document in
    # force. Enrollment stamps it on the template row — the compliance
    # artifact is the data model. Empty is allowed only outside production.
    biometric_notice_version: str = ""

    # Biweekly payroll periods are derived deterministically from this anchor
    # (a Monday). period_index = (business_date - anchor).days // 14.
    payroll_period_anchor: date = date(2026, 1, 5)

    # Client-side-sealed PII (Pillar C1). The recipient PRIVATE key lives in an
    # HSM in prod (HsmOpener, a deploy-time drop-in) — these dev settings feed the
    # SoftwareOpener for local/test only. `pii_hpke_private_key` is base64(PKCS8).
    pii_hpke_key_id: str = "dev-1"
    pii_hpke_private_key: str = ""  # set by the test/dev bootstrap; empty in prod

    @property
    def is_production(self) -> bool:
        # Fail closed: anything not explicitly a known non-prod env is treated as
        # production, so a typo like USALI_ENV=production still trips the guards.
        return self.env.strip().lower() not in {"dev", "test", "local"}

    @model_validator(mode="after")
    def _refuse_dev_secrets_in_prod(self) -> "Settings":
        # In production the committed dev-default symmetric key must never be in
        # effect — it would make every PII column decryptable by anyone with the
        # repo. Fail closed at construction so nothing can run under it.
        if self.is_production and (
            self.field_encryption_key == _DEV_DEFAULT_FIELD_ENCRYPTION_KEY
        ):
            raise ValueError(
                "field_encryption_key is the dev default but env=prod; set "
                "USALI_FIELD_ENCRYPTION_KEY to a real 32-byte key (Secrets Manager)"
            )
        # Production may not compute face templates without a versioned
        # notice-at-collection in force (Civ. Code §1798.100; plan decision
        # 7). Fail at construction so the gap surfaces at deploy, not at the
        # first enrollment.
        if (
            self.is_production
            and self.biometric_matching_enabled
            and not self.biometric_notice_version.strip()
        ):
            raise ValueError(
                "biometric_notice_version is empty but matching is enabled in "
                "production; set USALI_BIOMETRIC_NOTICE_VERSION to the employee "
                "notice-at-collection document version in force "
                "(docs/reference/biometric-attendance.md, checklist item 2)"
            )
        return self


def get_settings() -> Settings:
    return Settings()
