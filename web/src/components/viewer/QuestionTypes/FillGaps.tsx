import React from 'react';
import { FillGapsExercise, QuestionProps, FillGapsPart } from '../types';
import styles from './Question.module.css';

// Simple normalization helper to match python logic
function normalizeText(s: string): string {
    return (s || "").trim().toLowerCase().split(/\s+/).join(" ");
}

function checkMatch(user: string, accepted: string[] | undefined): boolean {
    if (!accepted) return false;
    const u = normalizeText(user);
    return accepted.some(a => normalizeText(a) === u);
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

    // Check all matches
    const correctMatches = gaps.map((g, i) => checkMatch(userValues[i], g.accepted_answers));
    const isAllCorrect = correctMatches.every(Boolean);

    return (
        <div className={`${styles.feedbackContainer} ${isAllCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isAllCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>

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
        </div>
    );
}
