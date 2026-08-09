"""F1: the biometric-processing posture table, pinned to the reference doc.

Every claim traces to docs/reference/biometric-attendance.md (researched
2026-07-21). Unlike sick leave, there is no meaningful plain-`US` posture: a
property is physically in SOME state, and not knowing which state's consent
regime applies is not a safe basis for computing face templates — so ONLY
`US-CA` returns rules and everything else refuses, including bare `US`.
"""

from datetime import date

import pytest

from usali.biometric_rules import (
    UnknownBiometricJurisdiction,
    biometric_rules_for,
)


def test_california_is_notice_mode_not_written_release():
    """CA's model is notice at collection (Civ. Code §1798.100), NOT the
    IL/TX/WA/CO opt-in signed-release regime. Encoding the distinction is
    the whole point of the table — a consent state must never silently run
    the notice flow."""
    rules = biometric_rules_for("US-CA")
    assert rules.consent_mode == "notice_at_collection"
    assert rules.requires_written_release is False


def test_california_retention_is_separation_plus_thirty_days():
    """The recorded retention posture: template and reference photo deleted
    within 30 days of separation (reference doc checklist item 3)."""
    rules = biometric_rules_for("US-CA")
    assert rules.retention_after_separation_days == 30


def test_the_ruleset_carries_its_citation_and_recheck_date():
    """Encoded law goes stale — the CA 2025-26 session could pass a BIPA
    successor and the ADMT regs land 2027-01-01. The ruleset names the doc
    and the re-check date so a consumer can warn past it."""
    rules = biometric_rules_for("US-CA")
    assert "1798.100" in rules.citation
    assert rules.recheck_by == date(2026, 12, 1)
    assert "biometric-attendance" in rules.reference_doc


def test_every_other_jurisdiction_is_refused_including_plain_us():
    """IL needs a written release with a private right of action behind it
    (Cothron); TX/WA/CO need consent; bare `US` means we don't know which
    regime applies. Defaulting any of them to CA's (weakest) posture would
    be the exact failure the table exists to prevent."""
    for juris in ("US-IL", "US-TX", "US-WA", "US-CO", "US-NV", "US", "", "CA"):
        with pytest.raises(UnknownBiometricJurisdiction):
            biometric_rules_for(juris)
