import React, { useState, useMemo, useEffect } from 'react';
import { Course, Exercise } from './types';
import { ExerciseRenderer } from './ExerciseRenderer';
import { StyledButton, Card } from './Styled';

interface Props {
    course: Course;
    seed: number;
}

interface FlattenedExercise {
    moduleTitle: string;
    lessonTitle: string;
    exercise: Exercise;
    slo: string;
    index: number;
}

export const QuizTab: React.FC<Props> = ({ course, seed }) => {
    // Flatten exercises
    const flatExercises = useMemo(() => {
        const flat: FlattenedExercise[] = [];
        let idx = 0;
        course.modules?.forEach(m => {
            m.lessons?.forEach(l => {
                l.exercises?.forEach(e => {
                    flat.push({
                        moduleTitle: m.title,
                        lessonTitle: l.title,
                        exercise: e,
                        slo: l.slo,
                        index: idx++
                    });
                });
            });
        });
        return flat;
    }, [course]);

    const [quizStarted, setQuizStarted] = useState(false);
    const [currentIndex, setCurrentIndex] = useState(0);
    const [answers, setAnswers] = useState<Record<number, any>>({});
    const [submittedIndices, setSubmittedIndices] = useState<Set<number>>(new Set());
    const [finished, setFinished] = useState(false);

    useEffect(() => {
        setQuizStarted(false);
        setCurrentIndex(0);
        setAnswers({});
        setSubmittedIndices(new Set());
        setFinished(false);
    }, [course, seed]);

    const currentEx = flatExercises[currentIndex];

    const handleStart = () => {
        setQuizStarted(true);
        setFinished(false);
    };

    const handleAnswer = () => {
        const next = new Set(submittedIndices);
        next.add(currentIndex);
        setSubmittedIndices(next);
    };

    const handleNext = () => {
        if (currentIndex < flatExercises.length - 1) {
            setCurrentIndex(currentIndex + 1);
        } else {
            setFinished(true);
        }
    };

    const handlePrev = () => {
        if (currentIndex > 0) {
            setCurrentIndex(currentIndex - 1);
        }
    };

    const handleResetQuestion = () => {
        const next = new Set(submittedIndices);
        next.delete(currentIndex);
        setSubmittedIndices(next);
    };

    if (!flatExercises.length) {
        return <div style={{ padding: '2rem', textAlign: 'center' }}>No exercises found in this course.</div>;
    }

    if (!quizStarted) {
        return (
            <div style={{ textAlign: 'center', padding: '6rem 2rem' }}>
                <div style={{ marginBottom: '2rem', fontSize: '3rem' }}>🎓</div>
                <h2 style={{ marginBottom: '1rem', fontSize: '1.5rem' }}>Full Course Quiz</h2>
                <p style={{ marginBottom: '2rem', color: 'var(--color-text-secondary)' }}>
                    Ready to test your knowledge? <br />
                    There are <strong>{flatExercises.length} questions</strong> across {course.modules.length} modules.
                </p>
                <StyledButton onClick={handleStart} style={{ fontSize: '1.1rem', padding: '1rem 2rem' }}>Start Quiz</StyledButton>
            </div>
        );
    }

    if (finished) {
        return (
            <div style={{ maxWidth: '800px', margin: '0 auto', paddingTop: '2rem' }}>
                <Card style={{ padding: '3rem', textAlign: 'center' }}>
                    <h2 style={{ marginBottom: '1rem' }}>Quiz Completed! 🎉</h2>
                    <p style={{ marginBottom: '2rem', color: 'var(--color-text-secondary)' }}>
                        You have completed all questions in the course.
                    </p>

                    <div style={{ display: 'flex', gap: '1rem', justifyContent: 'center' }}>
                        <StyledButton onClick={() => {
                            setQuizStarted(false);
                            setFinished(false);
                            setAnswers({});
                            setSubmittedIndices(new Set());
                            setCurrentIndex(0);
                        }}>Restart Quiz</StyledButton>
                    </div>
                </Card>
            </div>
        );
    }

    const isSubmitted = submittedIndices.has(currentIndex);

    return (
        <div style={{ maxWidth: '800px', margin: '0 auto', paddingTop: '1rem' }}>
            <div style={{ marginBottom: '2rem' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem', fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>
                    <div>Question {currentIndex + 1} of {flatExercises.length}</div>
                    <div>{currentEx.moduleTitle}</div>
                </div>
                <div className="progress-container">
                    <div className="progress-bar" style={{ width: `${((currentIndex + 1) / flatExercises.length) * 100}%` }}></div>
                </div>
            </div>

            <div style={{ marginBottom: '2rem' }}>
                <h2 style={{ fontSize: '1.5rem', marginBottom: '0.5rem', lineHeight: '1.4' }}>{currentEx.exercise.prompt}</h2>
                <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
                    <span style={{ fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}>{currentEx.lessonTitle}</span>
                </div>
            </div>

            <ExerciseRenderer
                exercise={currentEx.exercise}
                value={answers[currentIndex]}
                onChange={(val) => setAnswers(prev => ({ ...prev, [currentIndex]: val }))}
                submitted={isSubmitted}
                seed={seed + currentIndex}
            />

            <div style={{ marginTop: '2rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <StyledButton
                    variant="tertiary"
                    onClick={handlePrev}
                    disabled={currentIndex === 0}
                    style={{ visibility: currentIndex === 0 ? 'hidden' : 'visible' }}
                >
                    Back
                </StyledButton>

                <div style={{ display: 'flex', gap: '1rem' }}>
                    {isSubmitted ? (
                        <>
                            <StyledButton variant="tertiary" onClick={handleResetQuestion}>Try Again</StyledButton>
                            <StyledButton onClick={handleNext}>
                                {currentIndex === flatExercises.length - 1 ? "Finish" : "Next Question"}
                            </StyledButton>
                        </>
                    ) : (
                        <StyledButton onClick={handleAnswer}>Submit Answer</StyledButton>
                    )}
                </div>
            </div>
        </div>
    );
};
