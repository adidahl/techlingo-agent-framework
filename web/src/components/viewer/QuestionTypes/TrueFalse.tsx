import React from 'react';
import { TrueFalseExercise, QuestionProps } from '../types';
import styles from './Question.module.css';

export const TrueFalse: React.FC<QuestionProps<TrueFalseExercise>> = ({ exercise, value, onChange, submitted }) => {

    return (
        <div className={styles.questionContainer}>
            <div style={{ fontWeight: '500', marginBottom: '0.5rem' }}>
                {exercise.statement}
            </div>
            <div className={styles.optionsContainer}>
                <label className={`${styles.optionLabel} ${submitted ? styles.disabled : ''}`}>
                    <input
                        type="radio"
                        name={`tf-${exercise.prompt}`}
                        checked={value === true}
                        onChange={() => onChange(true)}
                        disabled={submitted}
                    />
                    True
                </label>
                <label className={`${styles.optionLabel} ${submitted ? styles.disabled : ''}`}>
                    <input
                        type="radio"
                        name={`tf-${exercise.prompt}`}
                        checked={value === false}
                        onChange={() => onChange(false)}
                        disabled={submitted}
                    />
                    False
                </label>
            </div>
            {submitted && value !== undefined && (
                <FeedbackDisplay exercise={exercise} value={value} />
            )}
        </div>
    );
};

const FeedbackDisplay: React.FC<{ exercise: TrueFalseExercise, value: boolean }> = ({ exercise, value }) => {
    const isCorrect = value === exercise.correct_answer;
    const fb = !isCorrect ? exercise.feedback_for_incorrect : null;

    return (
        <div className={`${styles.feedbackContainer} ${isCorrect ? styles.feedbackCorrect : styles.feedbackIncorrect}`}>
            <div><strong>{isCorrect ? "Correct! ✅" : "Incorrect ❌"}</strong></div>

            {!isCorrect && fb && (
                <div style={{ marginTop: '0.5rem' }}>
                    {fb.intrinsic && <div>{fb.intrinsic}</div>}
                    {fb.instructional && <div style={{ fontStyle: 'italic' }}>{fb.instructional}</div>}
                </div>
            )}
            {!isCorrect && (
                <div style={{ marginTop: '0.5rem' }}>
                    <strong>Correct Answer:</strong> {exercise.correct_answer ? "True" : "False"}
                </div>
            )}
        </div>
    );
}
