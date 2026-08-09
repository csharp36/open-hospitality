"""L5: the stores outside Postgres go per-org (Pillar L decision 5).

Two independent concerns, one wall each:

- **Photos** (GCS objects / local files, where RLS cannot reach): the
  object key gains an `org/<id>/` prefix AND the AES-256-GCM data key
  becomes per-org, HKDF-derived from the master key salted by org_id. A
  prefix-routing bug therefore yields UNDECRYPTABLE ciphertext, not
  another hotel's punch photos. Org 1 (founding) is DEFINED as the raw
  master key — existing objects stay readable with no re-encrypt.

- **Integration config**: a per-org `org_settings.crm_provider`, read
  under the active org's session; empty = the feature is OFF for that org
  (the J4 loud-refuse posture, now per-tenant). `USALI_CRM_PROVIDER` is
  the SEED default for org 1 only; runtime never reads env.

Mutants killed here: HKDF salt dropped (all org != 1 collapse to one
key), the org prefix dropped (keys collide + both decrypt under org 1),
and the org_settings read falling back to env for org != 1.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from sqlalchemy import update

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from tests.orgwall import app_role_url
from usali.auth import ACTIVE_ORG_HEADER
from usali.crm_feed import CrmCapabilities, CrmDemandDay, InMemoryCrmFeed
from usali.crypto import (
    _PHOTO_MASTER_ORG_ID,
    _key,
    _photo_key,
    decrypt_bytes,
    decrypt_photo,
    encrypt_bytes,
    encrypt_photo,
)
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.mapping.property_registry import ensure_default_org
from usali.models import Organization, OrgSettings, Property
from usali.photo_store import (
    FOUNDING_ORG_ID,
    InMemoryPhotoStore,
    LocalPhotoStore,
    face_reference_key,
    org_from_key,
    org_prefixed_key,
)
from usali.server import create_app
from usali.tenancy import bind_org_context

_JPEG = b"\xff\xd8\xff" + b"another hotel's punch photo" * 8

RIVAL_ALIAS = "rival-hotel-group"


# ======================================================================
# Photos: per-org key + per-org crypto
# ======================================================================


def test_org_a_cannot_decrypt_org_bs_photo_even_handed_the_bytes():
    """THE cross-org photo pin. Two DISTINCT non-founding orgs (2 and 3 —
    both HKDF-derived, so this also kills the salt-dropped mutant, under
    which they would collapse onto one key): a blob sealed for org 3 fails
    the GCM tag when opened as org 2, even with the raw bytes in hand.
    Undecryptable ciphertext is the intended failure of a prefix-routing
    bug — never a readable cross-tenant photo."""
    blob_b = encrypt_photo(_JPEG, org_id=3)

    assert decrypt_photo(blob_b, org_id=3) == _JPEG  # its own org opens it
    with pytest.raises(InvalidTag):
        decrypt_photo(blob_b, org_id=2)  # a sibling org cannot


def test_every_org_derives_a_distinct_key():
    """The salt IS org_id: orgs 1, 2, 3 each seal the same plaintext into
    three mutually-undecryptable ciphertexts. Drop the salt and 2 & 3
    collapse onto one key (info is fixed) — this is what that mutant breaks."""
    blobs = {org: encrypt_photo(_JPEG, org_id=org) for org in (1, 2, 3)}
    for owner, blob in blobs.items():
        assert decrypt_photo(blob, org_id=owner) == _JPEG
        for other in (1, 2, 3):
            if other != owner:
                with pytest.raises(InvalidTag):
                    decrypt_photo(blob, org_id=other)


def test_org_1_key_is_the_master_key_existing_objects_stay_readable():
    """The org-1 compatibility pin. Org 1's photo key is DEFINED as the raw
    master key (no HKDF), so a blob written by the PRE-L5 path
    (`encrypt_bytes`, the master key) decrypts unchanged under org 1 — no
    re-encrypt, no migration window. The two paths are byte-compatible both
    ways for org 1 only."""
    assert _PHOTO_MASTER_ORG_ID == FOUNDING_ORG_ID == 1

    legacy = encrypt_bytes(_JPEG)  # exactly what a pre-L5 object holds
    assert decrypt_photo(legacy, org_id=1) == _JPEG

    fresh = encrypt_photo(_JPEG, org_id=1)
    assert decrypt_bytes(fresh) == _JPEG  # master key opens org 1's fresh object

    # But org 1's key must NOT open a non-founding org's object.
    with pytest.raises(InvalidTag):
        decrypt_photo(encrypt_photo(_JPEG, org_id=2), org_id=1)


def test_photo_key_coerces_org_id_to_int_at_the_branch():
    """The org-1 special case is decided where the branch lives, not assumed
    of callers: a STRINGIFIED founding-org id must still take the master-key
    path, or it would silently HKDF-re-key every existing org-1 photo into
    undecryptable ciphertext. Both forms hit master."""
    master = _key()
    assert _photo_key("1") == _photo_key(1) == master
    # And a stringified non-founding id derives the SAME key as its int form
    # (no accidental second keyspace keyed on str-vs-int).
    assert _photo_key("2") == _photo_key(2) != master


def test_org_prefixed_key_never_produces_an_unprefixed_key():
    """The L6-fallback invariant: every write goes through org_prefixed_key,
    and it can only ever emit `org/<int>/…` — so no write can produce an
    un-prefixed key that would silently fall back to the founding master
    key. Locked in before L6 opens multi-org write paths."""
    for org in (1, 2, 3, 999):
        key = org_prefixed_key(org, "face-reference/5.jpg")
        assert key.startswith(f"org/{org}/")
        assert org_from_key(key) == org
    # A stringified org id coerces to the same int prefix (no `org/1/…` vs
    # `org/01/…` split), so put and delete rebuild one key.
    assert org_prefixed_key("2", "x") == org_prefixed_key(2, "x")


def test_org_prefixed_key_and_recovery_round_trip():
    """The key IS the salt: distinct orgs produce distinct keys, and
    `org_from_key` recovers the org the store derives crypto from. Drop the
    prefix and two orgs' keys collide (and both fall back to org 1) — the
    prefix-dropped mutant."""
    ka = org_prefixed_key(2, "face-reference/7.jpg")
    kb = org_prefixed_key(3, "face-reference/7.jpg")
    assert ka != kb
    assert org_from_key(ka) == 2
    assert org_from_key(kb) == 3


def test_unprefixed_key_is_the_founding_org():
    """A pre-L5 object key (no `org/<id>/` prefix) belongs to the founding
    org — the only org those objects can come from — so it decrypts under
    the master key. A malformed prefix falls back the same way, never to a
    guessed foreign org."""
    assert org_from_key("HISJ/2026-01-01/abc.bin") == FOUNDING_ORG_ID
    assert org_from_key("face-reference/9.jpg") == FOUNDING_ORG_ID
    assert org_from_key("org/notanint/x") == FOUNDING_ORG_ID


def test_local_store_seals_per_org_and_refuses_cross_org_bytes(tmp_path):
    """End to end through LocalPhotoStore: two orgs write the SAME logical
    photo under their own prefixes; each reads its own back, the on-disk
    bytes differ, and org 2's raw ciphertext handed to org 3's crypto fails
    the tag. The store parses the org from the key — one value routes AND
    seals."""
    store = LocalPhotoStore(tmp_path)
    key_b = org_prefixed_key(2, "HISJ/2026-08-02/punch.bin")
    key_c = org_prefixed_key(3, "HISJ/2026-08-02/punch.bin")
    store.put(key_b, _JPEG)
    store.put(key_c, _JPEG)

    assert store.get(key_b) == _JPEG
    assert store.get(key_c) == _JPEG

    raw_b = (tmp_path / key_b).read_bytes()
    raw_c = (tmp_path / key_c).read_bytes()
    assert raw_b != raw_c  # different keys -> different ciphertext
    # Hand org 2's bytes to org 3's key derivation: undecryptable.
    with pytest.raises(InvalidTag):
        decrypt_photo(raw_b, org_id=3)


def test_in_memory_store_two_org_face_keys_never_collide():
    """InMemoryPhotoStore stores PLAINTEXT (no crypto), so its two-org pin
    is on the KEYS: the same employee id enrolled under two orgs occupies
    two distinct slots — no cross-org key collision (the prefix-dropped
    mutant would overwrite one with the other)."""
    store = InMemoryPhotoStore()
    store.put(face_reference_key(2, 42), b"\xff\xd8\xffA")
    store.put(face_reference_key(3, 42), b"\xff\xd8\xffB")
    assert store.keys() == {
        face_reference_key(2, 42),
        face_reference_key(3, 42),
    }
    assert store.get(face_reference_key(2, 42)) == b"\xff\xd8\xffA"
    assert store.get(face_reference_key(3, 42)) == b"\xff\xd8\xffB"


# ======================================================================
# Per-org crm_provider selection
# ======================================================================


@pytest.fixture
def two_org_crm_world(db_session):
    """Org 1 (founding, alias DEFAULT_ORG_ALIAS) runs delphi; org 2 (rival)
    has NO org_settings row = feature OFF. One property each with a crm_ref;
    ADMIN holds org-wide org_admin in BOTH orgs so only the provider — not
    the gate — differs between them. Written by the superuser session so the
    world exists regardless of the walls."""
    ensure_default_org(db_session)  # org 1 + its org_settings (env default)
    # Org 1 explicitly runs delphi (the env default in this suite is empty).
    db_session.execute(
        update(OrgSettings).where(OrgSettings.org_id == 1)
        .values(crm_provider="delphi")
    )
    db_session.add(Organization(
        org_id=2, name="Rival Hotel Group", kc_org_alias=RIVAL_ALIAS
    ))
    db_session.flush()
    db_session.add(Property(
        property_id="L5A1", org_id=1, name="Org A Hotel",
        pms_source="opera", wage_jurisdiction="US-CA", crm_ref="REF-A",
    ))
    db_session.add(Property(
        property_id="L5B1", org_id=2, name="Org B Hotel",
        pms_source="opera", wage_jurisdiction="US-CA", crm_ref="REF-B",
    ))
    db_session.commit()
    grant_role(db_session, "org_admin", sub="l5-admin", org_id=1)
    grant_role(db_session, "org_admin", sub="l5-admin", org_id=2)


def _crm_client(db_url, tmp_path, verifier):
    """Serving app connected as the RLS-bound app role (so the DB wall is
    genuine, not superuser-bypassed) with a delphi feed for any configured
    provider."""
    # Inside the property-local today..+90d pull horizon regardless of the
    # wall clock (store_pull refuses days outside it).
    stay = datetime.now(ZoneInfo("America/Los_Angeles")).date() + timedelta(days=3)
    feed = InMemoryCrmFeed(
        days=[CrmDemandDay(stay_date=stay, rooms_on_books=100,
                           group_rooms=None, event_covers=None)],
        feed_capabilities=CrmCapabilities(emits_rooms_on_books=True),
    )
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(make_engine(app_role_url(db_url))),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        crm_feed_factory=lambda provider: feed if provider else None,
    )
    return TestClient(app)


def _admin_headers(mint, alias):
    token = mint(roles=["org_admin"], sub="l5-admin",
                 organizations=[DEFAULT_ORG_ALIAS, RIVAL_ALIAS])
    return {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: alias}


def test_demand_surface_provider_is_per_active_org(
    db_url, tmp_path, two_org_crm_world
):
    """The marquee L5 crm pin (ties L3 + L5), on the READ surface — the one
    that degrades without writing, so org B (org 2) never trips the pre-L6
    write-path wall. The SAME admin token: active in A the surface is
    configured (delphi); active in B it degrades to EXACTLY feature-off
    (configured: false, no provider) — the provider is the active org's,
    never the token's or the process's."""
    verifier, mint = make_authkit()
    client = _crm_client(db_url, tmp_path, verifier)
    window = {"start": "2026-08-01", "end": "2026-08-31"}

    on = client.get("/api/crm/demand", params={"property": "L5A1", **window},
                    headers=_admin_headers(mint, DEFAULT_ORG_ALIAS))
    assert on.status_code == 200, on.text
    assert on.json()["configured"] is True
    assert on.json()["provider"] == "delphi"

    off = client.get("/api/crm/demand", params={"property": "L5B1", **window},
                     headers=_admin_headers(mint, RIVAL_ALIAS))
    assert off.status_code == 200, off.text
    assert off.json()["configured"] is False
    assert off.json()["provider"] is None


def test_pull_uses_the_active_orgs_provider(
    db_url, tmp_path, two_org_crm_world
):
    """The write path for the founding org: active in A the pull runs under
    A's delphi provider (201, provider echoed) — the per-org provider feeds
    the PULL, not just the read surface. (Org B's pull path is the L6
    write-stamping deferral: an org != 1 session cannot yet INSERT the
    audit/batch rows, so its refusal is exercised on the read surface above.)"""
    verifier, mint = make_authkit()
    client = _crm_client(db_url, tmp_path, verifier)

    on = client.post("/api/crm/refresh", json={"property": "L5A1"},
                     headers=_admin_headers(mint, DEFAULT_ORG_ALIAS))
    assert on.status_code == 201, on.text
    assert on.json()["provider"] == "delphi"


def test_off_org_reads_no_provider_even_with_env_set(
    db_url, tmp_path, two_org_crm_world, monkeypatch
):
    """The env-fallback mutant, on the read SEAM directly: org 2 has no
    org_settings row, so `_active_org_crm_provider` returns '' (OFF) even
    when USALI_CRM_PROVIDER is set in the process — the read is the active
    org's row, NEVER env (env seeds org 1 only). Under the mutant that falls
    back to env for org != 1, org B would resolve 'delphi' and wrongly pull.
    Org 1 still reads its OWN row (delphi), proving the read is per-org, not
    a blanket ignore-env."""
    from usali.crm_api import _active_org_crm_provider

    monkeypatch.setenv("USALI_CRM_PROVIDER", "delphi")
    factory = make_session_factory(make_engine(app_role_url(db_url)))

    with factory() as s:
        bind_org_context(s, 2)
        assert _active_org_crm_provider(s) == ""  # no row, no env fallback
    with factory() as s:
        bind_org_context(s, 1)
        assert _active_org_crm_provider(s) == "delphi"  # its own row
