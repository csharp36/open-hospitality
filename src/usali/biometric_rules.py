"""Encoded biometric-processing postures, per wage jurisdiction (F1).

ONLY `US-CA` is encoded. `biometric_rules_for()` REFUSES anything else —
including bare `US`, which differs from `sick_leave_rules_for`: zero sick
accrual under plain `US` is encoded federal law, but a face template is
computed in whatever state the kiosk stands in, and not knowing that state's
consent regime is not a safe basis for biometric processing.

Every claim traces to `docs/reference/biometric-attendance.md` (researched
2026-07-21 against Civ. Code §1798.100/.140, Labor Code §1051, and the
state-by-state posture table there). The obligations differ in KIND across
states — CA is notice-at-collection; IL demands a signed release with a
private right of action behind it (Cothron v. White Castle); TX/WA/CO are
consent regimes — so an unknown state must refuse to enable the feature
rather than default to CA's (weakest) posture.
"""

from dataclasses import dataclass
from datetime import date


class UnknownBiometricJurisdiction(ValueError):
    """No encoded biometric posture for this jurisdiction. Refused rather
    than defaulted: running CA's notice flow in a consent state would be
    silently unlawful, with a private right of action behind it in IL."""


@dataclass(frozen=True)
class BiometricRules:
    jurisdiction: str
    # "notice_at_collection" (CA). A consent state would encode
    # "written_release" here and the enrollment flow would have to collect
    # one — the field exists so that difference cannot be papered over.
    consent_mode: str
    requires_written_release: bool
    # Template + reference photo deleted within this many days of
    # separation (the recorded retention posture; IL's "purpose satisfied"
    # benchmark is the strictest-plausible ceiling).
    retention_after_separation_days: int
    citation: str
    reference_doc: str
    recheck_by: date


_CALIFORNIA = BiometricRules(
    jurisdiction="US-CA",
    consent_mode="notice_at_collection",
    requires_written_release=False,
    retention_after_separation_days=30,
    citation=(
        "Cal. Civ. Code §1798.100 (notice at collection), §1798.140(c)/(ae) "
        "(biometric information as sensitive PI); Cal. Labor Code §1051"
    ),
    reference_doc="docs/reference/biometric-attendance.md",
    recheck_by=date(2026, 12, 1),
)


def biometric_rules_for(jurisdiction: str) -> BiometricRules:
    """The encoded posture, or a refusal — never a default."""
    if jurisdiction == "US-CA":
        return _CALIFORNIA
    raise UnknownBiometricJurisdiction(
        f"no encoded biometric posture for jurisdiction {jurisdiction!r}; "
        "refusing to enable face matching rather than defaulting to "
        "California's notice-mode rules. See "
        "docs/reference/biometric-attendance.md for the encoding standard."
    )
