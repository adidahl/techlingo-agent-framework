import React, { useMemo } from 'react';
import { MultiChoiceExercise, QuestionProps } from '../types';
import { getChoiceOptions } from '../utils';
import styles from './Question.module.css';

export const MultiChoice: React.FC<QuestionProps<MultiChoiceExercise>> = ({ exercise, value, onChange, submitted, seed }) => {
    const options = useMemo(() => getChoiceOptions(exercise.options, seed), [exercise.options, seed]);
    const selectedIds = (value as string[]) || [];

    const handleChange = (id: string, checked: boolean) => {
        if (checked) {
            onChange([...selectedIds, id]);
        } else {
            onChange(selectedIds.filter(i => i !== id));
        }
    };

    return (
        <div className={styles.questionContainer}>
            <div className={styles.optionsContainer}>
                {options.map((opt) => (
                    <label key={opt.id} className={`${styles.optionLabel} ${submitted ? styles.disabled : ''}`}>
                        <input
                            type="checkbox"
                            checked={selectedIds.includes(opt.id)}
                            onChange={(e) => handleChange(opt.id, e.target.checked)}
                            disabled={submitted}
                        />
                        {opt.label}
                    </label>
                ))}
            </div>
            {submitted && (
                <FeedbackDisplay selectedIds={selectedIds} options={options} />
            )}
        </div>
    );
};

const FeedbackDisplay: React.FC<{ selectedIds: string[], options: any[] }> = ({ selectedIds, options }) => {
    const correctIds = options.filter(o => o.is_correct).map(o => o.id);
    const isCorrect = selectedIds.length === correctIds.length && selectedIds.every(id => correctIds.includes(id));

    // Find first wrong option needed for feedback
    let wrongOption = null;
    if (!isCorrect) {
        // Did we pick a wrong one?
        wrongOption = options.find(o => selectedIds.includes(o.id) && !o.is_correct);
        // Or did we miss a correct one?
        if (!wrongOption) {
            wrongOption = options.find(o => !selectedIds.includes(o.id) && o.is_correct);
        }
    }

    return (
        <div className={`${styles.feedbackContainer} ${isCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>

            {!isCorrect && wrongOption?.feedback && (
                <div style={{ marginTop: '0.5rem' }}>
                    {wrongOption.feedback.intrinsic && <div>{wrongOption.feedback.intrinsic}</div>}
                    {wrongOption.feedback.instructional && <div style={{ fontStyle: 'italic' }}>{wrongOption.feedback.instructional}</div>}
                </div>
            )}

            {!isCorrect && (
                <div style={{ marginTop: '0.5rem', borderTop: '1px solid currentColor', paddingTop: '0.5rem' }}>
                    <strong>Correct Answers:</strong>
                    <ul style={{ marginTop: '0.25rem', paddingLeft: '1.25rem' }}>
                        {options.filter(o => o.is_correct).map(o => (
                            <li key={o.id}>{o.label}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
