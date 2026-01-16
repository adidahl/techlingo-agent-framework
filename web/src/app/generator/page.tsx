"use client";

import { useState } from "react";
import {
    Button,
    Textfield,
    Select,
    Label,
    Heading,
    Paragraph,
} from "@digdir/designsystemet-react";
import { useGenerator } from "../../hooks/useGenerator";
import Console from "../../components/Console";
import styles from "../layout.module.css";
import { Search, Check, Upload, Download, Zap } from "lucide-react";

import { useAnalyzer } from "../../hooks/useAnalyzer";

export default function GeneratorPage() {
    const { status, logs, startGenerator, result, error } = useGenerator();
    const {
        status: analyzeStatus,
        result: analysisResult,
        error: analyzeError,
        progress: analyzeProgress,
        currentStep: analyzeStep,
        logs: analyzeLogs,
        analyzeText
    } = useAnalyzer();

    const [topic, setTopic] = useState("");
    const [title, setTitle] = useState("");
    const [difficulty, setDifficulty] = useState("beginner");

    const [showAnalyzeLogs, setShowAnalyzeLogs] = useState(false);

    const defaultTemplate = {
        difficulty: "beginner",
        modules_count: 1,
        min_lessons_total: 1,
        max_lessons_total: 1,
        exercises_per_lesson: 5,
        flashcards_per_lesson: 3,
        blooms_distribution: {
            "Remembering": 2,
            "Understanding": 1,
            "Applying": 1,
            "Analyzing/Evaluating": 1
        },
        question_type_distribution: {
            "single_choice": 1,
            "multi_choice": 1,
            "true_false": 1,
            "fill_gaps": 1,
            "rearrange": 1
        }
    };

    // Config state - initialized with default template
    const [config, setConfig] = useState<any>(defaultTemplate);
    const [configJson, setConfigJson] = useState(JSON.stringify(defaultTemplate, null, 2));
    const [configFileName, setConfigFileName] = useState("Default Template");
    const [jsonError, setJsonError] = useState<string | null>(null);

    // Update both object and text when external sources (upload, analysis, template) change config
    const updateConfigCompletely = (newConfig: any, sourceName: string) => {
        setConfig(newConfig);
        setConfigJson(JSON.stringify(newConfig, null, 2));
        setConfigFileName(sourceName);
        setJsonError(null);
    };

    const handleConfigUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (event) => {
            try {
                const json = JSON.parse(event.target?.result as string);
                updateConfigCompletely(json, file.name);
            } catch (err) {
                alert("Invalid JSON file");
            }
        };
        reader.readAsText(file);
    };

    const handleTextFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (event) => {
            setTopic(event.target?.result as string);
        };
        reader.readAsText(file);
    };

    const handleDownloadTemplate = () => {
        // Use the current config if valid, or fallback to default
        const templateToDownload = config || defaultTemplate;

        const blob = new Blob([JSON.stringify(templateToDownload, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "workflow_config.json";
        a.click();
        URL.revokeObjectURL(url);
        URL.revokeObjectURL(url);
    };

    const handleAnalyze = () => {
        if (!topic.trim()) return;
        // Don't auto-show logs to avoid jumping the page
        analyzeText(topic);
    };

    const applyRecommendedConfig = () => {
        if (analysisResult?.recommended_config) {
            updateConfigCompletely(analysisResult.recommended_config, "Recommended Config (Applied)");
        }
    };

    const handleDifficultyChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
        const val = e.target.value;
        setDifficulty(val);

        // Sync with config if it exists
        if (config) {
            const updated = { ...config, difficulty: val };
            setConfig(updated);
            setConfigJson(JSON.stringify(updated, null, 2));
        }
    };

    const handleJsonChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        const newValue = e.target.value;
        setConfigJson(newValue);
        try {
            if (newValue.trim() === "") {
                setConfig(null);
                setJsonError(null);
            } else {
                const parsed = JSON.parse(newValue);
                setConfig(parsed);
                setJsonError(null);
            }
        } catch (err) {
            setJsonError("Invalid JSON syntax");
        }
    };

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!topic.trim()) return;
        if (jsonError) {
            alert("Please fix JSON errors in config before generating.");
            return;
        }
        startGenerator({
            input_text: topic,
            difficulty: difficulty,
            title: title || undefined,
            config: config || undefined
        });
    };

    const isRunning = status === "connecting" || status === "running";
    const isAnalyzing = analyzeStatus === "analyzing";

    return (
        <div style={{ maxWidth: "1200px", margin: "0 auto", width: "100%" }}>
            <div className="card">
                <Heading level={1} data-size="xl" style={{ marginBottom: "var(--ds-spacing-2)" }}>
                    Course Generator
                </Heading>
                <Paragraph style={{ marginBottom: "var(--ds-spacing-8)", color: "var(--color-text-secondary)" }}>
                    Create interactive courses tailored to your specific needs.
                </Paragraph>

                <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "2rem" }}>

                    {/* Top Row: Input & Analysis */}
                    <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "2rem" }}>
                        <div>
                            <Textfield
                                aria-label="Topic or Text"
                                placeholder="Paste your content or describe the topic..."
                                value={topic}
                                onChange={(e) => setTopic(e.target.value)}
                                disabled={isRunning || isAnalyzing}
                                required
                                rows={8}
                                multiline
                                style={{ borderRadius: "12px" }}
                            />

                            <div style={{ marginTop: "1rem", display: "flex", gap: "1rem" }}>
                                <Button
                                    type="button"
                                    onClick={handleAnalyze}
                                    disabled={isRunning || isAnalyzing || !topic.trim()}
                                    className="btn-lime" // Custom green class
                                    style={{ borderRadius: "12px", padding: "0.5rem 1.5rem", display: "flex", alignItems: "center", gap: "0.5rem" }}
                                >
                                    <Search size={18} />
                                    {isAnalyzing ? "Analyzing..." : "Analyze Content"}
                                </Button>

                                <div style={{ position: "relative", overflow: "hidden" }}>
                                    <Button type="button" disabled={isRunning || isAnalyzing} className="btn-orange" style={{ borderRadius: "12px", padding: "0.5rem 1.5rem", display: "flex", alignItems: "center", gap: "0.5rem" }}>
                                        <Upload size={18} />
                                        Upload File (.txt, .md)
                                    </Button>
                                    <input
                                        type="file"
                                        accept=".txt,.md"
                                        onChange={handleTextFileUpload}
                                        disabled={isRunning || isAnalyzing}
                                        style={{ position: "absolute", left: 0, top: 0, opacity: 0, cursor: "pointer", height: "100%", width: "100%" }}
                                    />
                                </div>
                            </div>


                            {analyzeError && <p style={{ color: "red", fontSize: "0.9rem", marginTop: "0.5rem" }}>{analyzeError}</p>}
                        </div>

                        {/* Analysis Card */}
                        {analysisResult && (
                            <div style={{ padding: "1.5rem", borderRadius: "16px", backgroundColor: "#F8FAC3", color: "#0B2318" }}>
                                <Heading level={3} data-size="xs" style={{ marginBottom: "1rem" }}>Analysis Insights</Heading>
                                <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", fontSize: "0.9rem", marginBottom: "1rem" }}>
                                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                                        <span>Parts Found:</span>
                                        <strong>{analysisResult.metadata.total_parts}</strong>
                                    </div>
                                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                                        <span>Est. Questions:</span>
                                        <strong>{analysisResult.metadata.estimated_questions_needed}</strong>
                                    </div>
                                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                                        <span>Completeness:</span>
                                        <strong>{Math.round(analysisResult.metadata.completeness_score * 100)}%</strong>
                                    </div>
                                </div>
                                <Button
                                    type="button"
                                    onClick={applyRecommendedConfig}
                                    disabled={isRunning || isAnalyzing}
                                    className="btn-lime"
                                    style={{ width: "100%", borderRadius: "12px", display: "flex", alignItems: "center", justifyContent: "center", gap: "0.5rem" }}
                                >
                                    <Check size={18} />
                                    Apply Settings
                                </Button>
                            </div>
                        )}
                    </div>

                    {/* Combined Analysis Feedback Block */}
                    {(isAnalyzing || analyzeLogs.length > 0) && (
                        <div style={{
                            marginTop: "1.5rem",
                            padding: "1rem",
                            backgroundColor: "#F9FAFB",
                            borderRadius: "16px",
                            border: "1px solid #E5E7EB"
                        }}>
                            {/* Progress UI */}
                            {isAnalyzing && (
                                <div style={{ marginBottom: analyzeLogs.length > 0 ? "1rem" : "0" }}>
                                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                                        <span style={{ fontSize: "0.85rem", fontWeight: 600, color: "#374151" }}>{analyzeStep}</span>
                                        <span style={{ fontSize: "0.8rem", color: "#6B7280" }}>{analyzeProgress}%</span>
                                    </div>
                                    <div className="progress-container" style={{ margin: "0", height: "8px" }}>
                                        <div className="progress-bar" style={{ width: `${analyzeProgress}%` }} />
                                    </div>
                                </div>
                            )}

                            {/* Logs Toggle */}
                            {analyzeLogs.length > 0 && (
                                <div>
                                    <div
                                        onClick={() => setShowAnalyzeLogs(!showAnalyzeLogs)}
                                        style={{
                                            display: "flex",
                                            alignItems: "center",
                                            gap: "0.5rem",
                                            cursor: "pointer",
                                            fontWeight: 600,
                                            fontSize: "0.85rem",
                                            color: "#6B7280"
                                        }}
                                    >
                                        <span>{showAnalyzeLogs ? "▼ Hide Detailed Logs" : "▶ Show Detailed Logs"}</span>
                                    </div>
                                    {showAnalyzeLogs && (
                                        <div style={{
                                            height: "200px",
                                            overflow: "hidden",
                                            borderRadius: "8px",
                                            marginTop: "0.75rem",
                                            position: "relative"
                                        }}>
                                            <Console logs={analyzeLogs} />
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    )}

                    <div style={{ borderTop: "1px solid #eee", paddingTop: "2rem", display: "flex", flexDirection: "column", gap: "1.5rem" }}>
                        <div>
                            <Label htmlFor="course-title" id="label-title" style={{ marginBottom: "var(--ds-spacing-2)" }}>Course Title (Optional)</Label>
                            <Textfield
                                id="course-title"
                                aria-labelledby="label-title"
                                value={title}
                                onChange={(e) => setTitle(e.target.value)}
                                disabled={isRunning}
                            />
                        </div>
                        <div>
                            <Label htmlFor="difficulty-select" style={{ marginBottom: "var(--ds-spacing-2)" }}>Difficulty Level</Label>
                            <Select
                                id="difficulty-select"
                                value={difficulty}
                                onChange={handleDifficultyChange}
                                disabled={isRunning || isAnalyzing}
                            >
                                <Select.Option value="novice">Novice</Select.Option>
                                <Select.Option value="beginner">Beginner</Select.Option>
                                <Select.Option value="intermediate">Intermediate</Select.Option>
                                <Select.Option value="advanced">Advanced</Select.Option>
                            </Select>
                        </div>
                    </div>


                    {/* Configuration */}
                    <div style={{ borderTop: "1px solid #eee", paddingTop: "2rem" }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
                            <Heading level={3} data-size="sm">Workflow Configuration</Heading>
                            <div style={{ display: "flex", gap: "1rem", alignItems: "center" }}>
                                <div style={{ position: "relative", overflow: "hidden" }}>
                                    <Button type="button" disabled={isRunning || isAnalyzing} className="btn-orange" data-size="sm" style={{ borderRadius: "10px", display: "flex", alignItems: "center", gap: "0.5rem" }}>
                                        <Upload size={16} />
                                        Upload JSON
                                    </Button>
                                    <input
                                        type="file"
                                        accept=".json"
                                        onChange={handleConfigUpload}
                                        disabled={isRunning || isAnalyzing}
                                        style={{ position: "absolute", left: 0, top: 0, opacity: 0, cursor: "pointer", height: "100%", width: "100%" }}
                                    />
                                </div>
                                <Button type="button" disabled={isRunning || isAnalyzing} className="btn-orange" data-size="sm" onClick={handleDownloadTemplate} style={{ borderRadius: "10px", display: "flex", alignItems: "center", gap: "0.5rem" }}>
                                    <Download size={16} />
                                    Download Template
                                </Button>
                            </div>
                        </div>

                        <Textfield
                            aria-label="Config Editor"
                            value={configJson}
                            onChange={handleJsonChange}
                            disabled={isRunning}
                            multiline
                            rows={8}
                            style={{
                                fontFamily: 'monospace',
                                fontSize: '0.85rem',
                                borderRadius: '12px',
                                border: jsonError ? "1px solid red" : "1px solid #eee"
                            }}
                        />
                        {configFileName && (
                            <div style={{ fontSize: "0.8rem", color: "#888", marginTop: "0.5rem", fontStyle: "italic" }}>
                                Values from: {configFileName}
                            </div>
                        )}
                    </div>

                    <div style={{ marginTop: "1rem" }}>
                        <Button
                            type="submit"
                            disabled={isRunning || isAnalyzing || !topic || !!jsonError}
                            className="btn-lime"
                            style={{ padding: "0.75rem 3rem", fontSize: "1rem", borderRadius: "12px", display: "flex", alignItems: "center", gap: "0.75rem" }}
                        >
                            <Zap size={20} fill="currentColor" />
                            {isRunning ? "Generating Course..." : "Generate Course"}
                        </Button>
                    </div>
                </form>

                {(isRunning || status === "completed" || status === "error") && (
                    <div style={{ marginTop: "3rem", padding: "1.5rem", borderTop: "1px solid #eee" }}>
                        <Heading level={2} data-size="sm" style={{ marginBottom: "1rem" }}>
                            System Logs
                        </Heading>
                        {error && <div style={{ color: "#FF6B6B", marginBottom: "1rem" }}>{error}</div>}
                        <div style={{ height: "400px", borderRadius: "12px", overflow: "hidden" }}>
                            <Console logs={logs} />
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
