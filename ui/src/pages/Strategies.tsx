import { usePolling } from '../lib/usePolling';
import type { AgentState } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import { money } from '../lib/utils';

export default function Strategies() {
  const { data: state, lastUpdated, refresh } = usePolling<AgentState>('/api/state');
  const strategies = state?.strategies || [];

  const borderColor: Record<string, string> = {
    active: 'var(--nest-success)',
    paused: 'var(--nest-warning)',
    retired: 'var(--nest-text-ghost)',
    exploring: 'var(--nest-blue)',
    planned: 'var(--nest-blue)',
  };

  return (
    <div>
      <PageHeader title="Strategies" lastUpdated={lastUpdated} onRefresh={refresh} />

      {strategies.length === 0 ? (
        <EmptyState message="No strategies yet. The agent will develop its first strategy during cycle 1." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {strategies.map((s, idx) => {
            const roi = s.invested > 0 ? ((s.returned - s.invested) / s.invested) * 100 : 0;
            return (
              <div
                key={s.name}
                className="nest-card p-4 animate-fade-up"
                style={{
                  borderLeftWidth: '2px',
                  borderLeftColor: borderColor[s.status] || 'var(--nest-text-ghost)',
                  animationDelay: `${idx * 60}ms`,
                }}
              >
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>{s.name}</h3>
                  <StatusBadge status={s.status} />
                </div>
                <p className="text-xs mb-3 line-clamp-2" style={{ color: 'var(--nest-text-dim)' }}>{s.description}</p>
                <div className="grid grid-cols-3 gap-2 text-center mb-3">
                  <div>
                    <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Invested</p>
                    <p className="text-sm font-mono" style={{ color: 'var(--nest-text)' }}>{money(s.invested)}</p>
                  </div>
                  <div>
                    <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Returned</p>
                    <p className="text-sm font-mono" style={{ color: 'var(--nest-text)' }}>{money(s.returned)}</p>
                  </div>
                  <div>
                    <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>ROI</p>
                    <p className={`text-sm font-mono ${roi >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]'}`}>
                      {roi >= 0 ? '+' : ''}{roi.toFixed(1)}%
                    </p>
                  </div>
                </div>
                {s.confidence > 0 && (
                  <div className="mb-2">
                    <div className="flex justify-between text-[10px] mb-1">
                      <span style={{ color: 'var(--nest-text-ghost)' }}>Confidence</span>
                      <span className="font-mono" style={{ color: 'var(--nest-text-dim)' }}>{s.confidence}%</span>
                    </div>
                    <div className="w-full h-1.5 rounded-full" style={{ background: 'var(--nest-bg-surface)' }}>
                      <div className="h-full confidence-bar rounded-full" style={{ width: `${s.confidence}%` }} />
                    </div>
                  </div>
                )}
                {s.notes && <p className="text-[11px] italic mt-2" style={{ color: 'var(--nest-text-ghost)' }}>{s.notes}</p>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
