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
    active: 'border-l-emerald-500',
    paused: 'border-l-amber-500',
    retired: 'border-l-zinc-600',
    exploring: 'border-l-blue-500',
    planned: 'border-l-blue-500',
  };

  return (
    <div>
      <PageHeader title="Strategies" lastUpdated={lastUpdated} onRefresh={refresh} />

      {strategies.length === 0 ? (
        <EmptyState message="No strategies yet. The agent will develop its first strategy during cycle 1." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {strategies.map(s => {
            const roi = s.invested > 0 ? ((s.returned - s.invested) / s.invested) * 100 : 0;
            return (
              <div
                key={s.name}
                className={`bg-zinc-900 border border-zinc-800 border-l-2 ${borderColor[s.status] || 'border-l-zinc-600'} rounded-lg p-4`}
              >
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-sm font-medium text-zinc-200">{s.name}</h3>
                  <StatusBadge status={s.status} />
                </div>
                <p className="text-xs text-zinc-500 mb-3 line-clamp-2">{s.description}</p>
                <div className="grid grid-cols-3 gap-2 text-center mb-3">
                  <div>
                    <p className="text-xs text-zinc-500">Invested</p>
                    <p className="text-sm font-mono text-zinc-300">{money(s.invested)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-zinc-500">Returned</p>
                    <p className="text-sm font-mono text-zinc-300">{money(s.returned)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-zinc-500">ROI</p>
                    <p className={`text-sm font-mono ${roi >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {roi >= 0 ? '+' : ''}{roi.toFixed(1)}%
                    </p>
                  </div>
                </div>
                {s.confidence > 0 && (
                  <div className="mb-2">
                    <div className="flex justify-between text-xs text-zinc-500 mb-1">
                      <span>Confidence</span>
                      <span>{s.confidence}%</span>
                    </div>
                    <div className="w-full h-1 bg-zinc-800 rounded-full">
                      <div className="h-full bg-violet-500 rounded-full" style={{ width: `${s.confidence}%` }} />
                    </div>
                  </div>
                )}
                {s.notes && <p className="text-xs text-zinc-600 italic mt-2">{s.notes}</p>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
