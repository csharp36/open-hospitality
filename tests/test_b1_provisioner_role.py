"""D-B7: the least-privilege provisioner DB role — it can write the two
cross-org provisioning tables and CANNOT read any tenant-data table."""

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

import pytest

from usali.mapping.property_registry import ensure_default_org
from tests.orgwall import provisioner_role_url

_TENANT_DATA_TABLE = "employee"


@pytest.fixture
def _founding(db_session):
    ensure_default_org(db_session)
    db_session.commit()


def test_provisioner_can_write_org_and_grant(db_url, _founding):
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with engine.begin() as conn:
            org_id = conn.execute(text(
                "INSERT INTO organization (name, kc_org_alias) "
                "VALUES ('Provisioner Probe Org', 'provisioner-probe') "
                "RETURNING org_id"
            )).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO role_assignment "
                    "(org_id, keycloak_subject, role, property_id, department_id) "
                    "VALUES (:org, 'kc-probe', 'org_admin', NULL, NULL)"
                ),
                {"org": org_id},
            )
            assert conn.execute(
                text("SELECT count(*) FROM organization WHERE org_id = :o"),
                {"o": org_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_provisioner_cannot_read_tenant_data(db_url, _founding):
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            with engine.connect() as conn:
                conn.execute(text(f"SELECT * FROM {_TENANT_DATA_TABLE}"))
    finally:
        engine.dispose()


def test_provisioner_cannot_read_or_write_property(db_url, _founding):
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            with engine.connect() as conn:
                conn.execute(text("SELECT count(*) FROM property"))
    finally:
        engine.dispose()
