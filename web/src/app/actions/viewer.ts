"use server";

import fs from "fs/promises";
import path from "path";

// Define the root of the project relative to this file
// deeper in web/src/app/actions -> ../../../..
// But simpler to just use process.cwd() which usually points to 'web' root when running next
// The outputs folder is in the parent of 'web', so '../outputs' from web root.
const OUTPUTS_DIR = path.resolve(process.cwd(), "../outputs");

export type RunInfo = {
    id: string; // Folder name
    name: string; // Folder name
    path: string; // Absolute path
};

export type CourseData = any; // We'll rely on the existing JSON structure

export async function getRuns(): Promise<RunInfo[]> {
    try {
        // Check if directory exists
        try {
            await fs.access(OUTPUTS_DIR);
        } catch {
            console.warn(`Outputs directory not found at ${OUTPUTS_DIR}`);
            return [];
        }

        const entries = await fs.readdir(OUTPUTS_DIR, { withFileTypes: true });

        const runs = entries
            .filter((e) => e.isDirectory() && e.name.startsWith("run-"))
            .map((e) => ({
                id: e.name,
                name: e.name,
                path: path.join(OUTPUTS_DIR, e.name),
            }))
            .sort((a, b) => b.name.localeCompare(a.name)); // Sort by name descending (newest first)

        return runs;
    } catch (error) {
        console.error("Error listing runs:", error);
        return [];
    }
}

export async function getCourse(runId: string): Promise<CourseData | null> {
    const runPath = path.join(OUTPUTS_DIR, runId);
    const coursePath = path.join(runPath, "course.json");

    try {
        const data = await fs.readFile(coursePath, "utf-8");
        return JSON.parse(data);
    } catch (error) {
        console.error(`Error loading course for run ${runId}:`, error);
        return null;
    }
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
