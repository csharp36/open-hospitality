# Comment claims: verify, relocate, or rephrase

**Status:** design, 2026-08-31
**Artifact:** `~/.claude/CLAUDE.md` — user-wide, not repo-local
**Origin:** the OH-17 six-lens review (PR #108), where five of eleven confirmed
findings were comments that were false rather than logic that was broken

## 1. The problem, as measured

OH-17's review confirmed eleven defects. Five were false comments. They were not
a random sample of the ~17,800 comment lines in this repo's Python — they shared
one structure:

| Comment | Asserted a fact about |
|---|---|
| "`compare_metadata`'s parity check is what fails if you edit one" | a **test** elsewhere |
| "The client never puts a response body in a QboError" | a **sibling module** |
| "A Core write bypasses the bind processor" (×3 sites) | a **library** |
| "fails two ways and only two" | a **closed set** of exceptions |
| "Returns the PAIR, never a bare adapter" | **call-site** discipline |

**Not one purely local comment was wrong.** Comments describing what the code
under them does, and why it is shaped that way, held up. Every failure was a
claim about something the author could not see from where they were standing.

That is the whole finding, and it is what makes the problem tractable: the
dangerous comments are identifiable *before* you write them, by their subject
rather than their content.

### 1.1 Why this is not a rot problem

There are two failure modes and they need different mechanisms:

- **False when written.** The `compare_metadata` claim was never true — alembic
  does not diff CHECK constraints in that configuration. The Core-write claim
  was never true either. Both were written in good faith by an author who was
  confident and did not check.
- **True, then rotted.** A cited test gets renamed; a call site drops the
  discipline a docstring promised. `_provider`'s "never a bare adapter" was
  accurate until a later call site made it false.

**CI can only address the second, and only where the citation is structured
enough to resolve.** For the first, a check encodes the same wrong belief the
comment does, and passes. This design targets the first — deliberately, because
it is both the larger share of the evidence and the class no tooling catches.

### 1.2 Why "be more careful" would not have worked

Nothing here was careless. The plaintext claim propagated to two source files,
a plan document, and a persistent memory entry because it was *believed*. Ten
seconds of `stmt.compile(...)._bind_processors` would have refuted it at any
point, and was never run, because nothing prompted the question.

A rule phrased as a disposition — "be careful about cross-boundary claims" —
changes nothing for an author who is not being careless. The rule has to name
a **trigger** and an **action**.

## 2. The invariant

**Trigger.** A comment (or docstring) asserting a fact beyond the function it
sits in: another module's behavior, a library's behavior, what a test covers,
or what callers do. The test is mechanical — *could I be wrong about something I
cannot see from here?*

**Action.** Discharge it one of three ways. Verification is the least common.

### 2.1 Rephrase to something local — the default

Most cross-boundary claims earn their place as *pointers*, not as assertions.
Name the enforcement point instead of asserting the outcome:

> ~~"The client never puts a response body in a QboError, so this cannot leak."~~
> "This detail reaches an unauthenticated caller, so what goes into a QboError
> is a security property of `qbo_client`, not a formatting choice there.
> `_unparseable` is where that is enforced."

The second cannot be false. It is also strictly more useful: it tells the reader
where to look, which the assertion did not.

### 2.2 Relocate — when the claim is really about another module

A claim about module B's behavior belongs in **B's own tests**. "The client
never leaks a body" was an intention in `integrations_api` and false in
`qbo_client`; as a test in `qbo_client`'s file it became true and stays true.
The comment then cites the test rather than restating the guarantee.

Cite the test **by name**. "There is a test for this" is unfalsifiable prose;
`test_the_models_check_is_byte_identical_to_the_migrations` is greppable, and
its absence is detectable.

### 2.3 Verify — when the claim genuinely carries weight

Run the thing that proves it, then **record the command in the comment** so the
next reader can re-run it in seconds rather than re-deriving trust:

```python
# Measured: `stmt.compile(dialect=postgresql.dialect())._bind_processors`
# shows Core insert()/update() DO apply the processor; only text() bypasses it.
```

Reserve this for claims that carry a correctness, security, or tenancy
guarantee. It is the expensive discharge and should stay uncommon.

### 2.4 Closed-set claims need closed sets in code

"Fails two ways and only two" is an enumeration in prose with nothing enforcing
it. This repo already knows the durable answer — closed sets in Python are
mirrored by literal DB CHECK constraints. `_UNREADABLE` had the claim without
the machinery, and drifted. The fix was not a better comment but a single
exception type (`MalformedCiphertext`) that closes the set **by construction**.

Where a comment enumerates a closed set, prefer making it closed in code.

## 3. What this explicitly is not

- **Not a case for fewer or shorter comments.** The 31% comment density is why a
  reviewer could *find* these: the claims were explicit enough to be falsifiable.
  A terser codebase would hold the same wrong assumptions, unstated and
  unfindable. Density is the asset; unverified assertion is the liability.
- **Not hedging.** "This may be the case" makes prose weaker without making it
  truer, and it removes the falsifiability that let the review catch these.
  Rephrase to a claim that is *locally true*, do not soften a claim that is not.
- **Not a linter.** No regex reliably distinguishes an assertion about another
  module from a description of this one.

## 4. Where it lives

`~/.claude/CLAUDE.md`, user-wide.

- **Not a skill.** Skills are invoked, which requires first thinking "this
  applies to me now" — exactly the moment of not-thinking that produced the
  bug. Wrong shape for an always-on discipline.
- **Not a hook.** A hook fires after the write, when the author is already
  committed, and regex over prose produces false positives that train the
  author to dismiss it.
- **Short, deliberately.** An always-loaded instruction competes with all other
  context. A long rule gets skimmed; dilution is the main failure mode of the
  chosen mechanism, so the entry stays near the length in §6.

Repo-local CI for §1.1's *second* class remains possible and is out of scope
here. A check that resolves `tests/…::test_…` citations would catch renamed and
deleted tests. It would **not** have caught the `compare_metadata` case, which
cited a real file and a property that never existed.

## 5. How we would know it is not working

Honest failure modes, since this governs behavior rather than code:

1. **Collapse into rephrase-only.** If every trigger discharges as §2.1 and
   §2.3 never fires, the verification habit is gone and only the prose improved.
   Signal: no "Measured:" lines appear in work that touches library behavior.
2. **Dilution.** If `CLAUDE.md` accretes unrelated rules, this one stops being
   read. Signal: cross-boundary claims reappear unverified.
3. **Over-application.** If ordinary local rationale starts carrying ceremony,
   the trigger is being read too broadly and comment quality drops.

The direct check is the next review of comparable scope: were any confirmed
findings false comments?

## 6. The installed `~/.claude/CLAUDE.md` entry

Reproduced verbatim from the live file. If the two ever disagree, the file
is the artifact and this section is the stale copy.

```markdown
## Comments that claim things

Before writing a comment or docstring that asserts a fact beyond the function
it sits in — another module's behavior, a library's behavior, what a test
covers, what callers do — ask: could I be wrong about something I cannot see
from here? If yes, discharge it one of three ways:

1. **Rephrase to something local.** Name the enforcement point ("X is where
   this is enforced"), not the guarantee ("this cannot happen"). Usually the
   best option: it cannot rot, and it tells the reader where to look.
2. **Relocate.** A claim about another module's behavior belongs in that
   module's tests. Cite the test BY NAME — a name is greppable and its absence
   is detectable; "there is a test for this" is not.
3. **Verify.** Run the thing that proves it and record the command in the
   comment. Reserve for claims carrying a correctness or security guarantee.

Where a comment enumerates a closed set ("fails two ways and only two"), prefer
making the set closed in code instead.

Do NOT respond to this by writing fewer or vaguer comments. Hedged prose is
weaker without being truer. The rule is about assertions you have not checked,
not about density.

*Origin: an eleven-defect review of the OH-17 branch in open-hospitality,
2026-08-31, where five findings were false comments and every one of them was a
cross-boundary claim. No purely local comment was wrong. Evidence and rejected
alternatives: `open-hospitality:docs/design/2026-08-31-comment-claims-invariant-design.md`.*
```
