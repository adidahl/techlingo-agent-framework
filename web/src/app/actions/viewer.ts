"use server";

import fs from "fs/promises";
import path from "path";
import type { Course } from "../../components/viewer/types";

// Define the root of the project relative to this file
// deeper in web/src/app/actions -> ../../../..
// But simpler to just use process.cwd() which usually points to 'web' root when running next
// The outputs folder is in the parent of 'web', so '../outputs' from web root.
const OUTPUTS_DIR = path.resolve(process.cwd(), "../outputs");
const COURSES_DIR = path.resolve(process.cwd(), "../courses");

export type RunInfo = {
    id: string; // Folder name
    name: string; // Folder name
    path: string; // Absolute path
};

export type CourseData = Course;

const SAFE_PATH_SEGMENT = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;

interface CompiledExerciseOptions {
    [key: string]: unknown;
    blooms_level?: string;
    original_question_type?: string;
    concept_id?: string;
    options?: unknown[];
    feedback_for_incorrect?: unknown;
    parts?: unknown[];
    word_bank?: string[];
    correct_order?: string[];
    interchangeable_groups?: number[][];
}

interface CompiledExercise {
    [key: string]: unknown;
    question_type: string;
    question_text?: string;
    options?: CompiledExerciseOptions;
    correct_answer?: unknown;
    explanation?: string;
}

interface CompiledLesson {
    [key: string]: unknown;
    flashcards?: unknown[];
    exercises?: CompiledExercise[];
}

interface CompiledModule {
    [key: string]: unknown;
    lessons?: CompiledLesson[];
}

interface CompiledCourse {
    schema_version: string;
    title: string;
    difficulty: unknown;
    modules?: CompiledModule[];
}

function bundleId(courseId: string, version: string): string {
    return `bundle:${courseId}:${version}`;
}

function parseBundleId(id: string): { courseId: string; version: string } | null {
    const [prefix, courseId, version, ...extra] = id.split(":");
    if (
        prefix !== "bundle" || extra.length > 0 ||
        !courseId || !version ||
        !SAFE_PATH_SEGMENT.test(courseId) || !SAFE_PATH_SEGMENT.test(version)
    ) return null;
    return { courseId, version };
}

/** Convert the compiled TechLingo-native bundle into the rich shape used by
 * the authoring viewer. The question content and order are unchanged; only
 * field names/types are adapted for the existing React question controls. */
function compiledCourseForViewer(course: CompiledCourse): CourseData {
    return {
        schema_version: course.schema_version,
        title: course.title,
        difficulty: course.difficulty,
        modules: (course.modules || []).map((module) => ({
            ...module,
            lessons: (module.lessons || []).map((lesson) => ({
                ...lesson,
                flashcards: lesson.flashcards || [],
                exercises: (lesson.exercises || []).map((exercise) => {
                    const metadata = exercise.options || {};
                    const base = {
                        blooms_level: metadata.blooms_level || "Unknown",
                        question_type: metadata.original_question_type || exercise.question_type,
                        prompt: exercise.question_text || "",
                        concept_id: metadata.concept_id,
                        feedback_for_correct: exercise.explanation || null,
                    };

                    switch (exercise.question_type) {
                        case "multiple_choice":
                            return { ...base, options: metadata.options || [] };
                        case "true_false":
                            return {
                                ...base,
                                statement: exercise.question_text || "",
                                correct_answer: String(exercise.correct_answer).toLowerCase() === "true",
                                feedback_for_incorrect: metadata.feedback_for_incorrect || null,
                            };
                        case "fill_blank":
                            return {
                                ...base,
                                parts: metadata.parts || [],
                                explanation: exercise.explanation || null,
                                feedback_for_incorrect: metadata.feedback_for_incorrect || null,
                            };
                        case "arrange_sentence":
                            return {
                                ...base,
                                word_bank: metadata.word_bank || [],
                                correct_order: metadata.correct_order || [],
                                interchangeable_groups: metadata.interchangeable_groups || [],
                                explanation: exercise.explanation || null,
                                feedback_for_incorrect: metadata.feedback_for_incorrect || null,
                            };
                        default:
                            return base;
                    }
                }),
            })),
        })),
    } as unknown as CourseData;
}

export async function getRuns(): Promise<RunInfo[]> {
    try {
        const runs: RunInfo[] = [];

        // Compiled workspace bundles are directly playable—no import or
        // synthetic outputs/run-* folder is needed.
        try {
            const courseDirs = await fs.readdir(COURSES_DIR, { withFileTypes: true });
            for (const courseEntry of courseDirs.filter((entry) => entry.isDirectory())) {
                const distDir = path.join(COURSES_DIR, courseEntry.name, "dist");
                let versions: import("fs").Dirent[] = [];
                try {
                    versions = await fs.readdir(distDir, { withFileTypes: true });
                } catch {
                    continue;
                }
                for (const versionEntry of versions.filter((entry) => entry.isDirectory())) {
                    const flatPath = path.join(distDir, versionEntry.name, "course.flat.json");
                    try {
                        const raw = JSON.parse(await fs.readFile(flatPath, "utf-8"));
                        runs.push({
                            id: bundleId(courseEntry.name, versionEntry.name),
                            name: `${raw.title || courseEntry.name} (${versionEntry.name})`,
                            path: flatPath,
                        });
                    } catch {
                        // Not a complete/readable bundle.
                    }
                }
            }
        } catch {
            // A repository without workspaces can still show ordinary runs.
        }

        try {
            const entries = await fs.readdir(OUTPUTS_DIR, { withFileTypes: true });
            runs.push(...entries
                // Symlinked run dirs count too (e.g. a course-workspace build dir
                // bridged into outputs/ for review); Dirent reports the link
                // itself, not its target, so isDirectory() alone would hide them.
                .filter((e) => (e.isDirectory() || e.isSymbolicLink()) && e.name.startsWith("run-"))
                .map((e) => ({
                    id: e.name,
                    name: e.name,
                    path: path.join(OUTPUTS_DIR, e.name),
                }))
            );
        } catch {
            // outputs/ is optional when only compiled bundles are viewed.
        }

        return runs.sort((a, b) => {
            const aBundle = a.id.startsWith("bundle:") ? 1 : 0;
            const bBundle = b.id.startsWith("bundle:") ? 1 : 0;
            return bBundle - aBundle || b.name.localeCompare(a.name);
        });
    } catch (error) {
        console.error("Error listing runs:", error);
        return [];
    }
}

export async function getCourse(runId: string): Promise<CourseData | null> {
    const bundle = parseBundleId(runId);
    if (bundle) {
        const flatPath = path.join(
            COURSES_DIR,
            bundle.courseId,
            "dist",
            bundle.version,
            "course.flat.json",
        );
        try {
            return compiledCourseForViewer(JSON.parse(await fs.readFile(flatPath, "utf-8")));
        } catch (error) {
            console.error(`Error loading compiled bundle ${runId}:`, error);
            return null;
        }
    }

    const runPath = path.join(OUTPUTS_DIR, runId);
    // The viewer renders the rich internal format. New runs write it to
    // course.internal.json (course.json is now the TechLingo-native output);
    // older runs only have the internal format in course.json.
    const candidates = [
        path.join(runPath, "course.internal.json"),
        path.join(runPath, "course.json"),
    ];

    for (const candidate of candidates) {
        try {
            const data = await fs.readFile(candidate, "utf-8");
            return JSON.parse(data);
        } catch {
            // try next candidate
        }
    }
    console.error(`Error loading course for run ${runId}: no readable course file`);
    return null;
}

export async function getArtifacts(runId: string): Promise<string[]> {
    const artifactsDir = path.join(OUTPUTS_DIR, runId, "artifacts");
    try {
        await fs.access(artifactsDir);
        const entries = await fs.readdir(artifactsDir);
        return entries.filter(f => f.endsWith(".json"));
    } catch {
        return [];
    }
}

export async function getArtifactContent(runId: string, filename: string): Promise<string | null> {
    const filePath = path.join(OUTPUTS_DIR, runId, "artifacts", filename);
    try {
        return await fs.readFile(filePath, "utf-8");
    } catch (error) {
        console.error(`Error reading artifact ${filename}:`, error);
        return null;
    }
}
