"use client";

import React, { useState, useEffect } from 'react';
import { RunInfo, getRuns, getCourse } from '../../app/actions/viewer';
import { RunSelector } from './RunSelector';
import { BrowseTab } from './BrowseTab';
import { QuizTab } from './QuizTab';
import { Course } from './types';
import { Tabs } from '@digdir/designsystemet-react';

interface Props {
    initialRuns: RunInfo[];
}

export const ViewerContainer: React.FC<Props> = ({ initialRuns }) => {
    const [runs, setRuns] = useState<RunInfo[]>(initialRuns);
    const [selectedRunId, setSelectedRunId] = useState<string>('');
    const [course, setCourse] = useState<Course | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [activeTab, setActiveTab] = useState("browse");

    // Helper to load course data
    useEffect(() => {
        if (!selectedRunId) {
            setCourse(null);
            return;
        }

        async function load() {
            setLoading(true);
            setError(null);
            try {
                const data = await getCourse(selectedRunId);
                if (data) {
                    setCourse(data);
                } else {
                    setError("Failed to load course data.");
                }
            } catch (err) {
                console.error(err);
                setError("An error occurred loading the course.");
            } finally {
                setLoading(false);
            }
        }

        load();
    }, [selectedRunId]);

    const seed = selectedRunId ? Math.abs(selectedRunId.split("").reduce((a, b) => { a = ((a << 5) - a) + b.charCodeAt(0); return a & a }, 0)) : 0;

    return (
        <div style={{ padding: '2rem', maxWidth: '1200px', margin: '0 auto' }}>
            <h1 style={{ marginBottom: '2rem' }}>Run Viewer</h1>

            <RunSelector
                runs={runs}
                selectedRunId={selectedRunId}
                onSelect={setSelectedRunId}
            />

            {error && (
                <div style={{ color: 'red', marginTop: '1rem', padding: '1rem', border: '1px solid currentColor', borderRadius: '4px' }}>
                    {error}
                </div>
            )}

            {loading && <div>Loading course data...</div>}

            {course && !loading && (
                <div style={{ marginTop: '2rem' }}>
                    <div style={{ marginBottom: '1rem', fontSize: '0.9rem', color: 'var(--color-text-subtle)' }}>
                        <strong>Topic:</strong> {course.topic || "Unknown"} •
                        <strong> Difficulty:</strong> {course.difficulty?.value || course.difficulty || "Unknown"} •
                        <strong> Modules:</strong> {course.modules?.length || 0}
                    </div>

                    <Tabs value={activeTab} onChange={(val) => setActiveTab(val)}>
                        <Tabs.List>
                            <Tabs.Tab value="browse">Browse Course</Tabs.Tab>
                            <Tabs.Tab value="quiz">Interactive Quiz</Tabs.Tab>
                        </Tabs.List>
                        <Tabs.Panel value="browse" style={{ paddingTop: '1.5rem' }}>
                            <BrowseTab course={course} />
                        </Tabs.Panel>
                        <Tabs.Panel value="quiz" style={{ paddingTop: '1.5rem' }}>
                            <QuizTab course={course} seed={seed} />
                        </Tabs.Panel>
                    </Tabs>
                </div>
            )}

            {!selectedRunId && (
                <div style={{ marginTop: '4rem', textAlign: 'center', color: 'var(--color-text-subtle)' }}>
                    Select a run from the dropdown above to view details.
                </div>
            )}
        </div>
    );
};
