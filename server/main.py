from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Ensure we can import from src
_SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(_SRC))

from techlingo_workflow.backends import (
    BACKEND_OPENAI,
    preflight_backend,
    resolve_backend_name,
    resolve_model_label,
)
from techlingo_workflow.io import new_run_dir, write_json, write_text
from techlingo_workflow.models import PipelineState, TextAnalysisResult
from techlingo_workflow.workflow import build_techlingo_workflow, build_analysis_workflow
from techlingo_workflow.config import WorkflowConfig, DifficultyLevel

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify the frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RunRequest(BaseModel):
    input_text: str
    config: Optional[WorkflowConfig] = None
    difficulty: Optional[DifficultyLevel] = None
    model_id: Optional[str] = None
    backend: Optional[str] = None  # openai | claude-code | codex (default: TECHLINGO_LLM_BACKEND env)


def _resolve_backend_for_request(request_data: dict) -> tuple[str | None, str | None, str | None]:
    """Resolve (backend_name, model_label, error). Same rules as the CLI:
    request field > TECHLINGO_LLM_BACKEND env > openai; preflight the CLI
    backends so a logged-out seat fails fast with an actionable message."""
    try:
        backend_name = resolve_backend_name(request_data.get("backend"))
        model_label = resolve_model_label(backend_name, request_data.get("model_id"))
    except ValueError as e:
        return None, None, str(e)
    if backend_name != BACKEND_OPENAI:
        failures = [
            f"{check}: {detail}"
            for check, ok, detail in preflight_backend(backend_name)
            if not ok
        ]
        if failures:
            return None, None, f"Backend '{backend_name}' failed preflight: " + "; ".join(failures)
    return backend_name, model_label, None

@app.get("/")
def read_root():
    return {"status": "ok", "message": "TechLingo API is running"}


# ---------------------------------------------------------------------------
# Post-run editing: manual exercise edits + single-question regeneration.
# All writes go through editing.save_course (normalize → re-emit → re-validate),
# so the canonical course.json can never drift from the edited internal course.
# ---------------------------------------------------------------------------

OUTPUTS_DIR = Path("outputs")


class ExerciseEditRequest(BaseModel):
    module_index: int
    lesson_index: int
    exercise_index: int
    exercise: dict


class RegenerateRequest(BaseModel):
    module_index: int
    lesson_index: int
    exercise_index: int
    instructions: Optional[str] = None
    backend: Optional[str] = None
    model_id: Optional[str] = None


def _run_dir_or_404(run_id: str) -> Path:
    # Run ids are directory names; refuse anything that could escape outputs/.
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="Invalid run id.")
    run_dir = OUTPUTS_DIR / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return run_dir


@app.put("/api/runs/{run_id}/exercise")
def edit_exercise(run_id: str, req: ExerciseEditRequest):
    """Apply a manual edit to one exercise, then re-emit + re-validate the course."""
    from techlingo_workflow.editing import (
        EditError,
        apply_exercise_edit,
        load_internal_course,
        save_course,
        _report_payload,
    )

    run_dir = _run_dir_or_404(run_id)
    try:
        course = load_internal_course(run_dir)
        edited = apply_exercise_edit(
            course, req.module_index, req.lesson_index, req.exercise_index, req.exercise
        )
        report = save_course(run_dir, course)
    except EditError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _report_payload(report, edited)


@app.post("/api/runs/{run_id}/exercise/regenerate")
async def regenerate_exercise_endpoint(run_id: str, req: RegenerateRequest):
    """Regenerate one exercise with the LLM (subscription backend by default)."""
    from techlingo_workflow.editing import (
        EditError,
        load_internal_course,
        regenerate_exercise,
        save_course,
        _report_payload,
    )
    from techlingo_workflow.llm import LLMClient

    run_dir = _run_dir_or_404(run_id)
    backend_name, model_label, backend_error = _resolve_backend_for_request(
        {"backend": req.backend, "model_id": req.model_id}
    )
    if backend_error:
        raise HTTPException(status_code=422, detail=backend_error)
    try:
        course = load_internal_course(run_dir)
        llm = LLMClient(model_id=model_label, name="Question_Regenerator")
        regenerated = await regenerate_exercise(
            course,
            req.module_index,
            req.lesson_index,
            req.exercise_index,
            llm,
            req.instructions,
        )
        report = save_course(run_dir, course)
    except EditError as e:
        raise HTTPException(status_code=422, detail=str(e))
    payload = _report_payload(report, regenerated)
    payload["model_id"] = model_label
    return payload

@app.websocket("/ws/run")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        # 1. Wait for the initial configuration message
        data = await websocket.receive_text()
        request_data = json.loads(data)
        
        # Parse request safely
        input_text = request_data.get("input_text", "")
        # Handle config if provided
        config_dict = request_data.get("config")
        config = WorkflowConfig(**config_dict) if config_dict else WorkflowConfig()
        
        difficulty_str = request_data.get("difficulty")
        difficulty = DifficultyLevel(difficulty_str) if difficulty_str else config.difficulty
        
        override_title = request_data.get("title")
        course_key = request_data.get("course_key")

        backend_name, model_id, backend_error = _resolve_backend_for_request(request_data)
        if backend_error:
             await websocket.send_json({"type": "error", "message": backend_error})
             await websocket.close()
             return

        # 2. Setup Workflow
        out_dir = Path("outputs")
        run_id, run_dir = new_run_dir(out_dir)
        
        state = PipelineState(
            run_id=run_id,
            run_dir=str(run_dir),
            input_text=input_text,
            model_id=model_id,
            difficulty=difficulty,
            config=config,
            override_title=override_title,
        )
        
        workflow = build_techlingo_workflow()
        
        await websocket.send_json({
            "type": "start",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "backend": backend_name,
            "model_id": model_id,
            "config": config.model_dump(mode="json")
        })

        # 3. Run Workflow Helpers
        def _get_executor_id(evt: object) -> str | None:
            return (
                getattr(evt, "executor_id", None)
                or getattr(evt, "executorId", None)
                or getattr(evt, "ExecutorId", None)
            )

        output = None
        started_at: dict[str, float] = {}

        # 4. Stream Events
        async for evt in workflow.run_stream(state):
            name = evt.__class__.__name__
            executor_id = _get_executor_id(evt)
            ts = time.strftime("%H:%M:%S")

            if name == "StageLogEvent":
                msg = getattr(evt, "message", None)
                if msg:
                     print(msg, flush=True)
                     await websocket.send_json({"type": "log", "ts": ts, "message": msg})

            elif name in {"ExecutorInvokedEvent", "ExecutorInvokeEvent"} and executor_id:
                started_at[executor_id] = time.monotonic()
                await websocket.send_json({"type": "progress", "ts": ts, "event": "start", "executor": executor_id})

            elif name in {"ExecutorCompletedEvent", "ExecutorCompleteEvent"} and executor_id:
                duration = 0.0
                if executor_id in started_at:
                    duration = time.monotonic() - started_at[executor_id]
                await websocket.send_json({"type": "progress", "ts": ts, "event": "done", "executor": executor_id, "duration": duration})

            elif name == "ExecutorFailedEvent" and executor_id:
                details = getattr(evt, "details", None)
                msg = getattr(details, "message", None) if details is not None else None
                await websocket.send_json({"type": "error", "ts": ts, "executor": executor_id, "message": msg or "Unknown error"})

            elif name == "WorkflowOutputEvent":
                output = getattr(evt, "data", None)

            elif name == "WorkflowErrorEvent":
                exc = getattr(evt, "exception", None)
                await websocket.send_json({"type": "error", "message": f"Workflow failed: {exc}"})
        
        if output:
            # Save artifacts (similar to CLI)
            run_path = Path(output.run_dir)

            from techlingo_workflow.emit import emit_and_validate, slugify
            from techlingo_workflow.validate_techlingo import TechLingoValidationError

            validation_report = output.validation_report.model_dump(mode="json")

            # Rich internal model kept for viewers/debug; canonical course.json is
            # the TechLingo-native output the importer consumes.
            write_json(run_path / "course.internal.json", output.course.model_dump(mode="json"))
            write_json(run_path / "validation_report.json", validation_report)

            resolved_course_key = course_key or slugify(output.course.title, fallback="course")
            try:
                tl_course = emit_and_validate(output.course, course_key=resolved_course_key)
                write_json(run_path / "course.json", tl_course.model_dump(mode="json"))
            except TechLingoValidationError as e:
                await websocket.send_json({"type": "error", "message": f"TechLingo output invalid: {e}"})
                raise
            
            # Markdown generation
            md_lines: List[str] = []
            md_lines.append(f"# {output.course.title}")
            for mod in output.course.modules:
                md_lines.append(f"## {mod.title}")
                for lesson in mod.lessons:
                    md_lines.append(f"- **{lesson.title}** — {lesson.slo}")
            
            md_content = "\n".join(md_lines)
            write_text(run_path / "course.md", md_content + "\n")

            await websocket.send_json({
                "type": "complete",
                "run_id": output.run_id,
                # Internal (rich) course format — same shape the Run Viewer renders.
                "course": output.course.model_dump(mode="json"),
                "report": validation_report,
                "markdown": md_content
            })
        else:
            await websocket.send_json({"type": "error", "message": "Workflow finished but no output generated."})

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Error: {e}")
        # Only try to send if still open (this might fail if socket is closed)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass

@app.websocket("/ws/analyze")
async def websocket_analyze(websocket: WebSocket):
    await websocket.accept()
    run_id = f"analyze-{int(time.time())}"
    run_dir = f"outputs/{run_id}"
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(f"{run_dir}/artifacts", exist_ok=True)
    
    # Need to import Any for the context class
    from typing import Any

    try:
        # Receive init payload
        data = await websocket.receive_text()
        payload = json.loads(data)
        input_text = payload.get("input_text", "")

        if not input_text:
            await websocket.send_json({"type": "error", "message": "No input text provided"})
            await websocket.close()
            return

        # Send start event immediately to clear "Connecting..." status
        await websocket.send_json({
            "type": "start",
            "run_id": run_id,
            "run_dir": run_dir,
            "ts": time.strftime("%H:%M:%S")
        })
        
        await websocket.send_json({"type": "log", "ts": time.strftime("%H:%M:%S"), "message": "System: Connection established. Initializing agents..."})

        # Build Workflow (now cached/pre-built)
        await websocket.send_json({"type": "log", "ts": time.strftime("%H:%M:%S"), "message": "System: Loading agent workflow..."})
        workflow_graph = build_analysis_workflow()
        
        await websocket.send_json({"type": "log", "ts": time.strftime("%H:%M:%S"), "message": "System: Agent state initialized. Starting execution..."})

        backend_name, model_id, backend_error = _resolve_backend_for_request(payload)
        if backend_error:
            await websocket.send_json({"type": "error", "message": backend_error})
            await websocket.close()
            return

        # Initialize State
        state = PipelineState(
            run_id=run_id,
            run_dir=run_dir,
            input_text=input_text,
            model_id=model_id,
            config=WorkflowConfig(),
            difficulty=DifficultyLevel.beginner
        )

        # Run Workflow using run_stream (same as /ws/run)
        try:
            output = None
            
            # Helper to get executor ID safely
            def _get_executor_id(evt: object) -> str | None:
                return (
                    getattr(evt, "executor_id", None)
                    or getattr(evt, "executorId", None)
                    or getattr(evt, "ExecutorId", None)
                )

            async for evt in workflow_graph.run_stream(state):
                name = evt.__class__.__name__
                executor_id = _get_executor_id(evt)
                ts = time.strftime("%H:%M:%S")

                if name == "StageLogEvent":
                    msg = getattr(evt, "message", None)
                    if msg:
                         print(msg, flush=True)
                         await websocket.send_json({"type": "log", "ts": ts, "message": msg})

                elif name in {"ExecutorInvokedEvent", "ExecutorInvokeEvent"} and executor_id:
                    await websocket.send_json({"type": "progress", "ts": ts, "event": "start", "executor": executor_id})

                elif name in {"ExecutorCompletedEvent", "ExecutorCompleteEvent"} and executor_id:
                    await websocket.send_json({"type": "progress", "ts": ts, "event": "done", "executor": executor_id})

                elif name == "ExecutorFailedEvent" and executor_id:
                    details = getattr(evt, "details", None)
                    msg = getattr(details, "message", None) if details is not None else None
                    await websocket.send_json({"type": "error", "ts": ts, "executor": executor_id, "message": msg or "Unknown error"})

                elif name == "WorkflowOutputEvent":
                    output = getattr(evt, "data", None)

                elif name == "WorkflowErrorEvent":
                    exc = getattr(evt, "exception", None)
                    await websocket.send_json({"type": "error", "message": f"Workflow failed: {exc}"})

            # Send result back
            if output and (isinstance(output, TextAnalysisResult) or isinstance(output, dict)):
                 # Result might be a dict if returned directly from LLM, or model if typed
                 res_data = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
                 
                 await websocket.send_json({
                    "type": "complete",
                    "result": res_data,
                     "ts": time.strftime("%H:%M:%S")
                })
            else:
                 await websocket.send_json({"type": "error", "message": "Analysis failed to produce result."})

        except Exception as e:
            import traceback
            traceback.print_exc()
            await websocket.send_json({
                "type": "error",
                "message": str(e),
                "ts": time.strftime("%H:%M:%S")
            })
            
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WS Handling Error: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass
