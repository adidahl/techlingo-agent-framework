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
# 1) Create .env from the template and set your key
cp example.env .env

# 2) Run the A0–A5 workflow on the included sample input
python main.py run --input-file sample.txt --out-dir outputs
```

### Pipeline

```
A0 Content Inventory  — extracts every term/definition/example from the source (coverage checklist)
A1 Modularizer        — course map + per-lesson "content packs" (concept atoms with confusables)
A2 Scaffolder         — exercises per concept (sees the FULL source text + content packs)
A3 Scenario Designer  — rewrites higher-order questions into grounded scenarios
A4 Feedback Architect — rationales, better_fit, paired intrinsic/instructional feedback
A5 Validator          — deterministic quality gates + LLM fact-check + repair, loops back to A2 on errors
```

Question-quality rules enforced deterministically (no LLM) in `validate.py`:
- every exercise targets one `concept_id`; coverage spread evenly per lesson
- near-duplicate detection (same fact re-asked in a different wrapper = error)
- Bloom/type coupling (Applying/Analyzing ⇒ scenario choice questions; true_false/fill_gaps/rearrange ⇒ Remembering/Understanding)
- true/false answer balance, tautology and correct-option-length-bias checks
- rearrange: 4–8 shuffled tokens (never shipped pre-arranged), generic prompt stems rotated

Notes:
- With the default `openai` backend you must set both `OPENAI_API_KEY` and `OPENAI_CHAT_MODEL_ID` in `.env` (or pass `--model-id`).

### LLM backends (subscription CLIs — $0 marginal cost)

The pipeline can run its completions through three interchangeable backends
(see `SUBSCRIPTION_BACKENDS_PLAN.md`):

| Backend | Engine | Cost | Structured output |
|---|---|---|---|
| `openai` (default) | OpenAI API via agent_framework | per token | OpenAI Structured Outputs + fallback |
| `claude-code` | headless `claude -p` (Claude subscription seat) | $0 marginal | prompt-schema + pydantic repair retries |
| `codex` | `codex exec` (Codex subscription seat) | $0 marginal | real `--output-schema` from pydantic |

```bash
# Preflight: binary + version + auth per backend (add --ping for a live 1-call test)
python main.py doctor

# Run the whole pipeline on a subscription seat
python main.py run --input-file sample.txt --backend claude-code   # or: --backend codex
python main.py run --input-file sample.txt --backend claude-code --model-id opus
```

Selection and tuning via env (CLI flags win):
- `TECHLINGO_LLM_BACKEND` — `openai` | `claude-code` | `codex`
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

