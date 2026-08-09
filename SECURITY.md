# Security Policy

Open Hospitality handles compensation, PII, and multi-tenant financial data. We take
vulnerabilities seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Do not open a public issue, pull request, or discussion for a security problem.**

Report privately through GitHub's **[Private Vulnerability Reporting](../../security/advisories/new)**
(Security tab → Report a vulnerability). This is the only reporting channel —
please do not use email, public issues, or discussions for a security problem.

Please include:

- what you found and where (endpoint, file, or flow),
- how to reproduce it,
- the impact you believe it has (e.g. cross-tenant read, PII exposure, privilege
  escalation),
- any suggested remediation.

## What to expect

- **Acknowledgement** within 3 business days.
- An initial assessment and severity within 10 business days.
- We'll keep you updated as we work a fix, and credit you in the advisory once it ships
  (unless you'd prefer to remain anonymous).

Please give us a reasonable window to remediate before any public disclosure.

## Scope — what we care about most

- **Cross-tenant / cross-property data exposure** — the row-level-security wall is a core
  guarantee.
- **PII vault** — anything that would expose SSN, bank, or tax data the server is
  designed never to hold in plaintext.
- **Compensation disclosure** — leaks of pay rate or per-person earnings past their
  intended gate, including via differencing (subtracting two legal reads).
- **Authentication / authorization** bypasses, privilege escalation, scope confusion.

## Out of scope

- Findings only reproducible against the fictitious demo data.
- Reports from automated scanners without a demonstrated, reproducible impact.
- Denial of service via unrealistic request volumes.
