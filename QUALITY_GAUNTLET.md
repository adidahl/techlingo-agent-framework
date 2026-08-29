# Final-Artifact Quality and Gauntlet

TechLingo has two complementary QA layers:

1. deterministic selection, scheduling, emission, and validation of the exact
   ordered questions a learner will play; and
2. an optional bounded qualitative Gauntlet with an isolated critic, narrowly
   scoped editor, and blind champion/challenger comparison.

The deterministic layer is always authoritative. A critic, comparator, human
approval, or reference example cannot waive schema, answer, identity, coverage,
sequence, source/config/hash, or publication failures.

## Deterministic quality checks

Run an in-memory final-artifact audit without writing a bundle:

```bash
python main.py course quality courses/ai-901 \
  --output /tmp/ai901-quality.json
```

The concise output reports final schema/sequence errors, warnings, and declared
relaxations. The JSON file contains `sequence-quality-v1` unit metrics with exact
question paths. Warnings and declared, explained relaxations do not make the
command fail; unexplained hard-sequence or TechLingo schema/answer errors do.

Reproduce the checked AI-901 raw-versus-compiled corpus audit:

```bash
PYTHONPATH=src python scripts/ai901_sequence_audit.py --pretty
PYTHONPATH=src pytest -q tests/test_ai901_sequence_audit.py
```

The audit is read-only: it fingerprints all banks and does not write `dist/`.
It reports both original learner mechanics (`single_choice` and `multi_choice`
remain distinct) and the collapsed TechLingo UI family (`multiple_choice`),
plus raw and compiled correct-option-position distributions and streaks.

Experience policy lives under `experience` and `sequence_quality` in
`compile.yaml`. Defaults enforce maximum streaks of 2 for both the original
mechanic and the rendered UI family, a maximum identical T/F answer streak of
2, three mechanics per six-question window where applicable, and no adjacent
same-concept questions when feasible. The cumulative relaxation order is
rolling-window diversity, T/F answer, UI family, original mechanic, then
concept adjacency. It never includes correctness, identity, coverage, payload
content, answer semantics, or schema. The final hard gate verifies the
scheduler's hash-bound decision attestations and independently reproduces any
claimed unavoidable violation; a hand-written waiver is not accepted, and
search exhaustion fails rather than relaxes.

## Reference drafts and human approval

The repository currently ships no approved examples, and there are no
fabricated or implicitly approved examples. Create a candidate from an exact
compiled unit and explain why it is strong:

```bash
python main.py course reference draft courses/ai-901 \
  --unit generative-ai-and-language-models-l1 \
  --reference-id genai-foundations-v1 \
  --annotation "Varies recognition and judgment without changing terminology." \
  --annotation "Every answer is directly supported by the attached source."
```

Drafts live under `gauntlet/references/drafts/` and are never supplied to the
critic as approved standards. After a human reviews the exact draft content:

```bash
python main.py course reference promote courses/ai-901 \
  genai-foundations-v1 --approved-by "Reviewer Name" \
  --note "Reviewed against source and played end to end."

python main.py course reference list courses/ai-901
```

Promotion writes a separate approved file, retains the draft, records a
timezone-aware approval, and binds it to the reviewed draft-content SHA-256.
Editing approved content invalidates that binding.

## Optional qualitative Gauntlet

Configure `gauntlet.critic_backend` and `gauntlet.critic_model` in
`compile.yaml`. The backend must be `codex` or `claude-code`; the model may be a
backend-qualified label such as `codex:o3`. Budgets, thresholds, protected
dimensions, plateau/repeated-loss limits, and comparison seed are all in the
same block.

Selection is dry by default and makes no model calls or history writes:

```bash
python main.py course gauntlet run courses/ai-901 \
  --unit generative-ai-and-language-models-l1
```

Explicitly opt in to live subscription-CLI calls:

```bash
python main.py course gauntlet run courses/ai-901 \
  --unit generative-ai-and-language-models-l1 --execute

# Deliberately expensive: evaluates every compiled learner unit.
python main.py course gauntlet run courses/ai-901 --all --execute
```

Each critic/editor/comparator call receives a fresh client/context. The critic sees
only the goal, complete 12-dimension rubric, relevant source text, approved
references, and exact final artifact—not builder reasoning or prior hidden
context. A separate editor can change only the critic-authorized item fields or
session order. Both deterministic A/B positions must support a stable challenger
win, with no protected regression, before champion promotion. If an item or
course repair changes payload content, a second fresh critic pass must satisfy
the configured factual-fidelity and confidence thresholds before comparison;
a session-only reorder does not repeat that content check.

History is immutable and stores structured evidence and decisions, never
private chain-of-thought:

```bash
python main.py course gauntlet history list courses/ai-901
python main.py course gauntlet history list courses/ai-901 --unit generative-ai-and-language-models-l1
python main.py course gauntlet history show courses/ai-901 RUN_ID
python main.py course gauntlet history show courses/ai-901 RUN_ID --json
```

Gauntlet records do not silently rewrite canonical banks. If the winning
champion contains a content edit, review and apply it through the authored/bank
workflow, rerun deterministic validation, and evaluate that new exact artifact
when qualitative publication evidence is required. A reviewed bank edit does
not require regenerating its source with A0–A5.

### Hash-bound authored proposals

Only promoted rounds with an exact persisted edited champion, passing hard
gate, passing fresh source-fidelity gate, stable position-swapped comparison,
and an unambiguous current bank/source mapping can be exported. The list view
shows exact item-key/field before-and-after values and the bound evidence:

```bash
python main.py course gauntlet proposal list courses/ai-901
python main.py course gauntlet proposal list courses/ai-901 --json
python main.py course gauntlet proposal queue courses/ai-901
python main.py course gauntlet proposal export courses/ai-901 PROPOSAL_ID \
  --output /tmp/proposal.json
python main.py course gauntlet proposal review courses/ai-901 PROPOSAL_ID \
  --output /tmp/proposal-review.html
```

`proposal queue` is deliberately separate. It contains blocking
`unsafe_or_unbounded_repair` findings that need a human-authored redesign, not
an automatic item edit.

Export is review-only. Approval repeats four exact hashes and writes an
immutable approval artifact without changing banks:

```bash
python main.py course gauntlet proposal approve courses/ai-901 /tmp/proposal.json \
  --approved-by "Reviewer" \
  --proposal-sha256 PROPOSAL_SHA \
  --history-sha256 HISTORY_SHA \
  --compiled-artifact-sha256 COMPILED_SHA \
  --champion-after-sha256 CHALLENGER_SHA
```

Incorporation is dry unless `--execute` is explicit. Before any write it
recomputes the proposal from immutable history and the current compiled and
authored artifacts. Execution updates only the uniquely mapped authoritative
bank item, marks it `human-edited` and pinned under the regeneration contract,
preserves its identity/mechanic/concept/source provenance, and runs the
deterministic AI-901 audit/course-quality checks. It does not invoke Terra,
regenerate a source, patch a generated compiled unit, or publish a bundle.
Fresh v6 Luna evaluation is a separate explicit option and covers only the
affected unit:

```bash
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json --execute
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json \
  --execute --fresh-gauntlet
python main.py course gauntlet proposal promote courses/ai-901 APPROVAL.json \
  --amendment APPROVED_AMENDMENT.json --receipt APPLICATION_RECEIPT.json --execute
```

Any hash drift, ambiguous mapping, protected-mechanic change, unsupported
authored mapping, deterministic failure, or non-eligible fresh evaluation
fails closed and leaves publication blocked; it does not weaken any gate.
An explicitly human-approved mechanic amendment is separate evidence: focused
promotion accepts it only when it changes the approved payload by the declared
mechanic conversion, the repaired item is pinned and human-edited, the old
promoted bank can be reconstructed by reversing that one item, and the receipt
matches the freshly compiled deterministic artifact. Promotion changes only
`build_state.json`; it never changes the bank or emits a bundle.

## Validated publication

The safe publication sequence is:

```bash
python main.py course build courses/ai-901
python main.py course quality courses/ai-901 --output /tmp/quality.json
# Run required reference/Gauntlet review here when configured.
python main.py course compile courses/ai-901
```

`course compile` refuses unresolved or unknown source validation, source or
configuration drift, bank tampering, emitted schema/answer errors, unexplained
sequence errors, and—when `qualitative_required_for_publication: true`—missing
exact Gauntlet coverage. Qualitative coverage is bound to the exact unit
champion content/order, complete Gauntlet policy and rubric,
current source hashes, approved-reference hashes, and actual model/backend
roles. An unchanged unit may reuse immutable evidence reviewed under an older
course-wide artifact hash; the manifest records both the reviewed course hash
and the course hash being published. Changed unit content, context drift, or an
incoherent/replayed history still fails closed. The bundle is staged, rechecked,
and atomically promoted; an invalid challenger cannot replace the last-known-good
bundle.

## Migration and compatibility

- `compile-v1` receives additive nested policy blocks; old files load the new
  defaults unchanged.
- Banks remain `exercise-bank-v1`; experience metadata is derived rather than
  duplicated. Emitted question `options` gain additive `variant`, ownership,
  and learning-status fields.
- Choice-option presentation order can change deterministically, but complete
  option objects/correctness flags are preserved and `correct_answer` is
  re-derived and revalidated.
- Runtime composition is `session-v2`. Its mastery buckets, rung gates,
  two-items-per-concept cap, and mistake semantics are unchanged. New optional
  `PoolItem` fields activate the shared experience scheduler; legacy callers
  use `unknown` metadata safely.
- `build-state-v2` separates latest attempts from last-known-good state. Legacy
  v1 records remain readable for diagnostics, but are deliberately not accepted
  as publication evidence because they lack exact report/config/bank bindings;
  a normal validated rebuild regenerates the v2 evidence.
- Reference and Gauntlet history formats are new, versioned, optional files;
  no approved reference is assumed to exist.

## Known limitations

- TechLingo emits both single- and multi-select questions as
  `multiple_choice`. The scheduler and quality gate separately enforce the
  original interaction mechanic and this coarser rendered UI-family metric,
  and the report exposes both.
- Coverage-first module checkpoints can exceed `session_size_hint`; the hint is
  a growth target, not a cap.
- Legacy banks can exhaust unseen variants and explicitly repeat an item across
  different units, never within one unit. The report/audit exposes repeats.
- Qualitative factual fidelity remains model/human judgment where it cannot be
  mechanically proven. Scope enforcement, source-bearing comparison, protected
  dimensions, and deterministic hard gates limit—but cannot eliminate—model
  error.
- Subscription CLIs may not expose exact token or monetary usage. Backend call
  counts are always recorded; configured token/cost stops apply when adapters
  provide those fields.
