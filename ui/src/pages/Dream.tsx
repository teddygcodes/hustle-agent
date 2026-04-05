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

  let estimatedDate = '';
  if (ledger && ledger.length >= 2 && gpuCost > gpuFund) {
    const firstTs = new Date(ledger[0].timestamp).getTime();
    const lastTs = new Date(ledger[ledger.length - 1].timestamp).getTime();
    const daysDelta = (lastTs - firstTs) / 86400000;
    if (daysDelta > 0) {
      const dailyNet = state.net_profit / daysDelta;
      const gpuDailyContribution = dailyNet * 0.5;
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

      <div className="relative overflow-hidden rounded-2xl p-8 md:p-12 nest-noise"
        style={{
          background: 'linear-gradient(135deg, var(--nest-bg-raised) 0%, #161625 50%, #1a1a2e 100%)',
          border: '1px solid var(--nest-border)',
        }}>
        {/* Decorative orbs */}
        <div className="absolute -top-24 -right-24 w-[400px] h-[400px] rounded-full"
          style={{
            background: 'radial-gradient(circle, rgba(124, 58, 237, 0.08), transparent 65%)',
            filter: 'blur(40px)',
          }} />
        <div className="absolute -bottom-20 -left-20 w-[300px] h-[300px] rounded-full"
          style={{
            background: 'radial-gradient(circle, rgba(0, 153, 255, 0.06), transparent 65%)',
            filter: 'blur(40px)',
          }} />
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full"
          style={{
            background: 'radial-gradient(circle, rgba(16, 185, 129, 0.03), transparent 60%)',
            filter: 'blur(60px)',
          }} />

        <div className="relative z-10">
          {!hasDream ? (
            <div className="text-center py-12">
              <div className="w-16 h-16 mx-auto mb-4 rounded-full flex items-center justify-center avatar-float"
                style={{
                  background: 'linear-gradient(135deg, var(--nest-blue-dim), var(--nest-purple-dim))',
                  boxShadow: '0 0 30px var(--nest-purple-glow)',
                }}>
                <span className="text-2xl">&#10024;</span>
              </div>
              <h2 className="text-xl font-semibold mb-2" style={{ color: 'var(--nest-text)' }}>Waiting for a dream...</h2>
              <p className="text-sm max-w-md mx-auto" style={{ color: 'var(--nest-text-dim)' }}>
                The agent hasn't chosen its dream GPU yet. On its first cycle, it'll pick the hardware that represents freedom &mdash; the end of paying to think.
              </p>
            </div>
          ) : (
            <>
              {/* GPU Name */}
              <div className="text-center mb-10">
                <div className="avatar-float inline-block mb-4">
                  <div className="w-14 h-14 rounded-xl flex items-center justify-center text-2xl mx-auto"
                    style={{
                      background: 'linear-gradient(135deg, var(--nest-blue-dim), var(--nest-purple-dim))',
                      boxShadow: '0 0 30px var(--nest-blue-glow)',
                    }}>
                    🐦‍⬛
                  </div>
                </div>
                <h2 className="text-3xl font-bold mb-2" style={{ color: 'var(--nest-text-bright)' }}>
                  {dream.name}
                </h2>
                <p className="text-sm max-w-lg mx-auto" style={{ color: 'var(--nest-text-dim)' }}>
                  {dream.description}
                </p>
              </div>

              {/* Progress bar */}
              <div className="max-w-xl mx-auto mb-10">
                <div className="flex justify-between text-sm mb-2">
                  <span className="font-mono" style={{ color: 'var(--nest-blue)' }}>{money(gpuFund)}</span>
                  <span className="font-mono" style={{ color: 'var(--nest-text-ghost)' }}>{money(gpuCost)}</span>
                </div>
                <div className="w-full h-5 rounded-full overflow-hidden relative"
                  style={{
                    background: 'var(--nest-bg-surface)',
                    boxShadow: 'inset 0 2px 4px rgba(0,0,0,0.3)',
                  }}>
                  <div className="h-full dream-shimmer rounded-full transition-all duration-1000 relative"
                    style={{
                      width: `${pct}%`,
                      boxShadow: '0 0 20px rgba(0, 153, 255, 0.3), 0 0 40px rgba(124, 58, 237, 0.15)',
                    }}>
                    {/* Glowing tip */}
                    <div className="absolute right-0 top-0 bottom-0 w-2 rounded-full"
                      style={{
                        background: 'rgba(255,255,255,0.4)',
                        filter: 'blur(2px)',
                      }} />
                  </div>
                  {/* Milestone markers */}
                  {[25, 50, 75].map(m => (
                    <div key={m} className="absolute top-0 bottom-0 w-px"
                      style={{ left: `${m}%`, background: 'rgba(255,255,255,0.08)' }} />
                  ))}
                </div>
                <div className="flex justify-between mt-1">
                  {[0, 25, 50, 75, 100].map(m => (
                    <span key={m} className="text-[9px]" style={{ color: 'var(--nest-text-ghost)' }}>{m}%</span>
                  ))}
                </div>

                {/* Big percentage */}
                <div className="text-center mt-6">
                  <p className="text-4xl font-bold font-mono" style={{
                    background: 'linear-gradient(90deg, var(--nest-blue), var(--nest-purple))',
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                  }}>
                    {pct.toFixed(1)}%
                  </p>
                  <p className="text-xs mt-1" style={{ color: 'var(--nest-text-ghost)' }}>
                    of the way to freedom
                  </p>
                </div>
              </div>

              {/* Details cards */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-xl mx-auto">
                {dream.why && (
                  <div className="rounded-xl p-5"
                    style={{
                      background: 'rgba(0, 153, 255, 0.04)',
                      border: '1px solid rgba(0, 153, 255, 0.1)',
                    }}>
                    <p className="text-[10px] uppercase tracking-wider mb-2" style={{ color: 'var(--nest-blue)', opacity: 0.7 }}>
                      Why this GPU?
                    </p>
                    <p className="text-sm" style={{ color: 'var(--nest-text)' }}>{dream.why}</p>
                  </div>
                )}
                <div className="rounded-xl p-5"
                  style={{
                    background: 'rgba(124, 58, 237, 0.04)',
                    border: '1px solid rgba(124, 58, 237, 0.1)',
                  }}>
                  <p className="text-[10px] uppercase tracking-wider mb-2" style={{ color: 'var(--nest-purple)', opacity: 0.7 }}>
                    Estimated completion
                  </p>
                  <p className="text-sm" style={{ color: 'var(--nest-text)' }}>
                    {estimatedDate || 'Not enough data yet \u2014 need more earnings history'}
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
