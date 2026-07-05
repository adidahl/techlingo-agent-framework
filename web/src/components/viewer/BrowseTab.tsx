import React, { useState } from 'react';
import { Course, Exercise } from './types';
import { ExerciseRenderer } from './ExerciseRenderer';
import { StyledSelect, StyledButton, Card } from './Styled';
import { ExerciseEditor } from './editor/ExerciseEditor';
import { saveExercise, regenerateExercise, EditReport } from './editor/api';

interface Props {
    course: Course;
    runId?: string;
    onCourseChanged?: () => Promise<void> | void;
}

interface BannerState {
    kind: 'ok' | 'error';
    text: string;
    details?: string[];
}

export const BrowseTab: React.FC<Props> = ({ course, runId, onCourseChanged }) => {
    const [selectedModuleIdx, setSelectedModuleIdx] = useState<number>(0);
    const [selectedLessonIdx, setSelectedLessonIdx] = useState<number>(0);
    const [editingIdx, setEditingIdx] = useState<number | null>(null);
    const [regenIdx, setRegenIdx] = useState<number | null>(null);
    const [regenNote, setRegenNote] = useState('');
    const [busy, setBusy] = useState<'save' | 'regen' | null>(null);
    const [banner, setBanner] = useState<BannerState | null>(null);

    const modules = course.modules || [];
    const currentModule = modules[selectedModuleIdx];
    const lessons = currentModule?.lessons || [];
    const currentLesson = lessons[selectedLessonIdx];
    const editable = Boolean(runId);

    if (!modules.length) return <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--color-text-secondary)' }}>No modules in this course.</div>;

    const reportBanner = (report: EditReport, action: string) => {
        if (report.ok) {
            const warn = report.warnings.length ? ` (${report.warnings.length} warning(s))` : '';
            setBanner({ kind: 'ok', text: `${action} saved — course re-emitted, all quality gates pass${warn}.` });
        } else {
            setBanner({
                kind: 'error',
                text: `${action} saved, but validation now reports ${report.errors.length} error(s):`,
                details: report.errors.slice(0, 5).map(e => `${e.path}: ${e.message}`),
            });
        }
    };

    const handleSave = async (i: number, exercise: Exercise) => {
        if (!runId) return;
        setBusy('save');
        setBanner(null);
        try {
            const report = await saveExercise(runId, selectedModuleIdx, selectedLessonIdx, i, exercise);
            reportBanner(report, 'Edit');
            setEditingIdx(null);
            await onCourseChanged?.();
        } catch (e: any) {
            setBanner({ kind: 'error', text: `Edit rejected: ${e.message}` });
        } finally {
            setBusy(null);
        }
    };

    const handleRegenerate = async (i: number) => {
        if (!runId) return;
        setBusy('regen');
        setBanner(null);
        try {
            const report = await regenerateExercise(runId, selectedModuleIdx, selectedLessonIdx, i, regenNote.trim() || undefined);
            reportBanner(report, `Regenerated question (${report.model_id || 'LLM'})`);
            setRegenIdx(null);
            setRegenNote('');
            await onCourseChanged?.();
        } catch (e: any) {
            setBanner({ kind: 'error', text: `Regeneration failed: ${e.message}` });
        } finally {
            setBusy(null);
        }
    };

    return (
        <div style={{ display: 'flex', gap: '2rem', marginTop: '1rem', flexDirection: 'column' }}>
            {banner && (
                <div style={{
                    padding: '0.75rem 1rem', borderRadius: '8px', fontSize: '0.95rem',
                    background: banner.kind === 'ok' ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)',
                    border: `1px solid ${banner.kind === 'ok' ? '#22C55E' : '#EF4444'}`,
                }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
                        <span>{banner.text}</span>
                        <button onClick={() => setBanner(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', fontWeight: 700 }}>✕</button>
                    </div>
                    {banner.details && (
                        <ul style={{ margin: '0.5rem 0 0', paddingLeft: '1.25rem' }}>
                            {banner.details.map((d, j) => <li key={j}>{d}</li>)}
                        </ul>
                    )}
                </div>
            )}

            <div style={{ display: 'flex', gap: '2rem', flexWrap: 'wrap' }}>
                <div style={{ width: '100%', maxWidth: '300px' }}>
                    <Card style={{ padding: '1.5rem' }}>
                        <h3 style={{ fontSize: '1rem', fontWeight: 600, marginBottom: '1rem', textTransform: 'uppercase', color: 'var(--color-text-secondary)', letterSpacing: '0.05em' }}>Structure</h3>

                        <div style={{ marginBottom: '1.5rem' }}>
                            <label style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.9rem', fontWeight: 500 }}>Module</label>
                            <StyledSelect
                                value={selectedModuleIdx}
                                onChange={(e) => {
                                    setSelectedModuleIdx(Number(e.target.value));
                                    setSelectedLessonIdx(0);
                                    setEditingIdx(null);
                                    setRegenIdx(null);
                                }}
                            >
                                {modules.map((m, i) => (
                                    <option key={i} value={i}>{m.title}</option>
                                ))}
                            </StyledSelect>
                        </div>

                        {currentModule && (
                            <div>
                                <label style={{ display: 'block', marginBottom: '0.5rem', fontSize: '0.9rem', fontWeight: 500 }}>Lesson</label>
                                <StyledSelect
                                    value={selectedLessonIdx}
                                    onChange={(e) => {
                                        setSelectedLessonIdx(Number(e.target.value));
                                        setEditingIdx(null);
                                        setRegenIdx(null);
                                    }}
                                >
                                    {lessons.map((l, i) => (
                                        <option key={i} value={i}>{l.title}</option>
                                    ))}
                                </StyledSelect>
                            </div>
                        )}
                    </Card>
                </div>

                <div style={{ flex: 1, minWidth: '300px' }}>
                    {currentLesson ? (
                        <div>
                            <div style={{ marginBottom: '2rem' }}>
                                <h2 style={{ fontSize: '2rem', fontWeight: 700, letterSpacing: '-0.02em', marginBottom: '0.5rem' }}>{currentLesson.title}</h2>
                                <div style={{ color: 'var(--color-text-secondary)', lineHeight: '1.5' }}>
                                    <span style={{ display: 'inline-block', backgroundColor: '#E5E7EB', padding: '0.25rem 0.5rem', borderRadius: '4px', fontSize: '0.8rem', fontWeight: 600, marginRight: '0.5rem' }}>SLO</span>
                                    {currentLesson.slo}
                                </div>
                            </div>

                            {currentLesson.exercises?.length > 0 && (
                                <div style={{ marginBottom: '3rem' }}>
                                    <h3 style={{ fontSize: '1.25rem', fontWeight: 700, marginBottom: '1.5rem' }}>Exercises</h3>
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
                                        {currentLesson.exercises.map((ex, i) => (
                                            <div key={i}>
                                                <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.75rem', gap: '0.75rem' }}>
                                                    <div style={{ fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>
                                                        Problem {i + 1} • {ex.blooms_level} • {ex.question_type}
                                                    </div>
                                                    {editable && editingIdx !== i && (
                                                        <div style={{ marginLeft: 'auto', display: 'flex', gap: '0.5rem' }}>
                                                            <StyledButton variant="tertiary" onClick={() => { setEditingIdx(i); setRegenIdx(null); setBanner(null); }} disabled={busy !== null}>
                                                                ✏️ Edit
                                                            </StyledButton>
                                                            <StyledButton variant="tertiary" onClick={() => { setRegenIdx(regenIdx === i ? null : i); setEditingIdx(null); setBanner(null); }} disabled={busy !== null}>
                                                                🪄 Regenerate
                                                            </StyledButton>
                                                        </div>
                                                    )}
                                                </div>

                                                {regenIdx === i && (
                                                    <div style={{ border: '2px dashed #C4B5FD', borderRadius: '12px', padding: '1rem', marginBottom: '1rem', background: 'rgba(196,181,253,0.06)' }}>
                                                        <label style={{ display: 'block', fontSize: '0.85rem', fontWeight: 600, marginBottom: '0.4rem' }}>
                                                            What should be better? (optional)
                                                        </label>
                                                        <textarea
                                                            rows={2}
                                                            style={{ width: '100%', padding: '0.5rem 0.75rem', borderRadius: '8px', border: '1px solid #D1D5DB', fontFamily: 'inherit', boxSizing: 'border-box' }}
                                                            placeholder="e.g. distractors are too obvious, make the scenario more concrete…"
                                                            value={regenNote}
                                                            onChange={(e) => setRegenNote(e.target.value)}
                                                            disabled={busy === 'regen'}
                                                        />
                                                        <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.75rem', alignItems: 'center' }}>
                                                            <StyledButton onClick={() => handleRegenerate(i)} disabled={busy !== null}>
                                                                {busy === 'regen' ? 'Regenerating… (can take ~1 min)' : 'Regenerate with AI'}
                                                            </StyledButton>
                                                            <StyledButton variant="tertiary" onClick={() => setRegenIdx(null)} disabled={busy === 'regen'}>Cancel</StyledButton>
                                                        </div>
                                                    </div>
                                                )}

                                                {editingIdx === i ? (
                                                    <ExerciseEditor
                                                        exercise={ex}
                                                        saving={busy === 'save'}
                                                        onSave={(edited) => handleSave(i, edited)}
                                                        onCancel={() => setEditingIdx(null)}
                                                    />
                                                ) : (
                                                    <ExerciseRenderer
                                                        exercise={ex}
                                                        value={null}
                                                        onChange={() => { }}
                                                        submitted={false}
                                                        seed={i}
                                                    />
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {currentLesson.flashcards && currentLesson.flashcards.length > 0 && (
                                <div>
                                    <h3 style={{ fontSize: '1.25rem', fontWeight: 700, marginBottom: '1.5rem' }}>Flashcards</h3>
                                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '1.5rem' }}>
                                        {currentLesson.flashcards.map((fc, i) => (
                                            <Card key={i} style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                                                <div>
                                                    <div style={{ fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '0.8rem', marginBottom: '0.25rem', textTransform: 'uppercase' }}>Front</div>
                                                    <div style={{ fontSize: '1.1rem' }}>{fc.front}</div>
                                                </div>
                                                <div style={{ height: '1px', background: '#E5E7EB' }}></div>
                                                <div>
                                                    <div style={{ fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '0.8rem', marginBottom: '0.25rem', textTransform: 'uppercase' }}>Back</div>
                                                    <div>{fc.back}</div>
                                                </div>
                                                {fc.hint && (
                                                    <div style={{ marginTop: 'auto', paddingTop: '0.5rem', fontSize: '0.9rem', color: 'var(--color-text-secondary)', fontStyle: 'italic' }}>
                                                        Tip: {fc.hint}
                                                    </div>
                                                )}
                                            </Card>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </div>
                    ) : (
                        <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--color-text-secondary)' }}>Select a lesson to view content.</div>
                    )}
                </div>
            </div>
        </div>
    );
};
