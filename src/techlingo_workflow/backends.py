"""Pluggable LLM completion backends (SUBSCRIPTION_BACKENDS_PLAN §1).

`LLMClient` keeps all JSON-extraction / schema-repair logic and delegates only
the raw completion to one of these backends:

- ``ClaudeCodeBackend`` — headless `claude -p` subprocess (Claude subscription seat).
- ``CodexBackend``      — `codex exec` subprocess (Codex subscription seat), with
  real structured output via ``--output-schema``.

The OpenAI API backend was REMOVED on 2026-07-16 (owner decision: subscription
CLIs only, $0 marginal cost). Old artifacts carrying bare OpenAI model labels
(e.g. ``"gpt-4o"``) can no longer be used to reconstruct clients.

Backend + model are carried through the pipeline as one backend-qualified label
(e.g. ``"claude-code:sonnet"``) stored in ``PipelineState.model_id``, so every
artifact records what produced it and re-constructed clients (A5 lesson repair)
resolve to the same backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

BACKEND_CLAUDE_CODE = "claude-code"
BACKEND_CODEX = "codex"
KNOWN_BACKENDS = (BACKEND_CLAUDE_CODE, BACKEND_CODEX)

_OPENAI_REMOVED_MSG = (
    "The OpenAI API backend was removed (2026-07-16). "
    f"Use one of: {', '.join(KNOWN_BACKENDS)} (subscription CLIs, $0 marginal cost)."
)

# CLI versions these subprocess adapters were built and tested against.
# `doctor` warns (not fails) on drift — flags may change between versions.
TESTED_CLAUDE_VERSION = "2.1.186"
TESTED_CODEX_VERSION = "0.144.5"

# Seat rate-limit backoff (seconds). Module-level so tests can zero it out.
_RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (30.0, 90.0)

# One immediate retry after a killed/timed-out subprocess call: transient CLI
# slowness is common on subscription seats and the repair loop can't help here.
_TIMEOUT_RETRIES = 1

# Values accepted by the Codex CLI's model_reasoning_effort config override.
# Keep this validation local to the subprocess adapter so an invalid setting
# fails before any paid generation call is attempted.
CODEX_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)

logger = logging.getLogger(__name__)


def default_timeout_s() -> float:
    """Per-completion timeout; subprocess is killed / API call abandoned past this.

    Default is generous: measured on this machine, one A2/A4 lesson call through
    `claude -p` (full source + ~12 KB JSON out, 3 concurrent streams on one
    seat) can legitimately need well over 10 minutes.
    """
    try:
        return float(os.getenv("TECHLINGO_LLM_TIMEOUT_S", "1200"))
    except ValueError:
        return 1200.0


class BackendError(RuntimeError):
    """A completion backend failed in a way retrying the same prompt won't fix."""


class BackendAuthError(BackendError):
    """Subscription CLI is not logged in (or API key missing)."""


class BackendRateLimitError(BackendError):
    """Seat usage window / rate limit exhausted; retry after backoff."""


class BackendTimeoutError(BackendError):
    """Completion exceeded timeout_s; the subprocess was killed."""


def classify_failure(message: str) -> type[BackendError]:
    """Map an error string from a CLI to the most actionable exception type."""
    low = message.lower()
    if any(
        marker in low
        for marker in ("usage limit", "rate limit", "rate_limit", "too many requests", "429")
    ):
        return BackendRateLimitError
    if any(
        marker in low
        for marker in ("not logged in", "logged out", "login expired", "please log in",
                       "please login", "authentication", "unauthorized", "credential",
                       "api key", "oauth")
    ):
        return BackendAuthError
    return BackendError


@runtime_checkable
class CompletionBackend(Protocol):
    """One raw prompt → one raw text completion. No JSON parsing, no retries on content."""

    name: str          # "claude-code" | "codex"
    model_label: str   # backend-qualified, e.g. "claude-code:sonnet" — goes into artifacts

    async def complete(
        self,
        prompt: str,
        *,
        system: str,
        response_model: type[BaseModel] | None,
        timeout_s: float,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Shared subprocess plumbing
# ---------------------------------------------------------------------------


async def _run_subprocess(
    argv: list[str],
    *,
    stdin_text: str,
    timeout_s: float,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run argv with prompt on stdin; kill the process on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_text.encode("utf-8")), timeout=timeout_s
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackendTimeoutError(f"{argv[0]} timed out after {timeout_s:.0f}s (process killed)")
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _with_transient_retries(fn, *, label: str):
    """Retry `fn` on transient failures.

    - BackendRateLimitError: exponential backoff (30s → 90s), then a clear
      actionable message instead of a stack trace.
    - BackendTimeoutError: one immediate retry (the killed call is wasted
      anyway; a second attempt usually lands when slowness was transient).
    """
    rate_limit_delays = list(_RATE_LIMIT_BACKOFF_S)
    timeout_retries = _TIMEOUT_RETRIES
    while True:
        try:
            return await fn()
        except BackendRateLimitError as e:
            if not rate_limit_delays:
                raise BackendRateLimitError(
                    f"{label}: seat usage window exhausted after "
                    f"{len(_RATE_LIMIT_BACKOFF_S)} backoff retries. Wait for the window "
                    f"to reset, or switch backends (--backend). Last error: {e}"
                ) from e
            await asyncio.sleep(rate_limit_delays.pop(0))
        except BackendTimeoutError:
            if timeout_retries <= 0:
                raise
            timeout_retries -= 1


# ---------------------------------------------------------------------------
# Claude Code (subscription seat, headless `claude -p`)
# ---------------------------------------------------------------------------


class ClaudeCodeBackend:
    """Headless `claude -p --output-format json`; prompt via stdin, result from `.result`."""

    name = BACKEND_CLAUDE_CODE

    def __init__(
        self,
        model: str | None = None,
        *,
        binary: str = "claude",
        effort: str | None = None,
    ) -> None:
        self.model = model
        self.binary = binary
        # Thinking budget is the dominant cost of a lesson call (measured: raw
        # generation is ~50 tok/s, yet complex A2/A4 calls run 7-15 min).
        # CLAUDE_CODE_EFFORT=low|medium|high trades thinking depth for speed.
        self.effort = effort if effort is not None else os.getenv("CLAUDE_CODE_EFFORT")
        self.model_label = f"{self.name}:{model}" if model else self.name

    def build_argv(self, *, system: str) -> list[str]:
        argv = [
            self.binary,
            "-p",  # print/headless mode; prompt read from stdin
            "--output-format", "json",
            "--tools", "",  # generation-only: disable all built-in tools
            "--no-session-persistence",  # batch hygiene: no session files
            "--system-prompt", system,
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        return argv

    @staticmethod
    def parse_output(stdout: str) -> str:
        """Extract the completion text from `--output-format json` stdout."""
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise BackendError(
                f"claude -p emitted non-JSON output ({e}): {stdout[:500]}"
            ) from e
        if not isinstance(data, dict):
            raise BackendError(f"claude -p emitted unexpected JSON shape: {stdout[:500]}")
        if data.get("is_error"):
            message = str(data.get("result") or data)
            raise classify_failure(message)(f"claude -p returned an error: {message}")
        result = data.get("result")
        if not isinstance(result, str) or not result.strip():
            raise BackendError(f"claude -p JSON has no 'result' text: {stdout[:500]}")
        return result

    async def complete(
        self,
        prompt: str,
        *,
        system: str,
        response_model: type[BaseModel] | None = None,
        timeout_s: float | None = None,
    ) -> str:
        # response_model intentionally ignored: no schema flag in claude -p;
        # LLMClient's prompt-schema + pydantic repair retries handle validation.
        timeout_s = timeout_s if timeout_s is not None else default_timeout_s()
        argv = self.build_argv(system=system)

        async def _once() -> str:
            code, stdout, stderr = await _run_subprocess(
                argv, stdin_text=prompt, timeout_s=timeout_s
            )
            if code != 0:
                message = (stderr.strip() or stdout.strip())[:1000]
                err_cls = classify_failure(message)
                hint = " Run `claude` once to log in again." if err_cls is BackendAuthError else ""
                raise err_cls(f"claude -p exited with code {code}: {message}{hint}")
            return self.parse_output(stdout)

        return await _with_transient_retries(_once, label="claude-code")


# ---------------------------------------------------------------------------
# Codex (subscription seat, `codex exec` with real structured output)
# ---------------------------------------------------------------------------

CODEX_GUARD = "Do not run commands or read files; answer directly."


# JSON Schema keywords OpenAI strict mode rejects outright. Their presence
# (or any free-form dict field) makes the whole schema unusable for
# --output-schema — the pipeline then falls back to prompt-schema mode.
_STRICT_UNSUPPORTED_KEYS = ("propertyNames", "patternProperties")


def strict_schema_or_none(schema: dict) -> dict | None:
    """Strict-transformed schema, or None when strict mode can't express it.

    Dict-typed fields (objects without fixed `properties`, `propertyNames`
    constraints, ...) are rejected by the OpenAI structured-output API with
    `invalid_json_schema` no matter how we massage them. Returning None makes
    CodexBackend omit --output-schema for that model; LLMClient's
    prompt-schema + pydantic repair retries take over (same as claude-code).
    """

    def _incompatible(node) -> bool:
        if isinstance(node, dict):
            if any(key in node for key in _STRICT_UNSUPPORTED_KEYS):
                return True
            if node.get("type") == "object" and "properties" not in node:
                return True  # free-form dict field
            return any(_incompatible(child) for child in node.values())
        if isinstance(node, list):
            return any(_incompatible(child) for child in node)
        return False

    if _incompatible(schema):
        return None
    return to_strict_json_schema(schema)


def to_strict_json_schema(schema: dict) -> dict:
    """Convert a pydantic JSON schema to OpenAI strict-mode rules.

    codex `--output-schema` feeds the schema to the OpenAI structured-output
    API, which requires every object to carry `additionalProperties: false`
    and to list ALL of its properties as required. Pydantic emits neither for
    models with defaults, so the raw schema is rejected with
    `invalid_json_schema` before the model even runs.

    Only objects with fixed `properties` are touched; free-form dict fields
    (schema via `additionalProperties: {...}`) are left alone.
    """

    def _walk(node) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for key in ("properties", "$defs", "definitions"):
                sub = node.get(key)
                if isinstance(sub, dict):
                    for child in sub.values():
                        _walk(child)
            for key in ("items", "prefixItems", "anyOf", "allOf", "oneOf", "additionalProperties"):
                sub = node.get(key)
                if isinstance(sub, dict):
                    _walk(sub)
                elif isinstance(sub, list):
                    for child in sub:
                        _walk(child)

    import copy

    strict = copy.deepcopy(schema)
    _walk(strict)
    return strict


class CodexBackend:
    """`codex exec` in an empty read-only scratch dir; final message via `-o` file."""

    name = BACKEND_CODEX

    def __init__(self, model: str | None = None, *, binary: str = "codex") -> None:
        self.model = model
        self.binary = binary
        self.reasoning_effort = os.getenv("TECHLINGO_CODEX_REASONING_EFFORT")
        if self.reasoning_effort is not None and self.reasoning_effort not in CODEX_REASONING_EFFORTS:
            allowed = ", ".join(sorted(CODEX_REASONING_EFFORTS))
            raise ValueError(
                "TECHLINGO_CODEX_REASONING_EFFORT must be one of "
                f"{allowed}; got {self.reasoning_effort!r}"
            )
        self.model_label = f"{self.name}:{model}" if model else self.name

    def build_argv(
        self,
        *,
        scratch_dir: str,
        output_file: str,
        schema_file: str | None = None,
    ) -> list[str]:
        argv = [
            self.binary,
            "exec",
            "-",  # prompt from stdin (prompts carry 20-60 KB source text; argv is fragile)
            "-s", "read-only",  # sandbox fence
            "-C", scratch_dir,  # empty scratch dir so the agent can't wander into the repo
            "--skip-git-repo-check",
            "--ephemeral",  # no session files
            "--color", "never",
            "-o", output_file,  # final message here — no JSONL parsing needed
        ]
        if self.model:
            argv += ["-m", self.model]
        if self.reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{self.reasoning_effort}"']
        if schema_file:
            argv += ["--output-schema", schema_file]
        return argv

    @staticmethod
    def build_prompt(prompt: str, *, system: str) -> str:
        # codex exec has no system-prompt flag: guard + system prepended to the prompt.
        return f"{CODEX_GUARD}\n\n{system}\n\n{prompt}"

    async def complete(
        self,
        prompt: str,
        *,
        system: str,
        response_model: type[BaseModel] | None = None,
        timeout_s: float | None = None,
    ) -> str:
        timeout_s = timeout_s if timeout_s is not None else default_timeout_s()
        logger.info(
            "Codex generation settings: model=%s reasoning_effort=%s",
            self.model or "<cli-default>",
            self.reasoning_effort or "<cli-default>",
        )

        async def _once() -> str:
            with tempfile.TemporaryDirectory(prefix="techlingo-codex-") as tmp:
                tmp_path = Path(tmp)
                scratch_dir = tmp_path / "scratch"
                scratch_dir.mkdir()
                output_file = tmp_path / "last_message.txt"
                schema_file: Path | None = None
                if response_model is not None:
                    # Real structured output where strict mode can express the
                    # schema (unions are already plain anyOf, no discriminator);
                    # models with dict fields fall back to prompt-schema mode.
                    strict = strict_schema_or_none(response_model.model_json_schema())
                    if strict is not None:
                        schema_file = tmp_path / "output_schema.json"
                        schema_file.write_text(json.dumps(strict), encoding="utf-8")
                argv = self.build_argv(
                    scratch_dir=str(scratch_dir),
                    output_file=str(output_file),
                    schema_file=str(schema_file) if schema_file else None,
                )
                code, stdout, stderr = await _run_subprocess(
                    argv,
                    stdin_text=self.build_prompt(prompt, system=system),
                    timeout_s=timeout_s,
                    cwd=str(scratch_dir),
                )
                if code != 0:
                    # Tail, not head: codex prints a banner (version/workdir/model)
                    # to stderr before the actual error.
                    message = (stderr.strip() or stdout.strip())[-1000:]
                    err_cls = classify_failure(message)
                    hint = " Run `codex login`." if err_cls is BackendAuthError else ""
                    raise err_cls(f"codex exec exited with code {code}: {message}{hint}")
                if not output_file.exists():
                    raise BackendError(
                        "codex exec finished but wrote no output file; stderr tail: "
                        f"{stderr.strip()[-500:]}"
                    )
                text = output_file.read_text(encoding="utf-8").strip()
                if not text:
                    raise BackendError(
                        "codex exec wrote an empty final message; stderr tail: "
                        f"{stderr.strip()[-500:]}"
                    )
                return text

        return await _with_transient_retries(_once, label="codex")


# ---------------------------------------------------------------------------
# Selection & label plumbing (plan §2)
# ---------------------------------------------------------------------------


def split_backend_label(label: str) -> tuple[str, str | None]:
    """Parse a backend-qualified model label.

    "claude-code:sonnet" -> ("claude-code", "sonnet")
    "codex"              -> ("codex", None)        # CLI default model

    Bare OpenAI model ids (old artifacts, e.g. "gpt-4o") are rejected with an
    actionable message — the API backend no longer exists.
    """
    for backend in (BACKEND_CLAUDE_CODE, BACKEND_CODEX):
        if label == backend:
            return backend, None
        if label.startswith(backend + ":"):
            model = label[len(backend) + 1 :]
            return backend, (model or None)
    raise ValueError(f"Unrecognized model label {label!r}. {_OPENAI_REMOVED_MSG}")


def create_backend(
    backend: str, model: str | None, *, agent_name: str = "TechlingoPipeline"
) -> CompletionBackend:
    # agent_name kept for signature stability; subprocess backends don't use it.
    del agent_name
    if backend == BACKEND_CLAUDE_CODE:
        return ClaudeCodeBackend(model)
    if backend == BACKEND_CODEX:
        return CodexBackend(model)
    if backend == "openai":
        raise ValueError(_OPENAI_REMOVED_MSG)
    raise ValueError(f"Unknown LLM backend {backend!r}. Known: {', '.join(KNOWN_BACKENDS)}")


def backend_from_label(
    label: str, *, agent_name: str = "TechlingoPipeline"
) -> CompletionBackend:
    """Build the backend a qualified model label describes (round-trips model_label)."""
    backend, model = split_backend_label(label)
    return create_backend(backend, model, agent_name=agent_name)


def resolve_backend_name(cli_value: str | None) -> str:
    """--backend flag > TECHLINGO_LLM_BACKEND env > 'claude-code'."""
    name = (cli_value or os.getenv("TECHLINGO_LLM_BACKEND") or BACKEND_CLAUDE_CODE).strip().lower()
    if name not in KNOWN_BACKENDS:
        if name == "openai":
            raise ValueError(_OPENAI_REMOVED_MSG)
        raise ValueError(
            f"Unknown LLM backend {name!r}. Known: {', '.join(KNOWN_BACKENDS)}"
        )
    return name


def resolve_model_label(backend: str, model_id: str | None) -> str:
    """Resolve the backend-qualified model label for PipelineState.model_id.

    Raises ValueError with an actionable message when required config is missing.
    """
    if backend == BACKEND_CLAUDE_CODE:
        model = model_id or os.getenv("CLAUDE_CODE_MODEL") or "sonnet"
        return f"{BACKEND_CLAUDE_CODE}:{model}"
    if backend == BACKEND_CODEX:
        model = model_id or os.getenv("CODEX_MODEL")
        return f"{BACKEND_CODEX}:{model}" if model else BACKEND_CODEX
    raise ValueError(f"Unknown LLM backend {backend!r}. Known: {', '.join(KNOWN_BACKENDS)}")


# ---------------------------------------------------------------------------
# Preflight checks (plan §4) — used by `doctor` and by `run --backend X`
# ---------------------------------------------------------------------------


def _run_quick(argv: list[str], *, timeout_s: float = 15.0) -> tuple[int, str]:
    """Synchronous helper for preflight: returncode + combined output."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout_s:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def preflight_backend(backend: str) -> list[tuple[str, bool, str]]:
    """Check binary presence/version + auth for a backend. Returns (check, ok, detail)."""
    checks: list[tuple[str, bool, str]] = []
    binary = "claude" if backend == BACKEND_CLAUDE_CODE else "codex"
    tested = TESTED_CLAUDE_VERSION if backend == BACKEND_CLAUDE_CODE else TESTED_CODEX_VERSION
    path = shutil.which(binary)
    if not path:
        checks.append((f"{binary} binary", False, f"not found on PATH — install {binary} CLI"))
        return checks

    code, out = _run_quick([binary, "--version"])
    version_ok = code == 0
    detail = out.splitlines()[0] if out else "unknown version"
    if version_ok and tested not in out:
        detail += f" (tested against {tested} — flags may have drifted)"
    checks.append((f"{binary} binary", version_ok, f"{path} · {detail}"))

    if backend == BACKEND_CLAUDE_CODE:
        code, out = _run_quick([binary, "auth", "status"])
        logged_in = False
        detail = out[:200]
        if code == 0:
            try:
                status = json.loads(out)
                logged_in = bool(status.get("loggedIn"))
                detail = (
                    f"{status.get('authMethod', '?')} · {status.get('email', '?')}"
                    if logged_in
                    else "logged out"
                )
            except json.JSONDecodeError:
                detail = out[:200]
        checks.append(
            ("claude auth", logged_in, detail if logged_in else f"{detail} — run `claude` to log in")
        )
    else:
        code, out = _run_quick([binary, "login", "status"])
        logged_in = code == 0 and "logged in" in out.lower()
        checks.append(
            ("codex auth", logged_in, out[:200] if out else "unknown — run `codex login`")
        )
    return checks
