"use client";

import { useState, useRef, useCallback, useEffect } from "react";

export type AnalyzeStatus = "idle" | "analyzing" | "completed" | "error";

export function useAnalyzer() {
    const [status, setStatus] = useState<AnalyzeStatus>("idle");
    const [result, setResult] = useState<any>(null);
    const [error, setError] = useState<string | null>(null);
    const [progress, setProgress] = useState(0);
    const [currentStep, setCurrentStep] = useState<string | null>(null);
    const [logs, setLogs] = useState<string[]>([]);
    const wsRef = useRef<WebSocket | null>(null);

    const analyzeText = useCallback((text: string) => {
        setStatus("analyzing");
        setResult(null);
        setError(null);
        setProgress(0);
        setCurrentStep("Connecting...");
        setLogs([]);

        const wsUrl = "ws://localhost:8000/ws/analyze";
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
            ws.send(JSON.stringify({ input_text: text }));
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "complete") {
                    setResult(data.result);
                    setStatus("completed");
                    setProgress(100);
                    setCurrentStep("Analysis Complete");
                    ws.close();
                } else if (data.type === "start") {
                    setCurrentStep("Initializing workflow...");
                    setProgress(5);
                } else if (data.type === "error") {
                    setError(data.message);
                    setStatus("error");
                    ws.close();
                } else if (data.type === "log") {
                    setLogs(prev => [...prev, `[${data.ts || "LOG"}] ${data.message}`]);
                } else if (data.type === "progress") {
                    if (data.event === "start") {
                        if (data.executor === "text_analyzer") {
                            setCurrentStep("Step 1/2: Analyzing content...");
                            setProgress(10);
                        } else if (data.executor === "text_reviewer") {
                            setCurrentStep("Step 2/2: Reviewing and refining...");
                            setProgress(50);
                        }
                    } else if (data.event === "done") {
                        if (data.executor === "text_analyzer") {
                            setProgress(50);
                        } else if (data.executor === "text_reviewer") {
                            setProgress(90);
                        }
                    }
                }
            } catch (err) {
                console.error("Failed to parse analyze message", err);
            }
        };

        ws.onerror = (e) => {
            console.error("Analysis WS error", e);
            setError("Analysis connection failed");
            setStatus("error");
        };
    }, []);

    useEffect(() => {
        return () => {
            if (wsRef.current) wsRef.current.close();
        };
    }, []);

    return { status, result, error, progress, currentStep, logs, analyzeText };
}
