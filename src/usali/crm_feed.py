"""The CRM demand port (Pillar J): read-only, inbound, forward-looking.

The port wants exactly ONE thing from a hotel sales CRM — forward-looking
demand: group blocks, booking pace, event covers — normalized to per-day
figures the scheduler can surface beside the GM's forecast. It is not a
CRM: no writes, no contacts, no money. Adapters map one provider's real
API onto this shape and are the ONLY code that sees provider wire
payloads; everything they do not explicitly map is dropped unread
(plan decision 5 — enforced in the adapters, J3).

Each demand figure is `int | None`, where None means "this provider does
not speak that dimension" — never zero. Absence of a dimension is not
zero demand (the D2 forecast-null rule, applied to the feed), and
`CrmCapabilities` declares which dimensions an adapter emits so surfaces
can label honestly instead of rendering a silent 0.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


class CrmFeedError(Exception):
    """A CRM call failed. Messages must NEVER include response payloads —
    CRM bodies carry contacts and revenue this system refuses to hold."""


# Field-name substrings this system refuses to ever ALLOWLIST (matched
# case-insensitively). This is the guard against allowlist widening, the
# roster-importer posture (`roster_seed._FORBIDDEN_COLUMNS`) applied to
# CRM payloads: contacts above all — a contact name/email/phone is always
# somebody's PII — and every revenue/rate field, which this product
# refuses to hold (plan decision 7). `take_allowlisted` refuses an
# allowlist matching any of these, so "just add Contact to the allowlist"
# cannot pass tests. NOT a blocklist of wire fields: unlisted fields are
# dropped by the allowlist regardless; these patterns police the
# allowlists themselves.
SENSITIVE_FIELD_PATTERNS: tuple[str, ...] = (
    "contact", "email", "phone", "address",
    "rate", "total", "revenue", "price", "amount", "fee", "deposit",
)

# The closed set of demand providers (Pillar J / L5, decision 5). The
# EMPTY string is the OFF sentinel, NOT a provider — the full set config
# may legally hold is ('',) + CRM_PROVIDERS. It is read by
# `mapping.property_registry` (which validates the SEED value of
# USALI_CRM_PROVIDER) and by the connect surfaces; OH-17 removed its two
# former readers in `server` — the create_app fail-fast and
# `_crm_feed_for_provider` — because the provider is no longer a
# process-wide string but a column on the tenant's credential row.
# `integrations.PROVIDERS` is now the app layer's working set, and it is
# where "add a provider" starts. The
# `ck_org_integration_credential_provider_fields` CHECK
# (models.OrgIntegrationCredential and the b3a0integcred migration) is the
# SCHEMA MIRROR of the same set — kept literal on purpose so the DB refuses
# an unknown value independently of the app import. It mirrors more than the
# names: OH-17 folded the demand provider into the credential row, so the
# CHECK also pins WHICH secret column each provider must carry.
CRM_PROVIDERS: tuple[str, ...] = ("delphi", "tripleseat")

# Labels (block/event names) are bounded operator display text, the
# `availability_note` class: bounded at the boundary, scheduler-surface
# only, never logged.
LABEL_MAX_CHARS = 80


def bound_label(raw: str) -> str:
    return raw[:LABEL_MAX_CHARS]


# What a wire FIELD NAME may look like to be reported verbatim in the
# dropped report. The report is names-only — but a name is attacker text
# too (a hostile CRM can use a contact string as a JSON key, and the
# report rides out through the refresh receipt and the demo's stdout
# note). Anything not shaped like a schema field name is aggregated
# under one sentinel instead (J7 disclosure finding).
_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_UNNAMEABLE = "<non-identifier field>"


def take_allowlisted(
    payload: Mapping[str, Any],
    allow: frozenset[str],
    dropped: dict[str, int],
) -> dict[str, Any]:
    """The ONE allowlist filter both adapters route every wire object
    through (plan decision 5). Exactly the allowlisted keys are read;
    every other field's VALUE is never bound to a variable — only its
    NAME is counted into `dropped`, which becomes the pull report.

    A non-mapping payload refuses as provider misbehavior (CrmFeedError):
    iterating a string here would walk attacker text character-by-
    character into the dropped report. A sensitive ALLOWLIST refuses as a
    programming error (ValueError) — that one must fail tests, not be
    audited away at runtime."""
    if not isinstance(payload, Mapping):
        raise CrmFeedError(
            "crm wire payload is not an object where one was expected"
        )
    for name in allow:
        lowered = name.lower()
        for pattern in SENSITIVE_FIELD_PATTERNS:
            if pattern in lowered:
                raise ValueError(
                    f"allowlist entry {name!r} matches sensitive field "
                    f"pattern {pattern!r}; contacts and revenue are "
                    "dropped unread, never mapped"
                )
    kept: dict[str, Any] = {}
    for key in payload:
        if key in allow:
            kept[key] = payload[key]
        else:
            name = (
                key if isinstance(key, str) and _FIELD_NAME_RE.fullmatch(key)
                else _UNNAMEABLE
            )
            dropped[name] = dropped.get(name, 0) + 1
    return kept


@dataclass(frozen=True)
class CrmCapabilities:
    """Which demand dimensions an adapter emits. All default False: an
    undeclared dimension is ABSENT (the G1 posture — absence degrades to
    an honest gap in the surface, never to a silent zero)."""

    emits_rooms_on_books: bool = False
    emits_group_rooms: bool = False
    emits_event_covers: bool = False


@dataclass(frozen=True)
class CrmDemandDay:
    """One stay-date's normalized demand. `labels` are bounded operator
    labels (a block or event name) — scheduler-surface working data,
    never logged, never on the kiosk (plan decision 5)."""

    stay_date: date
    rooms_on_books: int | None
    group_rooms: int | None
    event_covers: int | None
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Negative demand is nonsense at the boundary — refuse loudly.
        # Clamping would silently rewrite provider data (the silent
        # direction); None stays legal (absent dimension).
        for name in ("rooms_on_books", "group_rooms", "event_covers"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(
                    f"{name} cannot be negative on {self.stay_date}: "
                    f"{value}"
                )


@dataclass(frozen=True)
class CrmDemandPull:
    """One fetch's result: the normalized days plus the dropped-field
    report — every wire field the allowlist refused, counted BY NAME
    (values never read). The report is part of the fetch contract
    (plan decision 5) so the J4 pull endpoint can return it."""

    days: tuple[CrmDemandDay, ...]
    dropped_fields: dict[str, int] = field(default_factory=dict)


class CrmFeed(Protocol):
    def capabilities(self) -> CrmCapabilities: ...

    def verify(self, external_ref: str) -> None:
        """Prove these credentials authenticate against one property's feed
        (OH-17, D-OH17.8). Raises CrmFeedError on failure.

        Exists because the connect endpoint must refuse a key that cannot
        authenticate BEFORE storing it — otherwise a typo'd key becomes a
        checklist item reading `done` over an integration that 502s on the
        first pull. `capabilities()` cannot serve as that proof: it is a
        purely local declaration that touches no network.

        Needs a ref because every real CRM read is property-scoped — there is
        no account-level ping to call instead, and a key that authenticates
        against an account the tenant's property does not live in is still a
        broken connection. Read-only BY CONTRACT (this port never writes at
        all; the same posture as `fetch_demand`)."""
        ...


    def fetch_demand(
        self, external_ref: str, start: date, end: date
    ) -> CrmDemandPull:
        """Demand days for one property over [start, end], normalized,
        with the dropped-field report. `external_ref` is the
        provider-side property identity from the mapping registry
        (`crm_ref`). Raises CrmFeedError on provider failure; an unknown
        ref is a CrmFeedError naming the REF, never the response body."""
        ...


@dataclass
class InMemoryCrmFeed:
    """Test fake: returns scripted days and records every fetch, so
    endpoint tests can assert exactly what was asked of the provider."""

    days: list[CrmDemandDay] = field(default_factory=list)
    feed_capabilities: CrmCapabilities = field(default_factory=CrmCapabilities)
    dropped_fields: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, date, date]] = field(default_factory=list)

    def capabilities(self) -> CrmCapabilities:
        return self.feed_capabilities

    def verify(self, external_ref: str) -> None:
        # The fake authenticates against nothing, so there is nothing to
        # prove — and deliberately NOT recorded in `calls`, which endpoint
        # tests read as "what demand was pulled".
        return None

    def fetch_demand(
        self, external_ref: str, start: date, end: date
    ) -> CrmDemandPull:
        self.calls.append((external_ref, start, end))
        return CrmDemandPull(
            days=tuple(self.days), dropped_fields=dict(self.dropped_fields)
        )
