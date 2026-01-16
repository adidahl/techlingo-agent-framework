import React from 'react';
import { RearrangeExercise, QuestionProps } from '../types';
import styles from './Question.module.css';

import { StyledSelect } from '../Styled';

export const Rearrange: React.FC<QuestionProps<RearrangeExercise>> = ({ exercise, value, onChange, submitted }) => {
    // Value is array of selected strings in order
    const currentOrder = (value as string[]) || Array(exercise.correct_order.length).fill(exercise.word_bank[0]);

    const handleChange = (idx: number, val: string) => {
        const next = [...currentOrder];
        next[idx] = val;
        onChange(next);
    }

    return (
        <div className={styles.questionContainer}>
            <div style={{ marginBottom: '1rem', fontStyle: 'italic', color: 'var(--color-text-secondary)' }}>
                Word Bank: {exercise.word_bank.join(" | ")}
            </div>

            <div className={styles.optionsContainer}>
                {exercise.correct_order.map((_, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
                        <span style={{ width: '24px', fontWeight: 600, color: 'var(--color-text-secondary)' }}>{i + 1}.</span>
                        <StyledSelect
                            value={currentOrder[i]}
                            onChange={(e) => handleChange(i, e.target.value)}
                            disabled={submitted}
                            style={{ flex: 1 }}
                        >
                            {exercise.word_bank.map((word) => (
                                <option key={word} value={word}>{word}</option>
                            ))}
                        </StyledSelect>
                    </div>
                ))}
            </div>

            {submitted && (
                <FeedbackDisplay exercise={exercise} currentOrder={currentOrder} />
            )}
        </div>
    );
};

const FeedbackDisplay: React.FC<{ exercise: RearrangeExercise, currentOrder: string[] }> = ({ exercise, currentOrder }) => {

    // Check equality
    const isCorrect = JSON.stringify(currentOrder) === JSON.stringify(exercise.correct_order);

    return (
        <div className={`${styles.feedbackContainer} ${isCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>

            {!isCorrect && (
                <div style={{ marginTop: '0.5rem' }}>
                    <strong>Correct Order:</strong>
                    <div style={{ marginTop: '0.5rem', padding: '0.5rem', background: 'rgba(0,0,0,0.05)', borderRadius: '4px' }}>
                        {exercise.correct_order.join(" | ")}
                    </div>
                </div>
            )}
        </div>
    );
}
