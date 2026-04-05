import { usePolling } from '../lib/usePolling';
import type { PipelineItem } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { money, shortDate } from '../lib/utils';

const STAGES = ['lead', 'outreach_sent', 'negotiating', 'deal_pending', 'closed_won', 'closed_lost', 'recurring'];
const STAGE_COLORS: Record<string, string> = {
  lead: 'var(--nest-blue)',
  outreach_sent: '#38bdf8',
  negotiating: 'var(--nest-warning)',
  deal_pending: '#fb923c',
  closed_won: 'var(--nest-success)',
  closed_lost: 'var(--nest-error)',
  recurring: 'var(--nest-purple)',
};

export default function Pipeline() {
  const { data: pipeline, lastUpdated, refresh } = usePolling<PipelineItem[]>('/api/pipeline');
  const items = pipeline || [];

  const activeItems = items.filter(i => !['closed_won', 'closed_lost'].includes(i.stage));
  const totalValue = activeItems.reduce((s, i) => s + (i.expected_value || 0), 0);

  return (
    <div>
      <PageHeader
        title="Pipeline"
        subtitle={items.length > 0 ? `${activeItems.length} active deals \u00b7 ${money(totalValue)} expected value` : undefined}
        lastUpdated={lastUpdated}
        onRefresh={refresh}
      />

      {items.length === 0 ? (
        <EmptyState message="Pipeline empty. Deals will appear here as the agent pursues opportunities." />
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-4 max-md:flex-col">
          {STAGES.map(stage => {
            const stageItems = items.filter(i => i.stage === stage);
            const stageValue = stageItems.reduce((s, i) => s + (i.expected_value || 0), 0);
            return (
              <div key={stage} className="min-w-[220px] max-md:min-w-0 flex-1">
                <div className="nest-card p-3" style={{ borderTopWidth: '2px', borderTopColor: STAGE_COLORS[stage] }}>
                  <div className="flex justify-between items-center mb-3">
                    <h3 className="text-[10px] font-medium uppercase tracking-wider" style={{ color: 'var(--nest-text-dim)' }}>
                      {stage.replace(/_/g, ' ')}
                    </h3>
                    <span className="text-[10px] font-mono" style={{ color: 'var(--nest-text-ghost)' }}>{stageItems.length}</span>
                  </div>
                  {stageValue > 0 && (
                    <p className="text-xs font-mono mb-3" style={{ color: 'var(--nest-text-dim)' }}>{money(stageValue)}</p>
                  )}
                  <div className="space-y-2">
                    {stageItems.map(item => (
                      <div key={item.id} className="rounded-md p-2.5" style={{ background: 'var(--nest-bg-surface)' }}>
                        <p className="text-sm mb-1" style={{ color: 'var(--nest-text)' }}>{item.name}</p>
                        <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>{item.strategy}</p>
                        {item.expected_value > 0 && (
                          <p className="text-xs font-mono mt-1 text-[var(--nest-success)]">{money(item.expected_value)}</p>
                        )}
                        {item.expected_close_date && (
                          <p className="text-xs mt-1" style={{ color: 'var(--nest-text-ghost)' }}>{shortDate(item.expected_close_date)}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
