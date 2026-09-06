"""Shape pins for the GL tables (design D-OH27.1..3, D-OH27.12).

These are deliberate literals: the migration mirrors these shapes by hand
(the b3a0integcred convention), so a drifting model shows up here first.
"""

from usali import models


def test_gl_account_is_keyed_by_org_and_code():
    pk = [c.name for c in models.GlAccount.__table__.primary_key.columns]
    assert pk == ["org_id", "account_code"]


def test_gl_account_types_are_the_closed_set():
    ck = next(
        c for c in models.GlAccount.__table__.constraints
        if c.name == "ck_gl_account_type"
    )
    for t in ("asset", "contra_asset", "liability", "equity", "income", "expense"):
        assert t in ck.sqltext.text


def test_journal_line_posting_is_debit_or_credit_and_positive():
    names = {c.name for c in models.JournalLine.__table__.constraints}
    assert "ck_journal_line_posting" in names
    assert "ck_journal_line_amount_positive" in names


def test_journal_entry_supports_composite_children():
    names = {c.name for c in models.JournalEntry.__table__.constraints}
    assert "uq_journal_entry_org" in names  # the (org_id, entry_id) target


def test_posting_ledger_grain_is_property_date_source():
    uq = next(
        c for c in models.GlPostingLedger.__table__.constraints
        if c.name == "uq_gl_posting_ledger_org_row"
    )
    assert [c.name for c in uq.columns] == [
        "org_id", "property_id", "business_date", "source_type"
    ]
