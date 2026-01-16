"use client";

import { useState, useRef, useCallback, useEffect } from "react";

export type GeneratorStatus =
    | "idle"
    | "connecting"
    | "running"
    | "completed"
    | "error";

export type GeneratorGenericEvent = {
    type: "log" | "progress" | "start" | "complete" | "error";
    [key: string]: any;
};

export function useGenerator() {
    const [status, setStatus] = useState<GeneratorStatus>("idle");
    const [logs, setLogs] = useState<string[]>([]);
    const [result, setResult] = useState<any>(null);
    const [error, setError] = useState<string | null>(null);
    const wsRef = useRef<WebSocket | null>(null);

    const startGenerator = useCallback(
        (payload: {
            input_text: string;
            difficulty: string;
            model_id?: string;
            title?: string;
            config?: any; // WorkflowConfig object
        }) => {
            setStatus("connecting");
            setLogs([]);
            setResult(null);
            setError(null);

            // In development, backend is likely on 8000 while frontend is 3000
            const wsUrl = "ws://localhost:8000/ws/run";

            const ws = new WebSocket(wsUrl);
            wsRef.current = ws;

            ws.onopen = () => {
                setStatus("running");
                ws.send(JSON.stringify(payload));
            };

            ws.onmessage = (event) => {
                try {
                    const data: GeneratorGenericEvent = JSON.parse(event.data);

                    switch (data.type) {
                        case "log":
                            if (data.message) {
                                setLogs((prev) => [...prev, `[${data.ts || "LOG"}] ${data.message}`]);
                            }
                            break;
                        case "progress":
                            // Optional: handle structured progress if needed
                            break;
                        case "complete":
                            setStatus("completed");
                            setResult({
                                course: data.course,
                                report: data.report,
                                markdown: data.markdown,
                                run_id: data.run_id
                            });
                            ws.close();
                            break;
                        case "error":
                            setError(data.message || "Unknown error occurred");
                            setStatus("error");
                            setLogs((prev) => [...prev, `[ERROR] ${data.message}`]);
                            ws.close();
                            break;
                    }
                } catch (err) {
                    console.error("Failed to parse WebSocket message", err);
                }
            };

            ws.onerror = (e) => {
                console.error("WebSocket error", e);
                setError("Connection failed");
                setStatus("error");
            };

            ws.onclose = () => {
                if (status === "running") {
                    // If closed unexpectedly while running and not completed/error
                    // setStatus("error"); 
                    // (Optional: logic here depends on backend close behavior)
                }
            };
        },
        [status]
    );

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            if (wsRef.current) {
                wsRef.current.close();
            }
        };
    }, []);

    return {
        status,
        logs,
        result,
        error,
        startGenerator,
    };
}
