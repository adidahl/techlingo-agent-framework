"use client";

import { useState, useRef, useEffect } from "react";
import styles from "./page.module.css";

type LogMessage = {
    ts: string;
    executor?: string;
    message?: string;
    event?: string;
    duration?: number;
    type: "log" | "progress" | "error" | "complete" | "start";
    run_id?: string;
    run_dir?: string;
};

export default function GeneratorPage() {
    const [inputText, setInputText] = useState("");
    const [moduleTitle, setModuleTitle] = useState("");
    const [difficulty, setDifficulty] = useState("beginner");
    const [configJson, setConfigJson] = useState(
        JSON.stringify(
            {
                difficulty: "beginner",
                modules_count: 1,
                min_lessons_total: 5,
                max_lessons_total: 10,
                exercises_per_lesson: 15,
                flashcards_per_lesson: 5,
                blooms_distribution: {
                    "Remembering": 3,
                    "Understanding": 4,
                    "Applying": 4,
                    "Analyzing/Evaluating": 4
                },
                question_type_distribution: {
                    "single_choice": 3,
                    "multi_choice": 3,
                    "true_false": 3,
                    "fill_gaps": 3,
                    "rearrange": 3
                }
            },
            null,
            4
        )
    );
    const [status, setStatus] = useState<"idle" | "running" | "completed" | "error">("idle");
    const [logs, setLogs] = useState<LogMessage[]>([]);
    const [result, setResult] = useState<any>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const logEndRef = useRef<HTMLDivElement>(null);

    // Auto-scroll logs
    useEffect(() => {
        logEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [logs]);

    const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file) {
            const text = await file.text();
            setInputText(text);
        }
    };

    const handleAnalyze = () => {
        if (!inputText) return;

        // Reset logs but keep status
        setLogs([]);
        setStatus("running");

        const ws = new WebSocket("ws://localhost:8000/ws/analyze");
        wsRef.current = ws;

        ws.onopen = () => {
            ws.send(JSON.stringify({ input_text: inputText }));
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "complete") {
                // Analysis complete
                setStatus("idle"); // Go back to idle so user can click Generate
                ws.close();

                // Update Config with recommended values
                if (data.result && data.result.recommended_config) {
                    setConfigJson(JSON.stringify(data.result.recommended_config, null, 4));
                    if (data.result.recommended_config.difficulty) {
                        setDifficulty(data.result.recommended_config.difficulty);
                    }
                    setLogs((prev) => [...prev, { type: "log", ts: data.ts, message: "✅ Config updated with AI recommendations!" }]);
                }
            } else {
                setLogs((prev) => [...prev, data]);
                if (data.type === "error") {
                    setStatus("error");
                }
            }
        };

        ws.onerror = (error) => {
            setLogs((prev) => [...prev, { type: "error", ts: "", message: "Analysis WebSocket connection failed." }]);
            setStatus("error");
        };
    };

    const handleRun = () => {
        if (!inputText) return;

        // Validation
        try {
            const config = JSON.parse(configJson);
            const exercisesCount = config.exercises_per_lesson;

            const bloomsSum = Object.values(config.blooms_distribution || {}).reduce((a: any, b: any) => a + b, 0);
            if (bloomsSum !== exercisesCount) {
                setLogs([{ type: "error", ts: "", message: `Validation Error: Bloom's distribution sum (${bloomsSum}) does not match exercises_per_lesson (${exercisesCount})` }]);
                setStatus("error");
                return;
            }

            const typesSum = Object.values(config.question_type_distribution || {}).reduce((a: any, b: any) => a + b, 0);
            if (typesSum !== exercisesCount) {
                setLogs([{ type: "error", ts: "", message: `Validation Error: Question type distribution sum (${typesSum}) does not match exercises_per_lesson (${exercisesCount})` }]);
                setStatus("error");
                return;
            }

        } catch (e) {
            setLogs([{ type: "error", ts: "", message: "Invalid JSON Config" }]);
            setStatus("error");
            return;
        }

        // Reset
        setLogs([]);
        setResult(null);
        setStatus("running");

        // Connect
        const ws = new WebSocket("ws://localhost:8000/ws/run");
        wsRef.current = ws;

        ws.onopen = () => {
            // Send init payload
            try {
                const config = JSON.parse(configJson);
                ws.send(JSON.stringify({
                    input_text: inputText,
                    config,
                    title: moduleTitle,
                    difficulty: difficulty
                }));
            } catch (err) {
                setLogs((prev) => [...prev, { type: "error", ts: "", message: "Invalid JSON Config" }]);
                ws.close();
                setStatus("error");
            }
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "complete") {
                setResult(data);
                setStatus("completed");
                ws.close();
            } else {
                setLogs((prev) => [...prev, data]);
                if (data.type === "error") {
                    // Don't close immediately, let user see error
                    setStatus("error");
                }
            }
        };

        ws.onerror = (error) => {
            console.error("WebSocket error:", error);
            setLogs((prev) => [...prev, { type: "error", ts: "", message: "WebSocket connection failed. Is the server running?" }]);
            setStatus("error");
        };

        ws.onclose = () => {
            if (status === "running") {
                // If closed unexpectedly
            }
        };
    };

    return (
        <div className={styles.container}>
            <h1 className={styles.title}>TechLingo Generator</h1>

            <div className={styles.section}>
                <label className={styles.label}>1. Select Input File (Text)</label>
                <input type="file" accept=".txt,.md" onChange={handleFileUpload} />
                <br />
                <br />
                <label className={styles.label}>Or paste text:</label>
                <textarea
                    className={styles.textarea}
                    rows={5}
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                    placeholder="Paste course source material here..."
                />
            </div>

            <div className={styles.section}>
                <label className={styles.label}>2. Module Settings</label>
                <div style={{ display: "flex", gap: "20px", flexDirection: "row", flexWrap: "wrap" }}>
                    <div style={{ flex: 1, minWidth: "200px" }}>
                        <label style={{ display: "block", marginBottom: "5px", fontSize: "0.9rem", color: "#666" }}>Module Title (Optional Override)</label>
                        <input
                            type="text"
                            value={moduleTitle}
                            onChange={(e) => setModuleTitle(e.target.value)}
                            placeholder="e.g. AI Core Capabilities"
                            style={{
                                width: "100%",
                                padding: "10px",
                                border: "1px solid #ddd",
                                borderRadius: "4px",
                                fontSize: "1rem"
                            }}
                        />
                    </div>
                    <div style={{ width: "200px" }}>
                        <label style={{ display: "block", marginBottom: "5px", fontSize: "0.9rem", color: "#666" }}>Difficulty</label>
                        <select
                            value={difficulty}
                            onChange={(e) => setDifficulty(e.target.value)}
                            style={{
                                width: "100%",
                                padding: "10px",
                                border: "1px solid #ddd",
                                borderRadius: "4px",
                                fontSize: "1rem",
                                backgroundColor: "white"
                            }}
                        >
                            <option value="novice">Novice</option>
                            <option value="beginner">Beginner</option>
                            <option value="intermediate">Intermediate</option>
                            <option value="advanced">Advanced</option>
                        </select>
                    </div>
                </div>
            </div>

            <div className={styles.section}>
                <label className={styles.label}>3. Workflow Configuration (JSON)</label>
                <div style={{ marginBottom: "10px" }}>
                    <button
                        className={styles.button}
                        onClick={handleAnalyze}
                        disabled={status === "running" || !inputText}
                        style={{ marginRight: "10px", backgroundColor: "#2196F3" }}
                    >
                        Analyze & Recommend Config
                    </button>
                    <small style={{ color: "#666" }}>AI will analyze text and suggest optimal settings.</small>
                </div>
                <textarea
                    className={styles.textarea}
                    rows={8}
                    value={configJson}
                    onChange={(e) => setConfigJson(e.target.value)}
                />
            </div>

            <button className={styles.button} onClick={handleRun} disabled={status === "running" || !inputText}>
                {status === "running" ? "Generating..." : "Generate Course"}
            </button>

            {status !== "idle" && (
                <div className={styles.section} style={{ marginTop: "2rem" }}>
                    <label className={styles.label}>Progress Log</label>
                    <div className={styles.logs}>
                        {logs.map((log, i) => (
                            <div key={i}>
                                <span style={{ color: "#888", marginRight: "10px" }}>{log.ts}</span>
                                {log.type === "progress" && (
                                    <span>{log.event === "start" ? "🚀 START" : "✅ DONE "} {log.executor} {log.duration ? `(${log.duration.toFixed(1)}s)` : ""}</span>
                                )}
                                {log.type === "log" && (
                                    <span>{log.message}</span>
                                )}
                                {log.type === "error" && (
                                    <span style={{ color: "red" }}>❌ {log.executor ? `${log.executor}: ` : ""}{log.message}</span>
                                )}
                                {log.type === "start" && (
                                    <span style={{ color: "cyan" }}>Run Started: {log.run_id} ({log.run_dir})</span>
                                )}
                            </div>
                        ))}
                        <div ref={logEndRef} />
                    </div>
                </div>
            )}

            {result && (
                <div className={styles.result}>
                    <h2>Generation Complete!</h2>
                    <div className={styles.markdown}>
                        <pre style={{ whiteSpace: "pre-wrap" }}>{result.markdown}</pre>
                    </div>
                </div>
            )}
        </div>
    );
}
