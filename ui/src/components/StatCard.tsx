import clsx from 'clsx';

interface Props {
  label: string;
  value: string;
  sub?: string;
  accent?: boolean;
  shimmer?: boolean;
}

export function StatCard({ label, value, sub, accent, shimmer }: Props) {
  return (
    <div className={clsx('nest-card p-4 relative overflow-hidden', shimmer && 'data-shimmer')}>
      <p className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: 'var(--nest-text-ghost)' }}>
        {label}
      </p>
      <p className={clsx(
        'text-xl font-semibold font-mono relative z-10',
        accent ? 'text-[var(--nest-blue)]' : 'text-[var(--nest-text-bright)]'
      )}>
        {value}
      </p>
      {sub && <p className="text-[11px] mt-1" style={{ color: 'var(--nest-text-dim)' }}>{sub}</p>}
    </div>
  );
}
