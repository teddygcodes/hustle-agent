import { usePolling } from '../lib/usePolling';
import type { InstinctsData, Action, PriorsData } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import clsx from 'clsx';

const trendArrow = (t: string) =>
  t === 'improving' ? '\u2191' : t === 'declining' ? '\u2193' : '\u2192';

function pctStr(n: number) { return `${(n * 100).toFixed(0)}%`; }
function roiStr(n: number) { return `${(n * 100).toFixed(0)}%`; }

export default function Instincts() {
  const { data: instincts, lastUpdated, refresh } = usePolling<InstinctsData>('/api/instincts');
  const { data: actions } = usePolling<Action[]>('/api/actions');
  const { data: priors } = usePolling<PriorsData>('/api/priors');

  const hasData = instincts && instincts.last_computed;
  const mode = instincts?.exploration_mode || 'explore';
  const sentences = instincts?.instinct_sentences || [];
  const catScores = instincts?.category_scores || {};
  const dimScores = instincts?.dimension_scores || {};
  const patterns = instincts?.cross_patterns || [];
  const calibration = instincts?.calibration || { overall: 1.0, per_category: {} };
  const history = instincts?.history || [];

  const totalActions = actions?.length || 0;
  const resolvedActions = actions?.filter(a => a.status !== 'pending').length || 0;

  const catCounts: Record<string, number> = {};
  (actions || []).filter(a => a.status !== 'pending').forEach(a => {
    catCounts[a.category] = (catCounts[a.category] || 0) + 1;
  });
  const catsReady = Object.values(catCounts).filter(c => c >= 5).length;

  return (
    <div>
      <PageHeader title="Instincts" lastUpdated={lastUpdated} onRefresh={refresh} />

      {/* Mode Banner */}
      <div className="rounded-xl p-4 mb-6 flex items-center justify-between"
        style={{
          background: mode === 'exploit' ? 'rgba(16, 185, 129, 0.06)' : 'rgba(245, 158, 11, 0.06)',
          border: `1px solid ${mode === 'exploit' ? 'rgba(16, 185, 129, 0.2)' : 'rgba(245, 158, 11, 0.2)'}`,
        }}>
        <div>
          <span className="text-xs font-bold uppercase tracking-wider"
            style={{ color: mode === 'exploit' ? 'var(--nest-success)' : 'var(--nest-warning)' }}>
            {mode === 'exploit' ? 'Exploit Mode' : 'Exploration Mode'}
          </span>
          <p className="text-sm mt-1" style={{ color: 'var(--nest-text-dim)' }}>
            {mode === 'exploit'
              ? `Leveraging ${resolvedActions} resolved actions across ${Object.keys(catScores).length} categories.`
              : `Building data: ${resolvedActions} actions resolved. Need 5+ in 3+ categories (${catsReady}/3 ready).`
            }
          </p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-bold font-mono" style={{ color: 'var(--nest-text-bright)' }}>{resolvedActions}</p>
          <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>resolved</p>
        </div>
      </div>

      {/* Exploration Progress */}
      {mode === 'explore' && Object.keys(catCounts).length > 0 && (
        <div className="nest-card p-4 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Category Coverage</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(catCounts).sort((a, b) => b[1] - a[1]).map(([cat, count]) => (
              <div key={cat} className="flex items-center gap-2">
                <div className="flex-1">
                  <div className="flex justify-between text-xs mb-1">
                    <span style={{ color: 'var(--nest-text-dim)' }}>{cat}</span>
                    <span className="font-mono" style={{ color: 'var(--nest-text-ghost)' }}>{Math.min(count, 5)}/5</span>
                  </div>
                  <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--nest-bg-surface)' }}>
                    <div
                      className="h-full rounded-full transition-all"
                      style={{
                        width: `${Math.min(count / 5 * 100, 100)}%`,
                        background: count >= 5 ? 'var(--nest-success)' : 'var(--nest-warning)',
                      }}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Instinct Sentences */}
      {sentences.length > 0 && (
        <div className="nest-card p-5 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>What Your Data Says</h3>
          <ul className="space-y-2">
            {sentences.map((s, i) => (
              <li key={i} className="text-sm flex items-start gap-2" style={{ color: 'var(--nest-text)' }}>
                <span className="mt-0.5 shrink-0" style={{ color: 'var(--nest-blue)' }}>{'\u2022'}</span>
                {s}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Category Scores Table */}
      {Object.keys(catScores).length > 0 && (
        <div className="nest-card p-5 mb-6 overflow-x-auto">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Category Performance</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs" style={{ borderBottom: '1px solid var(--nest-border)', color: 'var(--nest-text-dim)' }}>
                <th className="text-left py-2 pr-4">Category</th>
                <th className="text-right px-3">Win Rate</th>
                <th className="text-right px-3">Avg ROI</th>
                <th className="text-right px-3">Avg Time</th>
                <th className="text-right px-3">Trend</th>
                <th className="text-right px-3">Calibration</th>
                <th className="text-right px-3">n</th>
                <th className="text-right pl-3">Source</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(catScores)
                .sort((a, b) => b[1].sample_size - a[1].sample_size)
                .map(([cat, s]) => {
                  const catCal = calibration.per_category[cat];
                  return (
                    <tr key={cat} style={{ borderBottom: '1px solid var(--nest-border-subtle)' }}>
                      <td className="py-2 pr-4 font-medium" style={{ color: 'var(--nest-text)' }}>{cat}</td>
                      <td className={clsx('text-right px-3 font-mono', s.win_rate >= 0.5 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>
                        {pctStr(s.win_rate)}
                      </td>
                      <td className={clsx('text-right px-3 font-mono', s.avg_roi >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>
                        {roiStr(s.avg_roi)}
                      </td>
                      <td className="text-right px-3 font-mono" style={{ color: 'var(--nest-text-dim)' }}>{s.avg_return_time_days.toFixed(1)}d</td>
                      <td className="text-right px-3" style={{ color: s.trend === 'improving' ? 'var(--nest-success)' : s.trend === 'declining' ? 'var(--nest-error)' : 'var(--nest-text-ghost)' }}>
                        {trendArrow(s.trend)} {s.trend !== 'insufficient_data' ? s.trend : '-'}
                      </td>
                      <td className="text-right px-3 font-mono" style={{ color: 'var(--nest-text-dim)' }}>
                        {catCal ? `${catCal.toFixed(2)}x` : '-'}
                      </td>
                      <td className="text-right px-3 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>{s.sample_size}</td>
                      <td className="text-right pl-3">
                        <span className={clsx(
                          'text-[10px] px-1.5 py-0.5 rounded',
                          s.sample_size >= 5 ? 'bg-emerald-900/50 text-emerald-400' :
                          s.sample_size > 0 ? 'bg-amber-900/50 text-amber-400' :
                          'text-[var(--nest-text-ghost)]'
                        )} style={s.sample_size === 0 ? { background: 'var(--nest-bg-surface)' } : undefined}>
                          {s.sample_size >= 5 ? 'earned' : s.sample_size > 0 ? 'blend' : 'prior'}
                        </span>
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      )}

      {/* Calibration Overview */}
      {calibration.overall !== 1.0 && (
        <div className="nest-card p-5 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Calibration</h3>
          <div className="flex items-center gap-6">
            <div>
              <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>Overall Multiplier</p>
              <p className="text-2xl font-bold font-mono"
                style={{ color: calibration.overall < 0.8 ? 'var(--nest-error)' : calibration.overall > 1.1 ? 'var(--nest-success)' : 'var(--nest-text)' }}>
                {calibration.overall.toFixed(2)}x
              </p>
            </div>
            <div className="text-sm" style={{ color: 'var(--nest-text-dim)' }}>
              {calibration.overall < 1.0
                ? `When you feel 80% confident, you're actually right ${(80 * calibration.overall).toFixed(0)}% of the time.`
                : `You tend to underestimate \u2014 you hit more often than you predict.`
              }
            </div>
          </div>
          {Object.keys(calibration.per_category).length > 1 && (
            <div className="mt-4 flex flex-wrap gap-3">
              {Object.entries(calibration.per_category)
                .sort((a, b) => a[1] - b[1])
                .map(([cat, cal]) => (
                  <div key={cat} className="px-3 py-1.5 rounded text-xs font-mono"
                    style={{
                      background: cal < 0.7 ? 'rgba(239, 68, 68, 0.1)' : cal > 1.1 ? 'rgba(16, 185, 129, 0.1)' : 'var(--nest-bg-surface)',
                      color: cal < 0.7 ? 'var(--nest-error)' : cal > 1.1 ? 'var(--nest-success)' : 'var(--nest-text-dim)',
                    }}>
                    {cat}: {cal.toFixed(2)}x
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Dimension Heatmap */}
      {Object.keys(dimScores).length > 0 && (
        <div className="nest-card p-5 mb-6 overflow-x-auto">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Dimension Analysis</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(dimScores).map(([dim, buckets]) => (
              <div key={dim}>
                <p className="text-xs mb-2" style={{ color: 'var(--nest-text-ghost)' }}>{dim.replace(/_/g, ' ')}</p>
                <div className="space-y-1">
                  {Object.entries(buckets)
                    .sort((a, b) => b[1].win_rate - a[1].win_rate)
                    .map(([bucket, data]) => (
                      <div key={bucket} className="flex items-center gap-2">
                        <span className="text-xs w-20 shrink-0" style={{ color: 'var(--nest-text-dim)' }}>{bucket.replace(/_/g, ' ')}</span>
                        <div className="flex-1 h-4 rounded-sm overflow-hidden relative" style={{ background: 'var(--nest-bg-surface)' }}>
                          <div
                            className="h-full rounded-sm"
                            style={{
                              width: `${data.win_rate * 100}%`,
                              background: data.win_rate >= 0.6 ? 'var(--nest-success)' : data.win_rate >= 0.4 ? 'var(--nest-warning)' : 'var(--nest-error)',
                              opacity: 0.7,
                            }}
                          />
                          <span className="absolute inset-0 flex items-center justify-center text-[10px] font-mono" style={{ color: 'var(--nest-text)' }}>
                            {pctStr(data.win_rate)} (n={data.sample_size})
                          </span>
                        </div>
                      </div>
                    ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Cross Patterns */}
      {patterns.length > 0 && (
        <div className="nest-card p-5 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Cross-Patterns</h3>
          <div className="space-y-2">
            {patterns.slice(0, 10).map((p, i) => (
              <div key={i} className="flex items-center justify-between text-sm py-1.5 last:border-0"
                style={{ borderBottom: '1px solid var(--nest-border-subtle)' }}>
                <span style={{ color: 'var(--nest-text)' }}>{p.description}</span>
                <div className="flex items-center gap-3 shrink-0 ml-3">
                  <span className="font-mono text-xs"
                    style={{ color: p.win_rate >= 0.6 ? 'var(--nest-success)' : p.win_rate <= 0.35 ? 'var(--nest-error)' : 'var(--nest-text-dim)' }}>
                    {pctStr(p.win_rate)}
                  </span>
                  <span className="text-xs font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                    sig: {p.signal_strength.toFixed(2)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Priors */}
      {priors && Object.keys(priors).length > 0 && mode === 'explore' && (
        <div className="nest-card p-5 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Base Rate Priors</h3>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {Object.entries(priors).map(([cat, p]) => (
              <div key={cat} className="text-sm">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium" style={{ color: 'var(--nest-text)' }}>{cat}</span>
                  <span className={clsx(
                    'text-[10px] px-1.5 py-0.5 rounded',
                    p.validated ? 'bg-emerald-900/50 text-emerald-400' : ''
                  )} style={!p.validated ? { background: 'var(--nest-bg-surface)', color: 'var(--nest-text-ghost)' } : undefined}>
                    {p.source}
                  </span>
                </div>
                <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                  {pctStr(p.win_rate)} win, {roiStr(p.avg_roi)} ROI
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* History Timeline */}
      {history.length > 1 && (
        <div className="nest-card p-5 mb-6">
          <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Instinct Evolution</h3>
          <div className="space-y-3">
            {history.slice().reverse().slice(0, 10).map((h, i) => (
              <div key={i} className="pl-3" style={{ borderLeft: '2px solid var(--nest-border)' }}>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                    {new Date(h.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
                  </span>
                  <span className="text-xs font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
                    cal: {h.overall_calibration.toFixed(2)} | {h.action_count} actions
                  </span>
                </div>
                {h.sentences.slice(0, 2).map((s, j) => (
                  <p key={j} className="text-xs" style={{ color: 'var(--nest-text-dim)' }}>{s}</p>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {!hasData && totalActions === 0 && (
        <EmptyState message="No instinct data yet. The agent needs to take and resolve actions to build instincts." />
      )}
    </div>
  );
}
