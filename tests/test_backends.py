"""Tests for the pluggable completion backends (backends.py) and the LLMClient refactor.

No real CLI/API calls: subprocess backends run against fake `claude`/`codex`
shell scripts, and LLMClient logic runs against an in-memory FakeBackend.

Run without pytest:  PYTHONPATH=src python tests/test_backends.py
Or with pytest:      PYTHONPATH=src pytest tests/test_backends.py
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel

import techlingo_workflow.backends as backends
from techlingo_workflow.backends import (
    BackendAuthError,
    BackendError,
    BackendRateLimitError,
    BackendTimeoutError,
    ClaudeCodeBackend,
    CodexBackend,
    classify_failure,
    create_backend,
    resolve_backend_name,
    resolve_model_label,
    split_backend_label,
)
from techlingo_workflow.llm import LLMClient

# Zero out seat backoff so rate-limit tests don't sleep 30s.
backends._RATE_LIMIT_BACKOFF_S = (0.0, 0.0)


def _run(coro):
    return asyncio.run(coro)


def _write_script(dir_path: Path, name: str, body: str) -> str:
    path = dir_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _Answer(BaseModel):
    ok: bool
    count: int


# ---------------------------------------------------------------------------
# Label parsing & selection
# ---------------------------------------------------------------------------


def test_split_backend_label():
    assert split_backend_label("claude-code:sonnet") == ("claude-code", "sonnet")
    assert split_backend_label("claude-code") == ("claude-code", None)
    assert split_backend_label("codex:gpt-5-codex") == ("codex", "gpt-5-codex")
    assert split_backend_label("codex") == ("codex", None)
    # Bare OpenAI labels (old artifacts) are rejected with the removal message.
    for legacy in ("gpt-4o-mini", "openai:gpt-4o"):
        try:
            split_backend_label(legacy)
            assert False, f"legacy openai label accepted: {legacy}"
        except ValueError as e:
            assert "removed" in str(e)


def test_backend_label_round_trip():
    for label in ("claude-code:sonnet", "claude-code:opus", "codex", "codex:o3"):
        backend = create_backend(*split_backend_label(label))
        assert backend.model_label == label, f"{label} -> {backend.model_label}"


def test_resolve_backend_name_env_and_flag():
    old = os.environ.pop("TECHLINGO_LLM_BACKEND", None)
    try:
        assert resolve_backend_name(None) == "claude-code"  # new default
        os.environ["TECHLINGO_LLM_BACKEND"] = "codex"
        assert resolve_backend_name(None) == "codex"
        assert resolve_backend_name("claude-code") == "claude-code"  # flag wins over env
        try:
            resolve_backend_name("gemini")
            assert False, "unknown backend accepted"
        except ValueError:
            pass
        try:
            resolve_backend_name("openai")
            assert False, "removed openai backend accepted"
        except ValueError as e:
            assert "removed" in str(e)
    finally:
        os.environ.pop("TECHLINGO_LLM_BACKEND", None)
        if old is not None:
            os.environ["TECHLINGO_LLM_BACKEND"] = old


def test_resolve_model_label_per_backend():
    saved = {k: os.environ.pop(k, None) for k in ("CLAUDE_CODE_MODEL", "CODEX_MODEL")}
    try:
        assert resolve_model_label("claude-code", None) == "claude-code:sonnet"  # recommended default
        assert resolve_model_label("claude-code", "opus") == "claude-code:opus"
        os.environ["CLAUDE_CODE_MODEL"] = "haiku"
        assert resolve_model_label("claude-code", None) == "claude-code:haiku"

        assert resolve_model_label("codex", None) == "codex"  # CLI default model
        os.environ["CODEX_MODEL"] = "o3"
        assert resolve_model_label("codex", None) == "codex:o3"

        try:
            resolve_model_label("openai", "gpt-4o")
            assert False, "removed openai backend accepted"
        except ValueError:
            pass
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_classify_failure():
    assert classify_failure("Usage limit reached, resets 5pm") is BackendRateLimitError
    assert classify_failure("HTTP 429 Too Many Requests") is BackendRateLimitError
    assert classify_failure("Not logged in. Please log in.") is BackendAuthError
    assert classify_failure("something exploded") is BackendError


# ---------------------------------------------------------------------------
# ClaudeCodeBackend: argv + output parsing + subprocess round-trip
# ---------------------------------------------------------------------------


def test_claude_argv():
    argv = ClaudeCodeBackend("sonnet").build_argv(system="SYS")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""  # all built-in tools disabled
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "sonnet"
    # default model -> no --model flag (CLI default)
    assert "--model" not in ClaudeCodeBackend().build_argv(system="SYS")


def test_claude_effort_flag():
    argv = ClaudeCodeBackend("sonnet", effort="medium").build_argv(system="SYS")
    assert argv[argv.index("--effort") + 1] == "medium"
    old = os.environ.pop("CLAUDE_CODE_EFFORT", None)
    try:
        assert "--effort" not in ClaudeCodeBackend("sonnet").build_argv(system="SYS")
        os.environ["CLAUDE_CODE_EFFORT"] = "low"
        argv = ClaudeCodeBackend("sonnet").build_argv(system="SYS")
        assert argv[argv.index("--effort") + 1] == "low"
    finally:
        os.environ.pop("CLAUDE_CODE_EFFORT", None)
        if old is not None:
            os.environ["CLAUDE_CODE_EFFORT"] = old


def test_claude_parse_output():
    ok = json.dumps({"is_error": False, "result": '{"ok": true}'})
    assert ClaudeCodeBackend.parse_output(ok) == '{"ok": true}'

    for bad, expected in [
        (json.dumps({"is_error": True, "result": "Usage limit reached"}), BackendRateLimitError),
        (json.dumps({"is_error": True, "result": "OAuth token revoked, please log in"}), BackendAuthError),
        (json.dumps({"is_error": True, "result": "internal error"}), BackendError),
        ("not json at all", BackendError),
        (json.dumps({"is_error": False}), BackendError),  # missing result text
    ]:
        try:
            ClaudeCodeBackend.parse_output(bad)
            assert False, f"parse_output accepted {bad!r}"
        except expected:
            pass


def test_claude_subprocess_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        # Fake claude: records stdin+argv, then answers like `-p --output-format json`.
        capture = Path(tmp) / "capture.txt"
        binary = _write_script(
            Path(tmp),
            "claude",
            f'cat > "{capture}.stdin"\n'
            f'printf \'%s\\n\' "$@" > "{capture}.argv"\n'
            'echo \'{"is_error": false, "result": "{\\"ok\\": true, \\"count\\": 2}"}\'',
        )
        backend = ClaudeCodeBackend("sonnet", binary=binary)
        text = _run(backend.complete("PROMPT-BODY", system="SYS", response_model=None, timeout_s=30))
        assert text == '{"ok": true, "count": 2}'
        assert Path(f"{capture}.stdin").read_text() == "PROMPT-BODY"  # prompt via stdin
        argv = Path(f"{capture}.argv").read_text().splitlines()
        assert "--system-prompt" in argv and "SYS" in argv


def test_claude_nonzero_exit_classified():
    with tempfile.TemporaryDirectory() as tmp:
        binary = _write_script(
            Path(tmp), "claude", 'cat > /dev/null\necho "usage limit reached" >&2\nexit 1'
        )
        backend = ClaudeCodeBackend(binary=binary)
        try:
            _run(backend.complete("p", system="s", response_model=None, timeout_s=30))
            assert False, "nonzero exit accepted"
        except BackendRateLimitError:
            pass  # backoff retried, then surfaced the actionable rate-limit error


def test_claude_timeout_kills_process():
    with tempfile.TemporaryDirectory() as tmp:
        binary = _write_script(Path(tmp), "claude", "cat > /dev/null\nsleep 30")
        backend = ClaudeCodeBackend(binary=binary)
        try:
            _run(backend.complete("p", system="s", response_model=None, timeout_s=0.3))
            assert False, "timeout not raised"
        except BackendTimeoutError as e:
            assert "timed out" in str(e)


def test_timeout_retried_once_then_succeeds():
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "first-attempt-done"
        # 1st invocation: hang past the timeout; 2nd: answer immediately.
        binary = _write_script(
            Path(tmp),
            "claude",
            f'cat > /dev/null\n'
            f'if [ ! -f "{marker}" ]; then touch "{marker}"; sleep 30; fi\n'
            'echo \'{"is_error": false, "result": "recovered"}\'',
        )
        backend = ClaudeCodeBackend(binary=binary)
        text = _run(backend.complete("p", system="s", response_model=None, timeout_s=1.0))
        assert text == "recovered"


# ---------------------------------------------------------------------------
# CodexBackend: argv + prompt building + subprocess round-trip
# ---------------------------------------------------------------------------


def test_codex_argv():
    backend = CodexBackend("o3")
    argv = backend.build_argv(scratch_dir="/scratch", output_file="/out.txt", schema_file="/schema.json")
    assert argv[:3] == ["codex", "exec", "-"]  # prompt from stdin
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == "/scratch"
    assert "--skip-git-repo-check" in argv
    assert "--ephemeral" in argv
    assert argv[argv.index("-o") + 1] == "/out.txt"
    assert argv[argv.index("-m") + 1] == "o3"
    assert argv[argv.index("--output-schema") + 1] == "/schema.json"
    # no model / no schema -> flags omitted
    argv2 = CodexBackend().build_argv(scratch_dir="/s", output_file="/o")
    assert "-m" not in argv2 and "--output-schema" not in argv2


def test_codex_prompt_prepends_guard_and_system():
    text = CodexBackend.build_prompt("TASK", system="SYS")
    assert text.startswith(backends.CODEX_GUARD)
    assert text.index(backends.CODEX_GUARD) < text.index("SYS") < text.index("TASK")


def test_codex_subprocess_round_trip_with_schema():
    with tempfile.TemporaryDirectory() as tmp:
        capture = Path(tmp) / "capture"
        # Fake codex: finds the -o and --output-schema argv values, copies the
        # schema aside, writes the final message to the -o file.
        binary = _write_script(
            Path(tmp),
            "codex",
            f'cat > "{capture}.stdin"\n'
            'out=""; schema=""\n'
            'while [ $# -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    -o) out="$2"; shift ;;\n'
            '    --output-schema) schema="$2"; shift ;;\n'
            '  esac\n'
            '  shift\n'
            'done\n'
            f'[ -n "$schema" ] && cp "$schema" "{capture}.schema"\n'
            'printf \'{"ok": true, "count": 7}\' > "$out"',
        )
        backend = CodexBackend("o3", binary=binary)
        text = _run(backend.complete("TASK", system="SYS", response_model=_Answer, timeout_s=30))
        assert text == '{"ok": true, "count": 7}'
        stdin_text = Path(f"{capture}.stdin").read_text()
        assert stdin_text.startswith(backends.CODEX_GUARD) and "SYS" in stdin_text and "TASK" in stdin_text
        schema = json.loads(Path(f"{capture}.schema").read_text())
        assert set(schema["properties"]) == {"ok", "count"}  # real pydantic schema passed
        assert schema["additionalProperties"] is False  # strict-mode transform applied


def test_to_strict_json_schema():
    class Inner(BaseModel):
        x: int = 0

    class Outer(BaseModel):
        name: str
        inner: Inner | None = None
        tags: list[Inner] = []

    strict = backends.to_strict_json_schema(Outer.model_json_schema())
    # Every object with fixed properties: additionalProperties false + ALL props required.
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"name", "inner", "tags"}
    inner = strict["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["x"]
    # Original schema untouched (deep copy).
    assert "additionalProperties" not in Outer.model_json_schema()


def test_strict_schema_or_none_falls_back_on_dict_fields():
    class WithDict(BaseModel):
        name: str
        counts: dict[str, int] = {}

    assert backends.strict_schema_or_none(WithDict.model_json_schema()) is None

    class Plain(BaseModel):
        name: str

    strict = backends.strict_schema_or_none(Plain.model_json_schema())
    assert strict is not None and strict["additionalProperties"] is False

    # The real pipeline models: LessonGen must keep real structured output,
    # TextAnalysisResult (dict field parts_by_type) must fall back.
    from techlingo_workflow.models import LessonGen, TextAnalysisResult

    assert backends.strict_schema_or_none(LessonGen.model_json_schema()) is not None
    assert backends.strict_schema_or_none(TextAnalysisResult.model_json_schema()) is None


def test_codex_missing_output_file_is_error():
    with tempfile.TemporaryDirectory() as tmp:
        binary = _write_script(Path(tmp), "codex", "cat > /dev/null\nexit 0")
        backend = CodexBackend(binary=binary)
        try:
            _run(backend.complete("p", system="s", response_model=None, timeout_s=30))
            assert False, "missing -o file accepted"
        except BackendError as e:
            assert "no output file" in str(e)


def test_codex_auth_error_hint():
    with tempfile.TemporaryDirectory() as tmp:
        binary = _write_script(
            Path(tmp), "codex", 'cat > /dev/null\necho "Not logged in" >&2\nexit 1'
        )
        backend = CodexBackend(binary=binary)
        try:
            _run(backend.complete("p", system="s", response_model=None, timeout_s=30))
            assert False, "auth failure accepted"
        except BackendAuthError as e:
            assert "codex login" in str(e)


# ---------------------------------------------------------------------------
# LLMClient over a FakeBackend: retry/extraction logic is backend-agnostic
# ---------------------------------------------------------------------------


class FakeBackend:
    name = "fake"
    model_label = "fake:model"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def complete(self, prompt, *, system, response_model=None, timeout_s=0.0):
        self.calls.append(
            {"prompt": prompt, "system": system, "response_model": response_model}
        )
        return self.outputs.pop(0)


def test_llmclient_run_json_repairs_bad_json():
    fake = FakeBackend(["this is not json", '```json\n{"a": 1}\n```'])
    client = LLMClient(backend=fake, instructions="SYS")
    data = _run(client.run_json("do the thing"))
    assert data == {"a": 1}
    assert len(fake.calls) == 2
    assert "was not valid JSON" in fake.calls[1]["prompt"]  # repair feedback sent
    assert fake.calls[0]["system"] == "SYS"


def test_llmclient_run_json_model_schema_repair():
    fake = FakeBackend(
        ['{"ok": true, "count": "many"}', '{"ok": true, "count": 3}']  # 1st fails validation
    )
    client = LLMClient(backend=fake)
    data, parsed = _run(client.run_json_model("task", _Answer))
    assert parsed.count == 3 and data == {"ok": True, "count": 3}
    assert fake.calls[0]["response_model"] is _Answer  # schema offered to the backend
    assert "did not match the required schema" in fake.calls[1]["prompt"]


def test_llmclient_model_id_defaults_to_backend_label():
    client = LLMClient(backend=FakeBackend([]))
    assert client.model_id == "fake:model"


def test_llmclient_from_label_builds_matching_backend():
    client = LLMClient(model_id="claude-code:sonnet")
    assert isinstance(client.backend, ClaudeCodeBackend)
    assert client.backend.model == "sonnet"
    assert client.model_id == "claude-code:sonnet"

    client2 = LLMClient(model_id="codex")
    assert isinstance(client2.backend, CodexBackend)
    assert client2.backend.model is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _run_all() else 0)
