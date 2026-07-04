# Subscription Backends Plan — Claude Code & Codex CLI as LLM engines

**Status:** planned (next session) · **Author:** 2026-07-04 session
**Goal:** run the whole A0–A5 pipeline through the company-paid **Claude Enterprise**
and **Codex Pro** subscriptions instead of the pay-per-token OpenAI API
(~$5/build today → $0 marginal). Both backends selectable per run; OpenAI API
path kept for comparison.

---

## 0. Verified facts (checked on this machine, 2026-07-04)

| | Claude Code | Codex CLI |
|---|---|---|
| Binary | `/opt/homebrew/bin/claude` v2.1.186 | `/opt/homebrew/bin/codex` v0.133.0 |
| Auth | ✅ logged in, `authMethod: claude.ai` (subscription, `apiProvider: firstParty`) | ✅ "Logged in using ChatGPT" (subscription) |
| Headless mode | `claude -p` (`--print`) | `codex exec [PROMPT]` |
| Prompt via stdin | yes (`-p` reads stdin) | yes (`-` or piped stdin) |
| System prompt | `--system-prompt <str>` | no flag → prepend to prompt |
| Model select | `--model <name>` (e.g. `sonnet`, `opus`) | `-m/--model <name>` |
| Structured output | ❌ no schema flag → rely on our layer-2 (prompt schema + pydantic retry) | ✅ `--output-schema <FILE>` (JSON Schema for final response!) |
| Clean result extraction | `--output-format json` → parse `.result` | `-o/--output-last-message <FILE>` (no JSONL parsing needed) |
| Batch hygiene | stateless per call | `--ephemeral` (no session files), `-s read-only`, `-C <dir>`, `--skip-git-repo-check` |

Both CLIs bill against the subscription seat, **not** an API key. This is the
supported "headless/exec" mode of each product.

Why this fits our pipeline especially well: the **chunked architecture**
(RESILIENCE_PLAN §11) already bounds every completion to one lesson, and
`LLMClient` already has JSON-extraction + pydantic-validation + repair-retry
layers built for backends without structured-output guarantees.

---

## 1. Architecture: pluggable completion backends

Refactor `llm.py` so `LLMClient` keeps ALL its logic (JSON extraction,
schema-error feedback retries, `run_json` / `run_json_model`) and delegates only
the raw completion to a backend:

```python
# src/techlingo_workflow/backends.py
class CompletionBackend(Protocol):
    name: str                      # "openai" | "claude-code" | "codex"
    model_label: str               # for logs/artifacts, e.g. "claude-code:sonnet"
    async def complete(
        self, prompt: str, *,
        system: str,
        response_model: type[BaseModel] | None,   # backends may use or ignore
        timeout_s: float,
    ) -> str: ...
```

Three implementations:

1. **`OpenAIBackend`** — wraps the current `agent_framework` ChatAgent path,
   including the Structured-Outputs attempt + fallback. Zero behavior change.
2. **`ClaudeCodeBackend`** — subprocess:
   ```
   claude -p --output-format json --model <model> --system-prompt <SYSTEM>
   ```
   - prompt piped via **stdin** (prompts carry the source text, 20–60 KB — argv
     is fragile);
   - parse stdout JSON → take `.result` (check `.is_error`); then LLMClient's
     `_extract_json` does the rest;
   - `response_model` ignored (layer-2 handles validation);
   - generation-only prompts don't invoke tools in `-p` mode; verify during
     implementation whether an explicit tool-disable flag is needed
     (`--disallowedTools`), and add it if so.
3. **`CodexBackend`** — subprocess:
   ```
   codex exec - -m <model> -s read-only -C <empty tmp dir> --skip-git-repo-check \
     --ephemeral -o <tmp result file> [--output-schema <tmp schema file>]
   ```
   - prompt (system prepended) via stdin;
   - when `response_model` is given, dump `model_json_schema()` to a temp file
     and pass `--output-schema` — **this is real structured output** and our
     schemas are already OpenAI-strict-compatible (the anyOf fix, RESILIENCE_PLAN §9);
   - read the final message from the `-o` file — no JSONL parsing;
   - `-C` MUST point to an empty scratch dir so the agent can't wander into the
     repo; read-only sandbox as a second fence; prompt begins with "Do not run
     commands or read files; answer directly."

`LLMClient.__init__` gains `backend: CompletionBackend` (default built from
config); its retry loops stay byte-for-byte identical.

## 2. Selection & configuration

- Env var **`TECHLINGO_LLM_BACKEND`** = `openai` (default for now) | `claude-code` | `codex`
- CLI flag **`--backend`** on `main.py run` / `analyze` (overrides env)
- Model resolution per backend:
  - `openai` → `OPENAI_CHAT_MODEL_ID` / `--model-id` (unchanged)
  - `claude-code` → `--model-id` or `CLAUDE_CODE_MODEL` or CLI default (recommend `sonnet`; try `opus` for A-quality comparison)
  - `codex` → `--model-id` or `CODEX_MODEL` or CLI default
- `PipelineState.model_id` becomes the backend-qualified label
  (`"claude-code:sonnet"`) so every artifact records what produced it.
- `MAX_CONCURRENT_LESSON_CALLS` becomes env-tunable
  (`TECHLINGO_MAX_CONCURRENCY`, default 4; recommend 2–3 for subscription
  backends to be gentle on seat rate windows).

## 3. Error handling & rate limits (subscription semantics)

- Both seats meter usage in rolling windows (Claude: 5-hour windows; Codex:
  plan limits). A 90-question build ≈ **26 calls** (1×A0, 1–3×A1, 6×A2, 6×A3,
  6×A4, 1×fidelity, 0–6 repairs) — comfortably within limits.
- Detect limit/auth errors distinctly:
  - claude: `.is_error` / stderr containing "usage limit" / nonzero exit;
  - codex: nonzero exit / stderr "usage limit" / login expiry.
- On rate-limit: exponential backoff (30s → 90s), max 2 retries, then a clear
  actionable message ("seat window exhausted, resets at …") instead of a stack
  trace. Auth errors → "run `claude` / `codex login`".
- Per-call timeout (default 300 s) via `asyncio.wait_for` + process kill.

## 4. Preflight: `python main.py doctor`

New CLI command that checks, per backend:
- binary present + version printed (tested versions: claude 2.1.186, codex 0.133.0 — warn on drift);
- auth status (`claude auth status` JSON; `codex login status`);
- optional `--ping` flag: one tiny completion per backend to prove end-to-end.
`run --backend X` executes the same preflight (without ping) before starting.

## 5. Testing strategy

1. **Unit (no cost):** `FakeBackend` returning canned strings → all existing
   LLMClient retry/extraction tests re-target the interface; command-builder
   unit tests for both subprocess backends (argv, stdin, schema temp files);
   parsers tested against captured real outputs (fixtures).
2. **Smoke (cents of seat quota):** `doctor --ping`.
3. **E2E comparison (the point of it all):**
   ```
   python main.py run --input-file sample.txt --backend claude-code
   python main.py run --input-file sample.txt --backend codex
   ```
   Compare: validation report (ok/errors/warnings), loop count, wall time,
   and spot-check question quality lesson-by-lesson. Record both run ids in
   RESILIENCE_PLAN §12 with a short verdict.

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| CLI flag drift across versions | doctor version check; flags centralized in one builder function per backend |
| Codex behaves agentically (explores files, runs commands) | empty `-C` dir + `-s read-only` + `--output-schema` + explicit "answer directly" instruction |
| Claude `-p` tool use surprises | generation prompts don't need tools; add explicit disable flag if observed |
| Enterprise org policy blocks headless/model | surfaces in doctor `--ping`; escalate to admin |
| Seat window exhaustion mid-build | backoff + resume-friendly: chunked A2 partial regeneration already tolerates re-runs |
| CLI startup latency (~2–5 s/call) | concurrency 2–4; ~26 calls → negligible vs LLM time |
| Long prompt > argv limits | stdin everywhere |
| Output truncation | already solved by chunked per-lesson calls |

## 7. Work order for the session (with rough sizes)

1. **Refactor to backend interface** — extract `backends.py`, `OpenAIBackend`,
   inject into `LLMClient`; all 37 existing tests must pass unchanged. (~45 min)
2. **`ClaudeCodeBackend`** + parser fixtures + unit tests. (~45 min)
3. **`CodexBackend`** (incl. `--output-schema` from pydantic) + unit tests. (~45 min)
4. **Wiring**: `--backend` flag, env vars, backend-qualified `model_id` in
   artifacts, concurrency env; `doctor` command. (~30 min)
5. **E2E both backends on sample.txt** + quality comparison + docs
   (README, RESILIENCE_PLAN §12, memory update). (~45 min)

Definition of done:
- `python main.py run --input-file sample.txt --backend claude-code` and
  `--backend codex` both produce a TechLingo-native `course.json` with the
  same quality gates, at $0 marginal cost;
- user can flip backends with one flag/env var;
- OpenAI path still works for A/B comparison.

## 8. Out of scope (later, if wanted)

- Mixed backends per stage (e.g. codex for A2 generation, claude for A5 review)
  — trivial once the interface exists; needs a small routing config.
- Claude Agent SDK (`claude-agent-sdk` pip) instead of subprocess — cleaner
  streaming/session control; subprocess is simpler and version-independent, so
  v1 uses subprocess.
- Caching the A0/A1 outputs across runs of the same source document.
