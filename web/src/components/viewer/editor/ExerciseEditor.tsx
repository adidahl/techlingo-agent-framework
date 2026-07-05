import React, { useState } from 'react';
import {
    Exercise,
    ChoiceOption,
    FillGapsPart,
    SingleChoiceExercise,
    MultiChoiceExercise,
    TrueFalseExercise,
    FillGapsExercise,
    RearrangeExercise,
} from '../types';
import { StyledButton } from '../Styled';

interface Props {
    exercise: Exercise;
    saving: boolean;
    onSave: (exercise: Exercise) => void;
    onCancel: () => void;
}

const labelStyle: React.CSSProperties = {
    display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.25rem',
    color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em',
};

const inputStyle: React.CSSProperties = {
    width: '100%', padding: '0.5rem 0.75rem', borderRadius: '8px',
    border: '1px solid #D1D5DB', fontSize: '0.95rem', fontFamily: 'inherit',
    background: 'var(--color-bg, #fff)', color: 'inherit', boxSizing: 'border-box',
};

const rowStyle: React.CSSProperties = { marginBottom: '1rem' };

function TextArea({ value, onChange, rows = 2 }: { value: string; onChange: (v: string) => void; rows?: number }) {
    return (
        <textarea
            style={{ ...inputStyle, resize: 'vertical' }}
            rows={rows}
            value={value}
            onChange={(e) => onChange(e.target.value)}
        />
    );
}

function TextInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
    return (
        <input
            type="text"
            style={inputStyle}
            value={value}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
        />
    );
}

export const ExerciseEditor: React.FC<Props> = ({ exercise, saving, onSave, onCancel }) => {
    const [draft, setDraft] = useState<Exercise>(() => JSON.parse(JSON.stringify(exercise)));

    const update = (patch: Partial<Exercise>) => setDraft(prev => ({ ...prev, ...patch } as Exercise));

    // ---- choice options ------------------------------------------------
    const updateOption = (i: number, patch: Partial<ChoiceOption>) => {
        const d = draft as SingleChoiceExercise | MultiChoiceExercise;
        const options = d.options.map((o, j) => (j === i ? { ...o, ...patch } : o));
        update({ options } as Partial<Exercise>);
    };

    const choiceEditor = (d: SingleChoiceExercise | MultiChoiceExercise) => (
        <>
            {d.options.map((opt, i) => (
                <div key={i} style={{ border: '1px solid #E5E7EB', borderRadius: '8px', padding: '0.75rem', marginBottom: '0.75rem' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                        <label style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.85rem', fontWeight: 600 }}>
                            <input
                                type={d.question_type === 'single_choice' ? 'radio' : 'checkbox'}
                                checked={opt.is_correct}
                                onChange={(e) => {
                                    if (d.question_type === 'single_choice') {
                                        const options = d.options.map((o, j) => ({ ...o, is_correct: j === i }));
                                        update({ options } as Partial<Exercise>);
                                    } else {
                                        updateOption(i, { is_correct: e.target.checked });
                                    }
                                }}
                            />
                            Correct
                        </label>
                        <span style={{ marginLeft: 'auto', fontSize: '0.8rem', color: 'var(--color-text-secondary)' }}>Option {i + 1}</span>
                    </div>
                    <div style={{ marginBottom: '0.5rem' }}>
                        <TextArea value={opt.text} onChange={(v) => updateOption(i, { text: v })} />
                    </div>
                    <label style={labelStyle}>Rationale (why right/wrong)</label>
                    <TextArea value={opt.rationale || ''} onChange={(v) => updateOption(i, { rationale: v || null })} />
                </div>
            ))}
        </>
    );

    // ---- fill gaps parts -----------------------------------------------
    const partsEditor = (d: FillGapsExercise) => {
        const updatePart = (i: number, part: FillGapsPart) => {
            const parts = d.parts.map((p, j) => (j === i ? part : p));
            update({ parts } as Partial<Exercise>);
        };
        return (
            <>
                {d.parts.map((part, i) => (
                    <div key={i} style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start', marginBottom: '0.5rem' }}>
                        <span style={{ fontSize: '0.75rem', fontWeight: 700, padding: '0.35rem 0.5rem', borderRadius: '6px', background: part.type === 'gap' ? '#DBEAFE' : '#F3F4F6', whiteSpace: 'nowrap' }}>
                            {part.type === 'gap' ? 'GAP' : 'TEXT'}
                        </span>
                        {part.type === 'text' ? (
                            <TextArea value={part.text || ''} onChange={(v) => updatePart(i, { ...part, text: v })} rows={1} />
                        ) : (
                            <div style={{ flex: 1 }}>
                                <TextInput
                                    value={(part.accepted_answers || []).join(', ')}
                                    placeholder="accepted answers, comma-separated"
                                    onChange={(v) => updatePart(i, { ...part, accepted_answers: v.split(',').map(s => s.trim()).filter(Boolean) })}
                                />
                            </div>
                        )}
                    </div>
                ))}
            </>
        );
    };

    // ---- rearrange tokens ------------------------------------------------
    const rearrangeEditor = (d: RearrangeExercise) => {
        const setToken = (i: number, v: string) => {
            const correct_order = d.correct_order.map((t, j) => (j === i ? v : t));
            update({ correct_order } as Partial<Exercise>);
        };
        const removeToken = (i: number) => {
            update({ correct_order: d.correct_order.filter((_, j) => j !== i) } as Partial<Exercise>);
        };
        return (
            <>
                <label style={labelStyle}>Correct order (word bank is auto-shuffled on save)</label>
                {d.correct_order.map((tok, i) => (
                    <div key={i} style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.4rem', alignItems: 'center' }}>
                        <span style={{ width: '20px', fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>{i + 1}.</span>
                        <TextInput value={tok} onChange={(v) => setToken(i, v)} />
                        <StyledButton variant="tertiary" onClick={() => removeToken(i)} disabled={d.correct_order.length <= 4}>✕</StyledButton>
                    </div>
                ))}
                <StyledButton
                    variant="tertiary"
                    onClick={() => update({ correct_order: [...d.correct_order, ''] } as Partial<Exercise>)}
                    disabled={d.correct_order.length >= 8}
                >
                    + Add token
                </StyledButton>
            </>
        );
    };

    // ---- save ------------------------------------------------------------
    const handleSave = () => {
        const cleaned: Exercise = JSON.parse(JSON.stringify(draft));
        if (cleaned.question_type === 'rearrange') {
            cleaned.correct_order = cleaned.correct_order.map(t => t.trim()).filter(Boolean);
            cleaned.word_bank = [...cleaned.correct_order]; // server reshuffles deterministically
        }
        onSave(cleaned);
    };

    const tf = draft as TrueFalseExercise;

    return (
        <div style={{ border: '2px solid #93C5FD', borderRadius: '12px', padding: '1.25rem', background: 'rgba(147,197,253,0.06)' }}>
            <div style={rowStyle}>
                <label style={labelStyle}>Prompt</label>
                <TextArea value={draft.prompt} onChange={(v) => update({ prompt: v })} />
            </div>

            {draft.question_type === 'true_false' && (
                <>
                    <div style={rowStyle}>
                        <label style={labelStyle}>Statement</label>
                        <TextArea value={tf.statement} onChange={(v) => update({ statement: v } as Partial<Exercise>)} />
                    </div>
                    <div style={rowStyle}>
                        <label style={labelStyle}>Correct answer</label>
                        <div style={{ display: 'flex', gap: '1rem' }}>
                            {[true, false].map((val) => (
                                <label key={String(val)} style={{ display: 'flex', gap: '0.35rem', alignItems: 'center' }}>
                                    <input
                                        type="radio"
                                        checked={tf.correct_answer === val}
                                        onChange={() => update({ correct_answer: val } as Partial<Exercise>)}
                                    />
                                    {val ? 'True' : 'False'}
                                </label>
                            ))}
                        </div>
                        <div style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary)', marginTop: '0.25rem' }}>
                            Note: flipping the answer may unbalance the course-wide true/false pattern (validation will warn).
                        </div>
                    </div>
                </>
            )}

            {(draft.question_type === 'single_choice' || draft.question_type === 'multi_choice') &&
                choiceEditor(draft as SingleChoiceExercise | MultiChoiceExercise)}

            {draft.question_type === 'fill_gaps' && partsEditor(draft as FillGapsExercise)}

            {draft.question_type === 'rearrange' && rearrangeEditor(draft as RearrangeExercise)}

            <div style={{ display: 'flex', gap: '0.75rem', marginTop: '1.25rem' }}>
                <StyledButton onClick={handleSave} disabled={saving}>
                    {saving ? 'Saving…' : 'Save & Validate'}
                </StyledButton>
                <StyledButton variant="tertiary" onClick={onCancel} disabled={saving}>Cancel</StyledButton>
            </div>
        </div>
    );
};
