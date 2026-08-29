# TechLingo Agent Framework

A framework for building and deploying AI agents.

## Getting Started

### Prerequisites

- Python 3.13 (recommended; pinned in `.python-version`)
  - Note: Python 3.14 is **not yet supported** — the `agent-framework` beta has no compatible wheels for it. Use 3.13.
- pip

### Installation

```bash
pip install -r requirements.txt
```

### Usage

```bash
# 1) Create .env from the template (backend selection; no API keys needed)
cp example.env .env

# 2) Run the A0–A5 workflow on the included sample input
python main.py run --input-file sample.txt --out-dir outputs
```

### Course workspaces (folder of .md files → importable course)

Phase 1 of ARCHITECTURE.md: build a whole course from ANY folder of markdown
files (1 file = 1 course module), incrementally, into a git-friendly workspace.

```bash
# 1) Create the workspace (copies sources in)
python main.py course init courses/ai-901 --from documents/ai-901 --course-key ai-901 --title "AI-901"

# 2) Build: runs the A0–A5 pipeline once per CHANGED source file only
python main.py course build courses/ai-901 --backend codex     # or claude-code (default)
python main.py course build courses/ai-901 --only "5. Introduction to computer vision concepts.md"
# Fast pipeline test: one lesson per file instead of 5-6
python main.py course build courses/ai-901 --only "5. Introduction to computer vision concepts.md" --lessons 1

# 3) Compile: deterministic, no LLM — levels + checkpoints, versioned bundle + flat course.json
python main.py course compile courses/ai-901

# 4) Inspect freshness + content counts
python main.py course status courses/ai-901
```

The workspace (`courses/<id>/`) is the canonical, git-versioned content store:
`graph/concepts.yaml` (concept graph with stable ids — mastery/telemetry key on
them), `curriculum.yaml` (modules → lessons → concept ids), `bank/*.json`
(exercise bank items addressed `concept × rung R1–R5 × variant`), and
`build_state.json` (source hashes → incremental rebuilds). `dist/<id>-v<N>/`
holds compiled bundles; `course.flat.json` inside is exactly today's importer
format. Pinned or human-edited bank items survive rebuilds (provenance
contract). See ARCHITECTURE.md §3–6.

**Publication safety.** A source is promoted into the canonical graph,
curriculum, and banks only when its hard A0–A5 validation report passes. A
failed challenger is recorded as failed, returns a non-zero build status, and
leaves the last-known-good content intact. `course compile` also refuses dirty,
failed, unvalidated, configuration-stale, or bank-tampered workspaces. Valid
bundles are written to a staging directory and atomically promoted; their
manifest and `build_state.json` record source-set, validation-report,
workflow-config, compile-config, bank, and compiled-artifact SHA-256 hashes.

**Levels & checkpoints (Phase 2a).** `compile.yaml` drives the deterministic
level compiler (ARCHITECTURE.md §5; every choice is seeded — same workspace +
same `compile.yaml` → byte-identical bundle). With `levels: 3` (the default)
each lesson emits one unit per level, playable in today's app (level = unit):

- **L1 Foundations** (`<lesson>-l1`) — rungs R1–R2 + the lesson's flashcards;
- **L2 Apply** (`-l2`) — R3–R4, plus `recycle.l2` (default 0.40) share of the
  lesson's concepts recycled with one R1/R2 item each — an unseen variant of
  the same concept×rung cell (concepts still holding unseen variants are
  recycled first; a seen repeat happens only when every pool is exhausted);
- **L3 Master** (`-l3`) — R5 plus `recycle.l3` (0.30) R3/R4 recycling.

No item ever appears twice within a unit; empty levels are skipped with a
note. `checkpoints: per_module` appends a `<module>-checkpoint` unit (1–2
items per concept, highest rung + unseen variants preferred, grown toward
`session_size_hint`), and `final_review: true` adds a course-wide Final
Review unit weighted decision > mechanism > fact when concept depth exists.
`levels: 1` keeps the Phase-1 flat shape and importer encoding (one unit per
lesson); `session-v2` intentionally changes learner order and may permute choice
presentation while preserving content and answer semantics.

**Final learner-sequence QA.** Variant selection now considers unseen status,
mechanic mix, T/F answers, and correct-option positions. A shared seeded
scheduler orders flat lessons, levels, checkpoints, final review, and runtime
practice selections without changing selected content or answers. The emitted
artifact is revalidated and ships with `quality_report.json`.

```bash
# Read-only deterministic audit; optional JSON has exact unit/item paths.
python main.py course quality courses/ai-901 --output /tmp/ai901-quality.json

# Reproduce the checked corpus before/after audit without writing a bundle.
PYTHONPATH=src python scripts/ai901_sequence_audit.py --pretty
```

**Optional qualitative Gauntlet.** Human-curated reference drafts, explicit
hash-bound approval, an isolated 12-dimension critic, narrow editor, inverse
blind A/B comparison, champion retention, budgets, and immutable history are
available after deterministic QA. Live calls require explicit `--execute`;
edited champions are recorded as proposals and never silently written to
canonical banks.

```bash
python main.py course reference draft courses/ai-901 --unit UNIT \
  --reference-id REF --annotation "Why this exact session is strong"
python main.py course reference promote courses/ai-901 REF --approved-by "Reviewer"
python main.py course gauntlet run courses/ai-901 --unit UNIT          # dry run
python main.py course gauntlet run courses/ai-901 --unit UNIT --execute
python main.py course gauntlet history list courses/ai-901
python main.py course gauntlet proposal list courses/ai-901
python main.py course gauntlet proposal queue courses/ai-901
```

Promoted edited champions remain proposals. A readable HTML page shows the
current and proposed question side by side. The machine-readable export and
approval bind the exact proposal, history, compiled-artifact, and proposed-
champion SHA-256 values. Incorporation is a separate `--execute` step that
updates only the uniquely mapped authoritative bank item, marks it human-edited
and pinned, and runs the read-only deterministic audit. It never regenerates a
source, patches a compiled unit, or publishes a bundle. Fresh Luna review of
only the affected unit is separately explicit with `--fresh-gauntlet`.

```bash
python main.py course gauntlet proposal export courses/ai-901 PROPOSAL_ID \
  --output /tmp/proposal.json
python main.py course gauntlet proposal review courses/ai-901 PROPOSAL_ID \
  --output /tmp/proposal-review.html
python main.py course gauntlet proposal approve courses/ai-901 /tmp/proposal.json \
  --approved-by "Reviewer" \
  --proposal-sha256 PROPOSAL_SHA \
  --history-sha256 HISTORY_SHA \
  --compiled-artifact-sha256 COMPILED_SHA \
  --champion-after-sha256 CHALLENGER_SHA
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json --execute
python main.py course gauntlet proposal incorporate courses/ai-901 APPROVAL.json \
  --execute --fresh-gauntlet
python main.py course gauntlet proposal promote courses/ai-901 APPROVAL.json \
  --amendment APPROVED_AMENDMENT.json --receipt APPLICATION_RECEIPT.json --execute
```

`proposal promote` does not regenerate content or create a bundle. It proves
that the reviewed item is the sole delta from the previous promoted bank,
rechecks the exact source, approval, amendment, receipt, and deterministic
artifact hashes, then records that evidence in publication state. Receipt
tampering or any unrelated bank drift closes the publication gate.

See [QUALITY_GAUNTLET.md](QUALITY_GAUNTLET.md) for configuration, publication,
reference approval, history review, migration details, and known limitations.

**Cell-quota generation with variants (Phase 2b).** A1 classifies every
concept atom by `depth` — `fact` | `mechanism` | `decision` — and each lesson
is generated against a deterministic **cell worksheet** expanded from the
quota table (`worksheet.py`, tunable): fact → R1×2 R2×2 R3×1 (5 items),
mechanism → +R3×2 R4×1 (7), decision → R1×1 R2×2 R3×2 R4×2 R5×2 (9). Each
worksheet row dictates one exercise's concept, question type, Bloom level and
(for true/false) the correct answer; variants of the same concept×rung cell
may test the same fact but must differ in surface (different scenario, angle,
distractor subset, gap, or statement). For a depth-classified lesson, the
selected worksheet determines the exact exercise count and its type/Bloom
distributions. `exercises_per_lesson` remains the legacy fallback for lessons
without a classified pack and the A1 concepts-per-lesson sizing hint. By
default every 5/7/9 quota row is generated. A course may set
`workflow.worksheet_items_per_lesson` to an exact pre-generation budget: every
required concept/rung row is retained, optional variants are deterministically
apportioned across learner bands and concepts, and an infeasible pack fails A1
instead of being padded or trimmed. AI-901 uses this policy to keep exactly 30
active generated items per lesson. The bank's rung is assigned by the selected
worksheet at generation time and persisted on each item (`derive_rung()`
remains the fallback for legacy payloads).

> **Build time:** oversampling variants makes a full per-file build take
> **~1.5–2× longer** than the Phase-1 numbers (a file that took ~30 min on
> `codex` is now ~45–60 min; `claude-code` proportionally more). Overnight
> batch territory — incremental builds and `--lessons 1` keep iteration cheap.

### Pipeline

```
A0 Content Inventory  — extracts every term/definition/example from the source (coverage checklist)
A1 Modularizer        — course map + per-lesson "content packs" (concept atoms with confusables)
A2 Scaffolder         — exercises per concept (sees the FULL source text + content packs)
A3 Scenario Designer  — rewrites higher-order questions into grounded scenarios
A4 Feedback Architect — rationales, better_fit, paired intrinsic/instructional feedback
A5 Validator          — deterministic quality gates + LLM fact-check + repair; routes map errors to A1 and lesson errors to A2
```

Question-quality rules enforced deterministically (no LLM) in `validate.py`:
- every exercise targets one `concept_id`; worksheet lessons get exact cell
  accounting — **ladder completeness** (every concept covered at each of its
  depth-required rungs = error) and per-cell variant quotas; legacy lessons
  keep the coverage floor / over-drill cap
- near-duplicate detection (same fact re-asked in a different wrapper = error);
  variant-aware tier: items in the SAME concept×rung cell may share the fact
  but must differ in surface (prompt-surface Jaccard < 0.7)
- confusable reciprocity: a decision-depth concept with zero `confusable_with`
  siblings is flagged (weak distractor / rejected_answers pool)
- Bloom/type coupling (Applying/Analyzing ⇒ scenario choice questions; true_false/fill_gaps/rearrange ⇒ Remembering/Understanding)
- true/false answer balance, tautology and correct-option-length-bias checks
- rearrange: 4–8 shuffled tokens (never shipped pre-arranged), generic prompt stems rotated

Notes:
- The OpenAI API backend was removed (2026-07-16) — the pipeline runs exclusively on the subscription CLI backends below.

### LLM backends (subscription CLIs — $0 marginal cost)

The pipeline runs its completions through two interchangeable subscription-CLI
backends (see `SUBSCRIPTION_BACKENDS_PLAN.md`; the OpenAI API path was removed 2026-07-16):

| Backend | Engine | Cost | Structured output |
|---|---|---|---|
| `claude-code` (default) | headless `claude -p` (Claude subscription seat) | $0 marginal | prompt-schema + pydantic repair retries |
| `codex` | `codex exec` (Codex subscription seat) | $0 marginal | real `--output-schema` from pydantic |

```bash
# Preflight: binary + version + auth per backend (add --ping for a live 1-call test)
python main.py doctor

# Run the whole pipeline on a subscription seat
python main.py run --input-file sample.txt --backend claude-code   # or: --backend codex
python main.py run --input-file sample.txt --backend claude-code --model-id opus
```

Selection and tuning via env (CLI flags win):
- `TECHLINGO_LLM_BACKEND` — `claude-code` | `codex`
- `CLAUDE_CODE_MODEL` (default `sonnet`) / `CODEX_MODEL` (default: CLI default)
- `CLAUDE_CODE_EFFORT` — thinking budget per call (`low`/`medium`/`high`; unset = CLI default). Complex lesson calls think for minutes at the default — `medium` is the speed lever.
- `TECHLINGO_MAX_CONCURRENCY` — parallel per-lesson calls (default 4; use 2–3 on subscription seats)
- `TECHLINGO_LLM_TIMEOUT_S` — per-completion timeout (default 1200; one A2/A4 lesson call through `claude -p` can need >10 min under concurrency)

`PipelineState.model_id` records the backend-qualified label (e.g.
`claude-code:sonnet`) in every artifact, so runs stay comparable across backends.

### Configuration

You can customize the course structure (number of modules, lessons, exercises, etc.) by modifying the `workflow_config.json` file in the root directory.

To use a different configuration file:

```bash
python main.py run --input-file sample.txt --config my_config.json
```

Default configuration (`workflow_config.json`):
```json
{
  "modules_count": 6,
  "min_lessons_total": 20,
  "max_lessons_total": 25,
  "exercises_per_lesson": 8,
  ...
}
```

### Editing questions after a run (web)

Open **Run Viewer → Browse Course** in the web app: every question has
**✏️ Edit** (form per question type) and **🪄 Regenerate** (one LLM call on the
default backend, with an optional note on what should be better). Both go
through `PUT/POST /api/runs/{run_id}/exercise[...]` on the FastAPI server, which
edits `course.internal.json`, **re-emits** the TechLingo-native `course.json`,
and re-runs the deterministic quality gates — the result banner tells you
immediately if an edit broke an invariant. Never edit `course.json` by hand.

Guardrails: question_type can't change, regeneration pins blooms/concept_id,
true/false answers keep the course-wide balance, rearrange word banks are
re-shuffled automatically.

### Outputs
Each run writes a folder under `outputs/run-YYYYMMDD-HHMMSS/` containing:
- `course.json` (final structured output)
- `course.md` (human-readable outline)
- `validation_report.json` (constraint checks)
- `artifacts/` (A1–A5 intermediate JSON)

## Simple UI (browse + quiz)

```bash
streamlit run ui/app.py
```

## Development

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run tests
pytest
```

## License

MIT
