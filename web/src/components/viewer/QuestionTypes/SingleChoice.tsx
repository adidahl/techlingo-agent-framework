import React, { useMemo } from 'react';
import { SingleChoiceExercise, QuestionProps } from '../types';
import { getChoiceOptions } from '../utils';
import styles from './Question.module.css';

export const SingleChoice: React.FC<QuestionProps<SingleChoiceExercise>> = ({ exercise, value, onChange, submitted, seed }) => {
    const options = useMemo(() => getChoiceOptions(exercise.options, seed), [exercise.options, seed]);

    return (
        <div className={styles.questionContainer}>
            <div className={styles.optionsContainer}>
                {options.map((opt) => (
                    <label key={opt.id} className={`${styles.optionLabel} ${submitted ? styles.disabled : ''}`}>
                        <input
                            type="radio"
                            name={`sc-${seed}`}
                            value={opt.id}
                            checked={value === opt.id}
                            onChange={() => onChange(opt.id)}
                            disabled={submitted}
                        />
                        {opt.label}
                    </label>
                ))}
            </div>
            {submitted && value && (
                <FeedbackDisplay selectedId={value} options={options} />
            )}
        </div>
    );
};

const FeedbackDisplay: React.FC<{ selectedId: string, options: any[] }> = ({ selectedId, options }) => {
    const selected = options.find(o => o.id === selectedId);
    const correct = options.find(o => o.is_correct);

    if (!selected) return null;

    const isCorrect = selected.is_correct;

    return (
        <div className={`${styles.feedbackContainer} ${isCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>

            {!isCorrect && selected.feedback && (
                <div style={{ marginTop: '0.5rem' }}>
                    {selected.feedback.intrinsic && <div>{selected.feedback.intrinsic}</div>}
                    {selected.feedback.instructional && <div style={{ fontStyle: 'italic' }}>{selected.feedback.instructional}</div>}
                </div>
            )}

            {isCorrect && selected.feedback && (
                <div style={{ marginTop: '0.5rem' }}>
                    {/* Usually correct answer doesn't have detailed feedback in this structure, but just in case */}
                </div>
            )}

            {!isCorrect && correct && (
                <div style={{ marginTop: '0.5rem', borderTop: '1px solid currentColor', paddingTop: '0.5rem' }}>
                    <strong>Correct Answer:</strong> {correct.label}
                    {correct.rationale && <div>{correct.rationale}</div>}
                </div>
            )}
            {isCorrect && selected.rationale && (
                <div style={{ marginTop: '0.5rem' }}>
                    <strong>Rationale:</strong> {selected.rationale}
                </div>
            )}
        </div>
    );
}
