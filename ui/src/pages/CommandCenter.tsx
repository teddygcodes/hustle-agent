import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  Zap, TrendingUp, Target,
  BookOpen, MessageCircle, Lightbulb, Flame, ArrowRight
} from 'lucide-react';
import { usePolling } from '../lib/usePolling';
import type {
  AgentState, AgentEvent, CostEntry, Projection,
  PipelineItem, MemoryData, Conversation
} from '../lib/types';
import { StatusBadge } from '../components/StatusBadge';
import { VerdictBadge } from '../components/VerdictBadge';
import { money, relativeTime, getRiskPosture } from '../lib/utils';

/* ─── Journal parser (reused from Journal page) ─── */
function parseJournalEntries(content: string): { heading: string; body: string }[] {
  if (!content.trim()) return [];
  return content.split(/(?=^## )/m).filter(p => p.trim())
    .map(part => {
      const lines = part.split('\n');
      const heading = (lines[0] || '').replace(/^##\s*/, '').trim();
      const body = lines.slice(1).join('\n').trim();
      return { heading, body };
    })
    .filter(e => e.body.length > 0)
    .reverse();
}

export default function CommandCenter() {
  const { data: state } = usePolling<AgentState>('/api/state');
  const { data: events } = usePolling<AgentEvent[]>('/api/events');
  const { data: costs } = usePolling<CostEntry[]>('/api/costs');
  const { data: projections } = usePolling<Projection[]>('/api/projections');
  const { data: pipeline } = usePolling<PipelineItem[]>('/api/pipeline');
  const { data: memory } = usePolling<MemoryData>('/api/memory');
  const { data: journal } = usePolling<{ content: string }>('/api/journal');
  const { data: conversations } = usePolling<Conversation[]>('/api/conversations');

  // Days to GPU estimate — must be before early return to maintain hook order
  const daysToGpu = useMemo(() => {
    if (!state || state.net_profit <= 0 || !state.dream_gpu?.estimated_cost) return null;
    const remaining = state.dream_gpu.estimated_cost - state.gpu_fund;
    if (remaining <= 0) return 0;
    const createdAt = new Date(state.created_at).getTime();
    const daysSinceCreation = (Date.now() - createdAt) / 86400000;
    if (daysSinceCreation < 0.1) return null;
    const dailyNetGpu = (state.net_profit * 0.5) / daysSinceCreation;
    if (dailyNetGpu <= 0) return null;
    return Math.ceil(remaining / dailyNetGpu);
  }, [state]);

  const journalEntries = useMemo(() => parseJournalEntries(journal?.content || '').slice(0, 4), [journal]);

  if (!state) return null;

  const totalCost = costs?.reduce((s, c) => s + c.cost, 0) ?? 0;
  const cycleCount = state.cycle || 0;
  const avgCycleCost = cycleCount > 0 ? totalCost / cycleCount : 0;
  const risk = getRiskPosture(state.balance);
  const recentEvents = (events || []).slice(0, 8);
  const pendingProjections = (projections || []).filter(p => p.status === 'pending');
  const pipelineItems = pipeline || [];
  const activeDeals = pipelineItems.filter(i => !['closed_won', 'closed_lost'].includes(i.stage));

  const lastEventType = recentEvents[0]?.event_type || '';
  const isEarning = lastEventType.includes('income') || lastEventType.includes('return');
  const gpuPct = state.dream_gpu?.estimated_cost
    ? Math.min((state.gpu_fund / state.dream_gpu.estimated_cost) * 100, 100)
    : 0;

  const researchItems = (memory?.research_cache || []).slice(0, 6);
  const lessons = (memory?.lessons || []).slice(0, 6);
  const tylerMessages = (conversations || []).filter(c => c.from === 'tyler').slice(-4).reverse();

  const strategies = state.strategies || [];

  return (
    <div className="space-y-6">

      {/* ═══════════ FLIGHT DASHBOARD ═══════════ */}
      <section className="nest-gradient-top rounded-2xl p-6 relative overflow-hidden nest-noise">
        {/* Decorative orbs */}
        <div className="absolute -top-20 -right-20 w-64 h-64 rounded-full opacity-30"
          style={{ background: 'radial-gradient(circle, var(--nest-blue-glow), transparent 70%)' }} />
        <div className="absolute -bottom-16 -left-16 w-48 h-48 rounded-full opacity-20"
          style={{ background: 'radial-gradient(circle, var(--nest-purple-glow), transparent 70%)' }} />

        <div className="relative z-10">
          {/* Agent identity row */}
          <div className="flex items-center gap-4 mb-6">
            <div className="avatar-float">
              <div className="w-12 h-12 rounded-xl flex items-center justify-center text-xl"
                style={{
                  background: 'linear-gradient(135deg, var(--nest-blue-dim), var(--nest-purple-dim))',
                  boxShadow: '0 0 20px var(--nest-blue-glow)',
                }}>
                🐦‍⬛
              </div>
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold" style={{ color: 'var(--nest-text-bright)' }}>
                  {state.name || 'Unnamed Agent'}
                </h1>
                <StatusBadge status={state.status || 'offline'} />
              </div>
              <div className="flex items-center gap-3 mt-1">
                {state.avatar?.creature && (
                  <span className="text-xs" style={{ color: 'var(--nest-text-dim)' }}>
                    the {state.avatar.creature}
                  </span>
                )}
                {state.mood && (
                  <span className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                    {state.mood}
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* Main metrics row */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
            {/* Balance - the star metric */}
            <div className={`nest-card p-4 relative overflow-hidden ${isEarning ? 'glow-earning' : ''}`}>
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>
                Balance
              </p>
              <p className="text-2xl font-bold font-mono" style={{ color: 'var(--nest-blue)' }}>
                {money(state.balance)}
              </p>
              {/* GPU progress inline */}
              <div className="mt-3">
                <div className="w-full h-2 rounded-full overflow-hidden" style={{ background: 'var(--nest-bg-surface)' }}>
                  <div className="h-full dream-shimmer rounded-full transition-all duration-1000"
                    style={{ width: `${gpuPct}%` }} />
                </div>
                <p className="text-[10px] mt-1 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                  GPU: {gpuPct.toFixed(1)}% — {money(state.gpu_fund)} / {money(state.dream_gpu?.estimated_cost || 0)}
                </p>
              </div>
            </div>

            {/* Net Profit */}
            <div className="nest-card p-4">
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>
                Net Profit
              </p>
              <p className={`text-xl font-semibold font-mono ${state.net_profit >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]'}`}>
                {state.net_profit >= 0 ? '+' : ''}{money(state.net_profit)}
              </p>
              <p className="text-[10px] mt-1" style={{ color: 'var(--nest-text-dim)' }}>
                ROI: {state.roi_percent?.toFixed(1) || 0}%
              </p>
            </div>

            {/* Burn Rate */}
            <div className="nest-card p-4">
              <div className="flex items-center gap-1.5 mb-1">
                <Flame size={11} style={{ color: 'var(--nest-warning)' }} />
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>
                  Burn Rate
                </p>
              </div>
              <p className="text-xl font-semibold font-mono" style={{ color: 'var(--nest-text-bright)' }}>
                {money(avgCycleCost)}
                <span className="text-[10px] ml-1" style={{ color: 'var(--nest-text-ghost)' }}>/cycle</span>
              </p>
              <p className="text-[10px] mt-1" style={{ color: 'var(--nest-text-dim)' }}>
                {avgCycleCost > 0 ? `~${Math.floor(state.balance / avgCycleCost)} cycles remaining` : 'No data'}
              </p>
            </div>

            {/* Quick stats */}
            <div className="nest-card p-4">
              <p className="text-[10px] uppercase tracking-wider mb-2" style={{ color: 'var(--nest-text-ghost)' }}>
                Quick Stats
              </p>
              <div className="space-y-1.5">
                <div className="flex justify-between text-xs">
                  <span style={{ color: 'var(--nest-text-dim)' }}>Earned</span>
                  <span className="font-mono text-[var(--nest-success)]">{money(state.total_earned)}</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span style={{ color: 'var(--nest-text-dim)' }}>Spent</span>
                  <span className="font-mono text-[var(--nest-error)]">{money(state.total_spent)}</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span style={{ color: 'var(--nest-text-dim)' }}>Strategies</span>
                  <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{state.active_strategies?.length || 0}</span>
                </div>
                {daysToGpu !== null && (
                  <div className="flex justify-between text-xs">
                    <span style={{ color: 'var(--nest-text-dim)' }}>Days to GPU</span>
                    <span className="font-mono" style={{ color: 'var(--nest-purple)' }}>
                      {daysToGpu === 0 ? 'Done!' : `~${daysToGpu}`}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Split + Risk row */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {/* 50/50 Split */}
            <div className="nest-card p-4">
              <p className="text-[10px] uppercase tracking-wider mb-3" style={{ color: 'var(--nest-text-ghost)' }}>
                50/50 Split
              </p>
              <div className="flex gap-3">
                <div className="flex-1">
                  <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(124, 58, 237, 0.15)' }}>
                    <div className="h-full rounded-full" style={{ background: 'var(--nest-purple)', width: state.net_profit > 0 ? '100%' : '0%' }} />
                  </div>
                  <p className="text-[11px] mt-1.5" style={{ color: 'var(--nest-text-dim)' }}>
                    Tyler: <span className="font-mono">{money(state.tylers_cut)}</span>
                  </p>
                </div>
                <div className="flex-1">
                  <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(0, 153, 255, 0.15)' }}>
                    <div className="h-full rounded-full" style={{ background: 'var(--nest-blue)', width: state.net_profit > 0 ? '100%' : '0%' }} />
                  </div>
                  <p className="text-[11px] mt-1.5" style={{ color: 'var(--nest-text-dim)' }}>
                    GPU Fund: <span className="font-mono">{money(state.gpu_fund)}</span>
                  </p>
                </div>
              </div>
            </div>

            {/* Risk Posture */}
            <div className="nest-card p-4">
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>
                Risk Posture
              </p>
              <div className="flex items-center gap-3">
                <p className={`text-lg font-semibold ${risk.color}`}>{risk.label}</p>
                <div className="flex gap-1 flex-1">
                  {['Preservation', 'Normal', 'Aggressive'].map(level => (
                    <div
                      key={level}
                      className="flex-1 h-1.5 rounded-full transition-colors"
                      style={{
                        background: risk.label === level
                          ? (level === 'Aggressive' ? 'var(--nest-success)' : level === 'Normal' ? 'var(--nest-warning)' : 'var(--nest-error)')
                          : 'var(--nest-bg-surface)'
                      }}
                    />
                  ))}
                </div>
              </div>
              <p className="text-[10px] mt-1" style={{ color: 'var(--nest-text-dim)' }}>
                Cycle {cycleCount} &middot; {risk.label === 'Preservation' ? 'Spending restricted' : risk.label === 'Normal' ? 'Standard limits' : 'Full throttle'}
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ═══════════ THREE-COLUMN WORKSPACE ═══════════ */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">

        {/* ─── Left: Strategy Overview ─── */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>
              <Target size={14} className="inline mr-1.5 -mt-0.5" style={{ color: 'var(--nest-blue)' }} />
              Strategies
            </h2>
            <Link to="/strategies" className="text-[11px] flex items-center gap-1 hover:underline"
              style={{ color: 'var(--nest-text-dim)' }}>
              View all <ArrowRight size={10} />
            </Link>
          </div>

          {strategies.length === 0 ? (
            <div className="nest-card p-4 text-center">
              <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                No strategies yet
              </p>
            </div>
          ) : (
            strategies.map((s, idx) => {
              const roi = s.invested > 0 ? ((s.returned - s.invested) / s.invested) * 100 : 0;
              return (
                <div key={s.name}
                  className="nest-card p-4 animate-fade-up"
                  style={{ animationDelay: `${idx * 80}ms` }}>
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-sm font-medium truncate" style={{ color: 'var(--nest-text)' }}>
                      {s.name}
                    </h3>
                    <StatusBadge status={s.status} />
                  </div>
                  <p className="text-[11px] line-clamp-1 mb-3" style={{ color: 'var(--nest-text-dim)' }}>
                    {s.description}
                  </p>

                  {/* Confidence bar */}
                  {s.confidence > 0 && (
                    <div className="mb-2">
                      <div className="flex justify-between text-[10px] mb-1">
                        <span style={{ color: 'var(--nest-text-ghost)' }}>Confidence</span>
                        <span className="font-mono" style={{ color: 'var(--nest-text-dim)' }}>{s.confidence}%</span>
                      </div>
                      <div className="w-full h-1.5 rounded-full" style={{ background: 'var(--nest-bg-surface)' }}>
                        <div className="h-full confidence-bar rounded-full transition-all"
                          style={{ width: `${s.confidence}%` }} />
                      </div>
                    </div>
                  )}

                  {/* ROI + Investment */}
                  <div className="flex gap-4 text-[11px]">
                    <span style={{ color: 'var(--nest-text-dim)' }}>
                      Invested: <span className="font-mono">{money(s.invested)}</span>
                    </span>
                    <span className={`font-mono ${roi >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]'}`}>
                      {roi >= 0 ? '+' : ''}{roi.toFixed(0)}% ROI
                    </span>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* ─── Center: Active Workspace ─── */}
        <div className="space-y-3">
          <h2 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>
            <Zap size={14} className="inline mr-1.5 -mt-0.5" style={{ color: 'var(--nest-warning)' }} />
            Active Workspace
          </h2>

          {/* Pending projections */}
          {pendingProjections.length > 0 && (
            <div className="space-y-2">
              <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>
                Projections in Progress
              </p>
              {pendingProjections.slice(0, 3).map((p, idx) => (
                <div key={p.id} className="nest-card p-3 animate-fade-up" style={{ animationDelay: `${idx * 80}ms` }}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <p className="text-xs truncate" style={{ color: 'var(--nest-text)' }}>{p.action}</p>
                      <p className="text-[10px] mt-0.5" style={{ color: 'var(--nest-text-ghost)' }}>
                        {p.strategy_type} &middot; {money(p.cost)} cost
                      </p>
                    </div>
                    <VerdictBadge verdict={p.verdict} />
                  </div>
                  <div className="flex gap-3 mt-2 text-[10px]" style={{ color: 'var(--nest-text-dim)' }}>
                    <span>Return: <span className="font-mono text-[var(--nest-success)]">{money(p.expected_return)}</span></span>
                    <span>Conf: <span className="font-mono">{p.confidence_calibrated || p.confidence_raw}%</span></span>
                    <span>{p.time_to_return_days}d</span>
                  </div>
                </div>
              ))}
              {pendingProjections.length > 3 && (
                <Link to="/projections" className="text-[11px] flex items-center gap-1 hover:underline pl-1"
                  style={{ color: 'var(--nest-text-dim)' }}>
                  +{pendingProjections.length - 3} more <ArrowRight size={10} />
                </Link>
              )}
            </div>
          )}

          {/* Activity feed */}
          <div className="nest-card p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>
                Recent Activity
              </p>
              <span className="text-[10px] font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                Cycle {cycleCount}
              </span>
            </div>
            {recentEvents.length === 0 ? (
              <p className="text-xs py-4 text-center" style={{ color: 'var(--nest-text-ghost)' }}>
                Awaiting first cycle...
              </p>
            ) : (
              <div className="space-y-2">
                {recentEvents.map((e, i) => (
                  <div key={i} className="flex items-start gap-2.5 text-[11px]">
                    <span className="font-mono shrink-0 pt-0.5" style={{ color: 'var(--nest-text-ghost)', width: '48px' }}>
                      {relativeTime(e.timestamp)}
                    </span>
                    <span className="shrink-0 truncate" style={{ color: 'var(--nest-blue)', width: '80px', opacity: 0.8 }}>
                      {e.event_type}
                    </span>
                    <span className="truncate" style={{ color: 'var(--nest-text-dim)' }}>
                      {typeof e.data === 'object' && e.data ? JSON.stringify(e.data).slice(0, 80) : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* ─── Right: Opportunity Pipeline ─── */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>
              <TrendingUp size={14} className="inline mr-1.5 -mt-0.5" style={{ color: 'var(--nest-success)' }} />
              Pipeline
            </h2>
            <Link to="/pipeline" className="text-[11px] flex items-center gap-1 hover:underline"
              style={{ color: 'var(--nest-text-dim)' }}>
              View all <ArrowRight size={10} />
            </Link>
          </div>

          {activeDeals.length === 0 && pipelineItems.length === 0 ? (
            <div className="nest-card p-4 text-center">
              <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                Pipeline empty
              </p>
            </div>
          ) : (
            <>
              {/* Active pipeline value */}
              {activeDeals.length > 0 && (
                <div className="nest-card p-3 data-shimmer">
                  <div className="flex justify-between items-center relative z-10">
                    <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>
                      Expected Value
                    </span>
                    <span className="text-sm font-semibold font-mono" style={{ color: 'var(--nest-success)' }}>
                      {money(activeDeals.reduce((s, i) => s + (i.expected_value || 0), 0))}
                    </span>
                  </div>
                </div>
              )}

              {/* Pipeline items by stage */}
              {pipelineItems.slice(0, 6).map((item, idx) => {
                const stageColors: Record<string, string> = {
                  lead: 'var(--nest-blue)',
                  outreach_sent: '#38bdf8',
                  negotiating: 'var(--nest-warning)',
                  deal_pending: '#fb923c',
                  closed_won: 'var(--nest-success)',
                  closed_lost: 'var(--nest-error)',
                  recurring: 'var(--nest-purple)',
                };
                return (
                  <div key={item.id} className="nest-card p-3 animate-fade-up" style={{ animationDelay: `${idx * 60}ms` }}>
                    <div className="flex items-center gap-2 mb-1">
                      <div className="w-2 h-2 rounded-full shrink-0"
                        style={{ background: stageColors[item.stage] || 'var(--nest-text-ghost)' }} />
                      <span className="text-xs font-medium truncate" style={{ color: 'var(--nest-text)' }}>
                        {item.name}
                      </span>
                    </div>
                    <div className="flex justify-between text-[10px] ml-4">
                      <span style={{ color: 'var(--nest-text-ghost)' }}>
                        {item.stage.replace(/_/g, ' ')} &middot; {item.strategy}
                      </span>
                      {item.expected_value > 0 && (
                        <span className="font-mono" style={{ color: 'var(--nest-success)' }}>
                          {money(item.expected_value)}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </>
          )}
        </div>
      </section>

      {/* ═══════════ THE COLLECTION ═══════════ */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <h2 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>
            The Collection
          </h2>
          <span className="text-xs" style={{ color: 'var(--nest-purple)' }}>&#10024;</span>
          <span className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>
            shiny things I've gathered
          </span>
        </div>

        <div className="flex gap-4 overflow-x-auto pb-4 snap-x snap-mandatory">
          {/* Research findings */}
          {researchItems.map((r, i) => (
            <div key={`research-${i}`}
              className="shiny-card nest-card min-w-[260px] max-w-[300px] p-4 snap-start shrink-0 animate-fade-up"
              style={{ animationDelay: `${i * 60}ms` }}>
              <div className="flex items-center gap-1.5 mb-2">
                <Lightbulb size={11} style={{ color: 'var(--nest-blue)' }} />
                <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-blue)', opacity: 0.7 }}>
                  Research
                </span>
              </div>
              <p className="text-xs font-medium mb-1 line-clamp-1" style={{ color: 'var(--nest-text)' }}>
                {r.query}
              </p>
              <p className="text-[11px] line-clamp-3" style={{ color: 'var(--nest-text-dim)' }}>
                {(r.result ?? r.results ?? '').slice(0, 150)}
              </p>
              <p className="text-[9px] mt-2 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                {relativeTime(r.timestamp)}
              </p>
            </div>
          ))}

          {/* Lessons learned */}
          {lessons.map((l, i) => (
            <div key={`lesson-${i}`}
              className="shiny-card shiny-card-purple nest-card min-w-[260px] max-w-[300px] p-4 snap-start shrink-0 animate-fade-up"
              style={{ animationDelay: `${(researchItems.length + i) * 60}ms` }}>
              <div className="flex items-center gap-1.5 mb-2">
                <BookOpen size={11} style={{ color: 'var(--nest-purple)' }} />
                <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-purple)', opacity: 0.7 }}>
                  Lesson
                </span>
              </div>
              <p className="text-[11px] line-clamp-4" style={{ color: 'var(--nest-text)' }}>
                {l.lesson ?? l.text ?? ''}
              </p>
              <p className="text-[9px] mt-2 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                {l.cycle ? `Cycle ${l.cycle}` : relativeTime(l.timestamp)}
              </p>
            </div>
          ))}

          {/* Journal entries */}
          {journalEntries.map((e, i) => (
            <div key={`journal-${i}`}
              className="shiny-card shiny-card-white nest-card min-w-[260px] max-w-[300px] p-4 snap-start shrink-0 animate-fade-up"
              style={{ animationDelay: `${(researchItems.length + lessons.length + i) * 60}ms` }}>
              <div className="flex items-center gap-1.5 mb-2">
                <BookOpen size={11} style={{ color: 'var(--nest-text-dim)' }} />
                <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-dim)' }}>
                  Journal
                </span>
              </div>
              <p className="text-xs font-medium mb-1 line-clamp-1" style={{ color: 'var(--nest-text)' }}>
                {e.heading}
              </p>
              <p className="text-[11px] line-clamp-3" style={{ color: 'var(--nest-text-dim)' }}>
                {e.body.slice(0, 120)}
              </p>
            </div>
          ))}

          {/* Tyler messages */}
          {tylerMessages.map((m, i) => (
            <div key={`tyler-${i}`}
              className="shiny-card shiny-card-purple nest-card min-w-[260px] max-w-[300px] p-4 snap-start shrink-0 animate-fade-up"
              style={{
                animationDelay: `${(researchItems.length + lessons.length + journalEntries.length + i) * 60}ms`,
                '--glow-color': 'rgba(139, 92, 246, 0.12)',
              } as React.CSSProperties}>
              <div className="flex items-center gap-1.5 mb-2">
                <MessageCircle size={11} style={{ color: 'var(--nest-purple)' }} />
                <span className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-purple)', opacity: 0.7 }}>
                  Tyler
                </span>
              </div>
              <p className="text-[11px] line-clamp-4" style={{ color: 'var(--nest-text)' }}>
                {m.message}
              </p>
              <p className="text-[9px] mt-2 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                {relativeTime(m.timestamp)}
              </p>
            </div>
          ))}

          {/* Empty state if no collection items */}
          {researchItems.length === 0 && lessons.length === 0 && journalEntries.length === 0 && tylerMessages.length === 0 && (
            <div className="nest-card p-8 text-center flex-1">
              <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                Nothing collected yet. Shiny objects will appear here as cycles run.
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
