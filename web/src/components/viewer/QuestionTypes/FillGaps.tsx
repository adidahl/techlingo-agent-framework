import React from 'react';
import { FillGapsExercise, QuestionProps, FillGapsPart } from '../types';
import { gradeGapAnswer, GapGrade } from '../grading';
import styles from './Question.module.css';

// Grading spec v1 (GRADING_SPEC.md): exact match after normalization, or a
// small typo (edit distance scaled by answer length) — same as the mobile app.
// rejected_answers (concept confusables like LLM/SLM) are never accepted.
function gradeGap(user: string, part: FillGapsPart): GapGrade {
    return gradeGapAnswer(user, part.accepted_answers || [], part.rejected_answers || []);
}

export const FillGaps: React.FC<QuestionProps<FillGapsExercise>> = ({ exercise, value, onChange, submitted }) => {
    const gaps = exercise.parts.filter(p => p.type === 'gap');
    const userValues = (value as string[]) || Array(gaps.length).fill("");

    const handleGapChange = (idx: number, val: string) => {
        const next = [...userValues];
        next[idx] = val;
        onChange(next);
    };

    // Construct the render parts
    // We need to interleave text and inputs
    let gapIndex = 0;

    return (
        <div className={styles.questionContainer}>
            <div style={{ lineHeight: '2.5rem' }}>
                {exercise.parts.map((part, i) => {
                    if (part.type === 'text') {
                        return <span key={i}>{part.text}</span>;
                    } else {
                        const currentGapIdx = gapIndex++;
                        return (
                            <input
                                key={i}
                                type="text"
                                className={styles.gapInput}
                                value={userValues[currentGapIdx] || ""}
                                placeholder={part.placeholder || `Gap ${currentGapIdx + 1}`}
                                onChange={(e) => handleGapChange(currentGapIdx, e.target.value)}
                                disabled={submitted}
                                style={{
                                    maxWidth: '150px'
                                }}
                            />
                        );
                    }
                })}
            </div>
            {submitted && (
                <FeedbackDisplay exercise={exercise} userValues={userValues} />
            )}
        </div>
    );
};

const FeedbackDisplay: React.FC<{ exercise: FillGapsExercise, userValues: string[] }> = ({ exercise, userValues }) => {
    const gaps = exercise.parts.filter(p => p.type === 'gap');

    // Check all matches (exact or tolerated typo counts as correct)
    const grades = gaps.map((g, i) => gradeGap(userValues[i], g));
    const correctMatches = grades.map(g => g !== 'wrong');
    const isAllCorrect = correctMatches.every(Boolean);
    const hadTypo = grades.some(g => g === 'typo');

    const fbi = exercise.feedback_for_incorrect;

    return (
        <div className={`${styles.feedbackContainer} ${isAllCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isAllCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>
            {isAllCorrect && hadTypo && (
                <div style={{ marginTop: '0.25rem', fontSize: '0.9rem' }}>
                    You have a small typo — accepted: {gaps.map(g => (g.accepted_answers || [])[0]).join(', ')}
                </div>
            )}

            {!isAllCorrect && fbi && (
                <div style={{ marginTop: '0.5rem' }}>
                    {typeof fbi === 'string' ? <div>{fbi}</div> : (
                        <>
                            {fbi.intrinsic && <div>{fbi.intrinsic}</div>}
                            {fbi.instructional && <div style={{ fontStyle: 'italic' }}>{fbi.instructional}</div>}
                        </>
                    )}
                </div>
            )}

            {!isAllCorrect && (
                <div style={{ marginTop: '0.5rem' }}>
                    <strong>Correct Answers:</strong>
                    <ul style={{ marginTop: '0.25rem', paddingLeft: '1.25rem' }}>
                        {gaps.map((g, i) => (
                            <li key={i}>
                                Gap {i + 1}: {(g.accepted_answers || []).join(", ")}
                                {!correctMatches[i] && <span style={{ color: 'red', marginLeft: '0.5rem' }}>(You: {userValues[i]})</span>}
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {exercise.explanation && (
                <div style={{ marginTop: '0.5rem' }}>
                    <strong>Why:</strong> {exercise.explanation}
                </div>
            )}
        </div>
    );
}
