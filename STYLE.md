# Comment & Documentation Style Guide

A language-agnostic guide for writing comments and docs: **terse and
rationale-driven in code, structured and generous in prose docs.**

## Overarching voice

- **Explain *why*, not *what*.** The strongest comment justifies a decision,
  defends a safety property, or flags a workaround. A comment that merely
  restates the code earns its place only in the exceptions noted below.
- **Anchor every comment to something off-screen.** Good comments supply
  context the reader *cannot* see from the code: an external tool's behavior,
  a version quirk, an upstream bug, a cross-file coupling, or an invariant a
  caller depends on. If the point is inferable from the line itself, cut it.
- **Invariants, not observations.** A comment must stay true without
  maintenance. Cite contracts, versions, limits, and upstream bugs — not the
  current state of the environment. If a fact can change with no edit to this
  file (inventory counts, current footprint, benchmark numbers, anything
  "as of \<date\>"), it belongs in the commit message or a dated report, not
  in a comment.
- **The consequence is the emphasis.** Never grade your own comment: cut
  "note that", "important:", "crucial", "load-bearing", "be careful". State
  what breaks and how; the failure mode carries the alarm.
- **Cite your sources.** When a comment describes a workaround or a surprising
  behavior, link the issue/ticket by full URL and, for version-dependent
  behavior, name the version (`built-in since <tool> X.Y+`). A citation the
  reader cannot follow ("see discussion notes") is not a citation.
- **Match the local convention.** Follow the comment density and documentation
  style of the file and module you're editing. Don't introduce a doc style a
  codebase doesn't already use; a "correct" style imposed on unfamiliar code is
  still noise.
- **Aim low on density.** Roughly one comment per few dozen lines is healthy; a
  densely commented file is usually a smell that the code should be clearer or
  the comments should be fewer.
- **Be terse in code, thorough in prose docs.** Inline comments are one line
  (a short block only for a correctness/safety rationale). Long-form
  explanation, tables, and callouts live in the README/docs, not in source.

## Comments in code (all languages)

**File header** — add one only when the file's purpose isn't inferable from its
name and location, or when tooling consumes it. When you do, keep it to 1–2
lines: what it is, what it provides.

**Function/unit comments** — present only on non-obvious units; omit on
self-evident ones. Two cases justify one:

- *Rationale / safety block* — a short paragraph defending a decision,
  especially a correctness or security one, including the invariant it depends
  on ("this protection requires that X is never done").
- *Contract declaration* — when a unit mutates shared state or has a
  non-obvious return/side effect, state it explicitly ("Sets globals: …",
  "Returns empty string if …").

**Inline comments** — one line, explaining a non-obvious mechanic or an
external-system quirk. Prefer these over narration.

**Document a convention once.** A cross-cutting convention (a key-naming
scheme, a shared placeholder value, an ordering rule) gets one authoritative
comment where the convention is defined; use sites carry at most a short
pointer to it. The same sentence pasted at every use site will drift. Name an
external standard (OTel, an RFC, a spec) only in that one place — where the
codebase's adherence to it is declared — never per-field.

**Step markers are the exception, not the rule.** A `# label` for a block is
justified only when it names an *ordering or sequence* the reader must respect
(`# Deletion order: stop, remove container, then volumes`) — not when it
narrates what the next line obviously does.

**Delete, don't disable.** Remove commented-out code; version control
remembers it. A block of dead code behind `#` is a maintenance liability, not
a safety net.

**TODO/FIXME must carry an owner or issue link** — `TODO(#123):` or
`FIXME(@name):`. An unattributed TODO is a comment nobody owns and nobody will
remove.

**Keep comments in sync with the code.** When you change code, update or delete
the comments that describe it. A wrong comment is worse than no comment.

## What to do instead of commenting

Most of the urge to comment is better spent making the code self-explanatory:

- Extract a named function or constant so the name carries the meaning.
- Turn a condition that needs explaining into a guard clause with a
  well-named predicate.
- Put the context a *person* needs at runtime into the error/log message, not
  a comment.

## Docstrings / API documentation

- **Module/file docstring:** add one when the module's purpose isn't obvious
  from its name; a one-line summary usually suffices. Extend only when behavior
  is non-obvious — avoid a bulleted narration of everything the module does, as
  it goes stale the moment the module grows.
- **Public functions get a docstring;** trivial private helpers do not. Simple
  functions get an imperative one-liner (`Configure X with Y enabled.`).
  Non-obvious ones extend with paragraphs that explain *expected-but-surprising*
  runtime behavior (why a timeout is fine, why an error is swallowed) and link
  the upstream issue.
- Document raised errors and non-obvious return contracts.
- **Don't** paraphrase the signature or type annotations, and don't repeat the
  module docstring in a class docstring (or vice versa) — say each thing in
  one place.

## Runtime messages (logs / errors / CLI output)

- Identify the source with a consistent tag (`[component] …`) when the logging
  infrastructure doesn't already inject it.
- Make them **self-contained** — readable without the source open. On failure,
  state both the cause (include the exception) and the consequence
  (`… — falling back to defaults`).
- Spend the verbosity budget here, not in source comments: context a person
  needs at runtime belongs in the message.

## Config / data files without comment support (e.g. strict JSON)

Convey intent structurally: declare a schema reference, use descriptive keys,
and document the *why* of individual settings in the accompanying docs — not in
the file.

## Build / infrastructure files (Dockerfiles, CI, manifests)

- Header comment naming the file and the *why* of a key choice (base image,
  runtime).
- Add a group comment only when the grouping or ordering isn't evident, or to
  note a non-local fact ("base image already includes git, curl, …"). A header
  that just restates the command below it ("Install dependencies") is noise.
- **Categorizing a long list** (packages, dependencies) with per-item
  sub-comments *is* idiomatic here — this is one place dense labeling is
  welcome, because a long flat list has no self-description.
- Justify tool/source choices inline ("from releases, newer than the distro
  package").
- Keep machine-readable annotations (bot markers, lint directives) immediately
  adjacent to what they govern; treat them as couplings, not decoration.

## Prose docs (READMEs, guides)

Precise, candid, peer-to-peer — subject, as always, to the project's existing
conventions.

- Use domain terms precisely.
- Prefer concrete, active verbs over abstract nouns — a thing "caches,"
  "retries," or "breaks," rather than "provides caching for."
- Quantify instead of hyping: "5–10× faster," "200k lines," not "much faster"
  or "powerful." Prefer durable quantities (limits, complexity, versioned
  benchmarks) over point-in-time inventory that ages silently.
- Keep informality sparse and deliberate. A contraction or a wry aside is fine;
  filler adverbs ("simply," "just," "very") and marketing superlatives are not.
- Put the caveat in the same breath as the claim ("X handles Y automatically,
  but Z still needs manual cleanup").
- Use the imperative for instructions ("Run …", "Verify …") and conditional
  framing for guidance ("Best for …", "If your project needs …") — help the
  reader match their situation rather than prescribing one path.
- Keep paragraphs short — one idea each, topic first. Order by progressive
  disclosure: what it is and why before how; the common path before the
  advanced one; caveats after the reader knows what they're looking at.
- Surface trade-offs and limitations candidly. Stating plainly what a thing
  *doesn't* do builds more trust than reassurance does.
- Reserve warning callouts (`> **Warning:**`, `> **Security note:**`) for things
  that weaken a guarantee; don't spend them on ordinary tips.
- Use tables, collapsible sections, and annotated code samples to keep the main
  path short; long-form explanation lives here, not in source.
- Write for a competent peer who makes their own decisions, not a novice to be
  shielded. Present the trade-off and let them choose; prescribe one path only
  when there genuinely is one.
- Calibrate assumed knowledge to your readers: assume fluency in the tools and
  concepts central to the project, and explain only what's genuinely
  project-specific or surprising.
- Keep the register calm and authoritative — no exclamation-point enthusiasm,
  no hedging weasel words. Precision is the tone.

## The litmus test

Before writing any comment, ask: *does it carry off-screen context — a
rationale, an upstream bug, a version quirk, an external-system fact, or a
cross-file coupling — that will still be true in a year?* If not — it narrates
the code, or it snapshots the environment — cut it, **except** in a build-file
dependency list or an annotated doc sample, where per-item labeling is the
house style. If yes, it is a *candidate*, not an automatic keeper: the density
budget still applies, so write only the comments with the highest
cost-of-not-knowing, and cite the source.
