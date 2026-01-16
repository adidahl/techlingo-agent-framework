import React, { useState } from 'react';
import { Course } from './types';
import { ExerciseRenderer } from './ExerciseRenderer';
import { StyledSelect, Card } from './Styled';

interface Props {
    course: Course;
}

export const BrowseTab: React.FC<Props> = ({ course }) => {
    const [selectedModuleIdx, setSelectedModuleIdx] = useState<number>(0);
    const [selectedLessonIdx, setSelectedLessonIdx] = useState<number>(0);

    const modules = course.modules || [];
    const currentModule = modules[selectedModuleIdx];
    const lessons = currentModule?.lessons || [];
    const currentLesson = lessons[selectedLessonIdx];

    if (!modules.length) return <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--color-text-secondary)' }}>No modules in this course.</div>;

    return (
        <div style={{ display: 'flex', gap: '2rem', marginTop: '1rem', flexDirection: 'column' }}>
            {/* Use responsive grid for layout? For now keep simple flex but maybe row on desktop */}
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
                                    onChange={(e) => setSelectedLessonIdx(Number(e.target.value))}
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
                                                <div style={{ fontWeight: 600, marginBottom: '0.75rem', color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>
                                                    Problem {i + 1} • {ex.blooms_level}
                                                </div>
                                                <ExerciseRenderer
                                                    exercise={ex}
                                                    value={null}
                                                    onChange={() => { }}
                                                    submitted={false}
                                                    seed={i}
                                                />
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
