import re
from datetime import date

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_amount, parse_opera_date
from usali.schemas import LedgerRecord, StagedRecord

CODE_RE = re.compile(r"^\d{3,5}$")
AMOUNT_RE = re.compile(r"^-?\$?[\d,]+\.\d{2}$")
_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")
SECTIONS = {"Revenue", "Non Revenue", "Payment"}

# The ledger reconciliation block follows the transaction sections. It starts right
# after the "Transaction Total Today" roll-up and runs to the end of the report.
_LEDGER_BLOCK_START = "Transaction Total Today"
LEDGER_HEADINGS = {"Guest Ledger", "AR Ledger", "Deposit Ledger", "Package Ledger"}
# Sanity-check prose Opera prints below the ledgers ("Guest Ledger is in balance",
# "There are no invalid transactions", ...). None of it carries an amount, but match it
# explicitly so a future layout change cannot silently stage prose as data.
_NOISE_RE = re.compile(
    r"(is in balance$|are in balance$|^There are no |^No differences found )"
)


def extract_business_date(words: list[Word]) -> date:
    for w in words:
        if _DATE_RE.match(w.text):
            return parse_opera_date(w.text)
    raise ValueError("no Opera business date (MM-DD-YY) found in report")


def parse_trial_balance(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StagedRecord]:
    records: list[StagedRecord] = []
    section: str | None = None
    for row in cluster_rows(words, y_tol):
        tokens = [w.text for w in sorted(row, key=lambda w: w.x0)]
        joined = " ".join(tokens).strip()
        if joined in SECTIONS:
            section = joined
            continue
        # A data row starts with the transaction code. The code is always the leftmost
        # token because Opera renders it in a fixed left column — we rely on column
        # position, not merely on "looks like a 3-5 digit number".
        if not tokens or not CODE_RE.match(tokens[0]):
            continue
        code, rest = tokens[0], tokens[1:]
        # Pull the trailing amount off the row. Opera renders a negative amount as a
        # separate leading "-" token when the sign + digits don't fit one word
        # (e.g. ["-", "149.75"]), so accept a lone "-" as part of the amount run.
        amount_tokens: list[str] = []
        while rest and (AMOUNT_RE.match(rest[-1]) or rest[-1] == "-"):
            amount_tokens.insert(0, rest.pop())
        if not amount_tokens:
            continue
        records.append(
            StagedRecord(
                property_id=property_id,
                pms_source="OPERA",
                report_type="trial_balance",
                business_date=business_date,
                pms_trx_code=code,
                pms_trx_desc=" ".join(rest).strip() or None,
                raw_amount=parse_amount("".join(amount_tokens)),
                section=section,
            )
        )
    return records


def parse_trial_balance_ledgers(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[LedgerRecord]:
    """Parse the ledger reconciliation block of an Opera trial balance.

    Rows are `<label> <amount>` lines: first a run of report-level summary rows (Grand
    Total, AR Ledger Payments, Guest Ledger Balance, ...), then per-ledger sub-blocks
    introduced by a bare heading row (Guest/AR/Deposit/Package Ledger). The heading is
    part of a row's identity — "Deposits Transfered at Check-In" appears under BOTH the
    Guest Ledger and the Deposit Ledger with opposite signs — so it is folded into the
    label ("Guest Ledger / Deposits Transfered at Check-In"). Page furniture (headers,
    "Filter Date: ... Page n of m") carries no trailing amount and drops out naturally;
    the sanity-check prose at the bottom is skipped explicitly.
    """
    records: list[LedgerRecord] = []
    in_block = False
    heading: str | None = None
    for row in cluster_rows(words, y_tol):
        tokens = [w.text for w in sorted(row, key=lambda w: w.x0)]
        joined = " ".join(tokens).strip()
        if not in_block:
            in_block = joined.startswith(_LEDGER_BLOCK_START)
            continue
        if joined in LEDGER_HEADINGS:
            heading = joined
            continue
        if _NOISE_RE.search(joined):
            continue
        # Pull the trailing amount off the row; Opera renders a negative as a separate
        # leading "-" token (e.g. ["-", "1,934.14"]) — same quirk as the coded rows.
        rest = list(tokens)
        if not rest or not AMOUNT_RE.match(rest[-1]):
            continue  # heading-less text without an amount: page furniture
        amount_tokens = [rest.pop()]
        if rest and rest[-1] == "-":
            rest.pop()
            amount_tokens.insert(0, "-")
        label = " ".join(rest).strip()
        if not label or CODE_RE.match(rest[0]):
            continue  # never a ledger row: blank label or a coded transaction row
        if label == "Hotel Balance":
            heading = None  # report-level summary row printed after the last sub-block
        ledger_label = f"{heading} / {label}" if heading else label
        records.append(
            LedgerRecord(
                property_id=property_id,
                pms_source="OPERA",
                report_type="trial_balance",
                business_date=business_date,
                ledger_label=ledger_label,
                kind="balance" if "Balance" in ledger_label else "activity",
                amount=parse_amount("".join(amount_tokens)),
            )
        )
    return records
