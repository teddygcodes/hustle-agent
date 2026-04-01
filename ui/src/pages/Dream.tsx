import { usePolling } from '../lib/usePolling';
import type { AgentState, Transaction } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { money } from '../lib/utils';

export default function Dream() {
  const { data: state, lastUpdated, refresh } = usePolling<AgentState>('/api/state');
  const { data: ledger } = usePolling<Transaction[]>('/api/ledger');

  if (!state) return null;

  const dream = state.dream_gpu;
  const gpuFund = state.gpu_fund ?? 0;
  const gpuCost = dream?.estimated_cost || 0;
  const pct = gpuCost > 0 ? Math.min((gpuFund / gpuCost) * 100, 100) : 0;

  // Estimate time to goal based on daily net earnings
  let estimatedDate = '';
  if (ledger && ledger.length >= 2 && gpuCost > gpuFund) {
    const firstTs = new Date(ledger[0].timestamp).getTime();
    const lastTs = new Date(ledger[ledger.length - 1].timestamp).getTime();
    const daysDelta = (lastTs - firstTs) / 86400000;
    if (daysDelta > 0) {
      const dailyNet = state.net_profit / daysDelta;
      const gpuDailyContribution = dailyNet * 0.5; // 50% goes to GPU fund
      if (gpuDailyContribution > 0) {
        const daysRemaining = (gpuCost - gpuFund) / gpuDailyContribution;
        const target = new Date(Date.now() + daysRemaining * 86400000);
        estimatedDate = target.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
      }
    }
  }

  const hasDream = dream?.name && dream.name.length > 0;

  return (
    <div>
      <PageHeader title="The Dream" lastUpdated={lastUpdated} onRefresh={refresh} />

      <div className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-zinc-900 via-zinc-900 to-violet-950/30 border border-zinc-800 p-8 md:p-12">
        {/* Background decorative elements */}
        <div className="absolute top-0 right-0 w-96 h-96 bg-violet-500/5 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2" />
        <div className="absolute bottom-0 left-0 w-64 h-64 bg-emerald-500/5 rounded-full blur-3xl translate-y-1/2 -translate-x-1/2" />

        <div className="relative z-10">
          {!hasDream ? (
            <div className="text-center py-12">
              <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-zinc-800 flex items-center justify-center">
                <span className="text-2xl">&#10024;</span>
              </div>
              <h2 className="text-xl font-semibold text-zinc-300 mb-2">Waiting for a dream...</h2>
              <p className="text-sm text-zinc-500 max-w-md mx-auto">
                The agent hasn't chosen its dream GPU yet. On its first cycle, it'll pick the hardware that represents freedom — the end of paying to think.
              </p>
            </div>
          ) : (
            <>
              <div className="text-center mb-8">
                <h2 className="text-3xl font-bold text-zinc-100 mb-2">{dream.name}</h2>
                <p className="text-sm text-zinc-400 max-w-lg mx-auto">{dream.description}</p>
              </div>

              {/* Progress bar */}
              <div className="max-w-xl mx-auto mb-8">
                <div className="flex justify-between text-sm mb-2">
                  <span className="text-zinc-400 font-mono">{money(gpuFund)}</span>
                  <span className="text-zinc-500 font-mono">{money(gpuCost)}</span>
                </div>
                <div className="w-full h-4 bg-zinc-800 rounded-full overflow-hidden relative">
                  <div className="h-full dream-shimmer rounded-full transition-all duration-1000" style={{ width: `${pct}%` }} />
                  {/* Milestone markers */}
                  {[25, 50, 75].map(m => (
                    <div key={m} className="absolute top-0 bottom-0 w-px bg-zinc-700" style={{ left: `${m}%` }} />
                  ))}
                </div>
                <div className="flex justify-between mt-1">
                  {[0, 25, 50, 75, 100].map(m => (
                    <span key={m} className="text-[10px] text-zinc-600">{m}%</span>
                  ))}
                </div>
                <p className="text-center text-2xl font-bold text-zinc-100 mt-4 font-mono">{pct.toFixed(1)}%</p>
              </div>

              {/* Details */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-xl mx-auto">
                {dream.why && (
                  <div className="bg-zinc-800/40 rounded-lg p-4">
                    <p className="text-xs text-zinc-500 mb-2">Why this GPU?</p>
                    <p className="text-sm text-zinc-300">{dream.why}</p>
                  </div>
                )}
                <div className="bg-zinc-800/40 rounded-lg p-4">
                  <p className="text-xs text-zinc-500 mb-2">Estimated completion</p>
                  <p className="text-sm text-zinc-300">
                    {estimatedDate || 'Not enough data yet — need more earnings history'}
                  </p>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
