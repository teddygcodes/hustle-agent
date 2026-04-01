import { usePolling } from '../lib/usePolling';
import type { PipelineItem } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { money, shortDate } from '../lib/utils';

const STAGES = ['lead', 'outreach_sent', 'negotiating', 'deal_pending', 'closed_won', 'closed_lost', 'recurring'];
const STAGE_COLORS: Record<string, string> = {
  lead: 'border-t-blue-500',
  outreach_sent: 'border-t-sky-500',
  negotiating: 'border-t-amber-500',
  deal_pending: 'border-t-orange-500',
  closed_won: 'border-t-emerald-500',
  closed_lost: 'border-t-red-500',
  recurring: 'border-t-violet-500',
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
        subtitle={items.length > 0 ? `${activeItems.length} active deals · ${money(totalValue)} expected value` : undefined}
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
                <div className={`border-t-2 ${STAGE_COLORS[stage]} bg-zinc-900 border border-zinc-800 rounded-lg p-3`}>
                  <div className="flex justify-between items-center mb-3">
                    <h3 className="text-xs font-medium text-zinc-400 uppercase tracking-wider">
                      {stage.replace(/_/g, ' ')}
                    </h3>
                    <span className="text-xs text-zinc-600 font-mono">{stageItems.length}</span>
                  </div>
                  {stageValue > 0 && (
                    <p className="text-xs text-zinc-500 mb-3 font-mono">{money(stageValue)}</p>
                  )}
                  <div className="space-y-2">
                    {stageItems.map(item => (
                      <div key={item.id} className="bg-zinc-800/50 rounded-md p-2.5">
                        <p className="text-sm text-zinc-200 mb-1">{item.name}</p>
                        <p className="text-xs text-zinc-500">{item.strategy}</p>
                        {item.expected_value > 0 && (
                          <p className="text-xs text-emerald-400 font-mono mt-1">{money(item.expected_value)}</p>
                        )}
                        {item.expected_close_date && (
                          <p className="text-xs text-zinc-600 mt-1">{shortDate(item.expected_close_date)}</p>
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
