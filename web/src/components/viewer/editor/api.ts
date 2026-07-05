import { Exercise } from "../types";

// FastAPI backend (same host the generator websocket uses).
const API_BASE = "http://localhost:8000";

export interface EditReport {
    ok: boolean;
    errors: { severity: string; path: string; message: string }[];
    warnings: { severity: string; path: string; message: string }[];
    exercise?: Exercise;
    model_id?: string;
}

async function request(path: string, method: string, body: unknown): Promise<EditReport> {
    let res: Response;
    try {
        res = await fetch(`${API_BASE}${path}`, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
    } catch {
        throw new Error(
            "Cannot reach the API server. Start it with: uvicorn server.main:app --port 8000"
        );
    }
    if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
            const data = await res.json();
            if (data?.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
        } catch { /* keep status text */ }
        throw new Error(detail);
    }
    return res.json();
}

export function saveExercise(
    runId: string,
    moduleIndex: number,
    lessonIndex: number,
    exerciseIndex: number,
    exercise: Exercise,
): Promise<EditReport> {
    return request(`/api/runs/${encodeURIComponent(runId)}/exercise`, "PUT", {
        module_index: moduleIndex,
        lesson_index: lessonIndex,
        exercise_index: exerciseIndex,
        exercise,
    });
}

export function regenerateExercise(
    runId: string,
    moduleIndex: number,
    lessonIndex: number,
    exerciseIndex: number,
    instructions?: string,
): Promise<EditReport> {
    return request(`/api/runs/${encodeURIComponent(runId)}/exercise/regenerate`, "POST", {
        module_index: moduleIndex,
        lesson_index: lessonIndex,
        exercise_index: exerciseIndex,
        instructions: instructions || null,
    });
}
