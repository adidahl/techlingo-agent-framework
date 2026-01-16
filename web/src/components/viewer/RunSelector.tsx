import React from 'react';
import { RunInfo } from '../../app/actions/viewer';
import { StyledSelect } from './Styled';

interface Props {
    runs: RunInfo[];
    selectedRunId: string;
    onSelect: (runId: string) => void;
}

export const RunSelector: React.FC<Props> = ({ runs, selectedRunId, onSelect }) => {
    return (
        <div style={{ marginBottom: '1.5rem' }}>
            <label style={{ display: 'block', marginBottom: '0.5rem', fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>
                Select Run
            </label>
            <StyledSelect
                value={selectedRunId}
                onChange={(e) => onSelect(e.target.value)}
                style={{ maxWidth: '400px' }}
            >
                <option value="">-- Choose a run --</option>
                {runs.map((run) => (
                    <option key={run.id} value={run.id}>
                        {run.name}
                    </option>
                ))}
            </StyledSelect>
        </div>
    );
};
