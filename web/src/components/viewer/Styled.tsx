import React from 'react';

// Common styled components for reuse within the viewer
// Using simple inline styles + classes for speed, mimicking Designsystemet if needed or custom theme

export const StyledSelect = ({ style, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) => (
    <select
        {...props}
        style={{
            padding: '0.75rem 1rem',
            borderRadius: '12px',
            border: '1px solid #E5E7EB',
            backgroundColor: '#fff',
            color: 'var(--color-text-primary)',
            fontSize: '1rem',
            width: '100%',
            cursor: 'pointer',
            appearance: 'none',
            backgroundImage: `url("data:image/svg+xml;charset=US-ASCII,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%22292.4%22%20height%3D%22292.4%22%3E%3Cpath%20fill%3D%22%23131313%22%20d%3D%22M287%2069.4a17.6%2017.6%200%200%200-13-5.4H18.4c-5%200-9.3%201.8-12.9%205.4A17.6%2017.6%200%200%200%200%2082.2c0%205%201.8%209.3%205.4%2012.9l128%20127.9c3.6%203.6%207.8%205.4%2012.8%205.4s9.2-1.8%2012.8-5.4L287%2095c3.5-3.5%205.4-7.8%205.4-12.8%200-5-1.9-9.2-5.5-12.8z%22%2F%3E%3C%2Fsvg%3E")`,
            backgroundRepeat: 'no-repeat',
            backgroundPosition: 'right 1rem center',
            backgroundSize: '0.75rem',
            ...style
        }}
    />
);

export const StyledButton = ({ variant = 'primary', style, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'tertiary' }) => {
    let bg = 'var(--color-accent-lime)';
    let color = '#0B2318';

    if (variant === 'secondary') {
        bg = '#F59E0B';
        color = '#fff';
    } else if (variant === 'tertiary') {
        bg = 'transparent';
        color = 'var(--color-text-secondary)';
    }

    return (
        <button
            {...props}
            style={{
                backgroundColor: bg,
                color: color,
                border: variant === 'tertiary' ? '1px solid #E5E7EB' : 'none',
                padding: '0.75rem 1.5rem',
                borderRadius: '12px',
                fontWeight: 600,
                fontSize: '1rem',
                cursor: 'pointer',
                transition: 'opacity 0.2s',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                ...style
            }}
            onMouseOver={(e) => {
                if (variant !== 'tertiary' && !props.disabled) e.currentTarget.style.opacity = '0.9';
            }}
            onMouseOut={(e) => {
                if (variant !== 'tertiary' && !props.disabled) e.currentTarget.style.opacity = '1';
            }}
        />
    );
}

export const Card = ({ children, style }: { children: React.ReactNode, style?: React.CSSProperties }) => (
    <div className="card" style={style}>
        {children}
    </div>
);
