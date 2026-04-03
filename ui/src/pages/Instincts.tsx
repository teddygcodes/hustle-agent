import { usePolling } from '../lib/usePolling';
import type { InstinctsData, Action, PriorsData } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import clsx from 'clsx';

const trendArrow = (t: string) =>
  t === 'improving' ? '\u2191' : t === 'declining' ? '\u2193' : '\u2192';

const trendColor = (t: string) =>
  t === 'improving' ? 'text-emerald-400' : t === 'declining' ? 'text-red-400' : 'text-zinc-500';

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

  // Exploration progress
  const catCounts: Record<string, number> = {};
  (actions || []).filter(a => a.status !== 'pending').forEach(a => {
    catCounts[a.category] = (catCounts[a.category] || 0) + 1;
  });
  const catsReady = Object.values(catCounts).filter(c => c >= 5).length;

  return (
    <div>
      <PageHeader title="Instincts" lastUpdated={lastUpdated} onRefresh={refresh} />

      {/* Mode Banner */}
      <div className={clsx(
        'rounded-lg border p-4 mb-6 flex items-center justify-between',
        mode === 'exploit'
          ? 'bg-emerald-950/30 border-emerald-800'
          : 'bg-amber-950/30 border-amber-800'
      )}>
        <div>
          <span className={clsx(
            'text-xs font-bold uppercase tracking-wider',
            mode === 'exploit' ? 'text-emerald-400' : 'text-amber-400'
          )}>
            {mode === 'exploit' ? 'Exploit Mode' : 'Exploration Mode'}
          </span>
          <p className="text-sm text-zinc-400 mt-1">
            {mode === 'exploit'
              ? `Leveraging ${resolvedActions} resolved actions across ${Object.keys(catScores).length} categories.`
              : `Building data: ${resolvedActions} actions resolved. Need 5+ in 3+ categories (${catsReady}/3 ready).`
            }
          </p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-bold font-mono text-zinc-100">{resolvedActions}</p>
          <p className="text-xs text-zinc-500">resolved</p>
        </div>
      </div>

      {/* Exploration Progress (explore mode only) */}
      {mode === 'explore' && Object.keys(catCounts).length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Category Coverage</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(catCounts).sort((a, b) => b[1] - a[1]).map(([cat, count]) => (
              <div key={cat} className="flex items-center gap-2">
                <div className="flex-1">
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-zinc-400">{cat}</span>
                    <span className="text-zinc-500 font-mono">{Math.min(count, 5)}/5</span>
                  </div>
                  <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                    <div
                      className={clsx('h-full rounded-full transition-all', count >= 5 ? 'bg-emerald-500' : 'bg-amber-500')}
                      style={{ width: `${Math.min(count / 5 * 100, 100)}%` }}
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
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">What Your Data Says</h3>
          <ul className="space-y-2">
            {sentences.map((s, i) => (
              <li key={i} className="text-sm text-zinc-300 flex items-start gap-2">
                <span className="text-violet-400 mt-0.5 shrink-0">{'\u2022'}</span>
                {s}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Category Scores Table */}
      {Object.keys(catScores).length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6 overflow-x-auto">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Category Performance</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-zinc-500 text-xs border-b border-zinc-800">
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
                  const prior = priors?.[cat];
                  const catCal = calibration.per_category[cat];
                  return (
                    <tr key={cat} className="border-b border-zinc-800/50">
                      <td className="py-2 pr-4 text-zinc-200 font-medium">{cat}</td>
                      <td className={clsx('text-right px-3 font-mono', s.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400')}>
                        {pctStr(s.win_rate)}
                      </td>
                      <td className={clsx('text-right px-3 font-mono', s.avg_roi >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                        {roiStr(s.avg_roi)}
                      </td>
                      <td className="text-right px-3 text-zinc-400 font-mono">{s.avg_return_time_days.toFixed(1)}d</td>
                      <td className={clsx('text-right px-3', trendColor(s.trend))}>
                        {trendArrow(s.trend)} {s.trend !== 'insufficient_data' ? s.trend : '-'}
                      </td>
                      <td className="text-right px-3 text-zinc-400 font-mono">
                        {catCal ? `${catCal.toFixed(2)}x` : '-'}
                      </td>
                      <td className="text-right px-3 text-zinc-500 font-mono">{s.sample_size}</td>
                      <td className="text-right pl-3">
                        <span className={clsx(
                          'text-[10px] px-1.5 py-0.5 rounded',
                          s.sample_size >= 5 ? 'bg-emerald-900/50 text-emerald-400' :
                          s.sample_size > 0 ? 'bg-amber-900/50 text-amber-400' :
                          'bg-zinc-800 text-zinc-500'
                        )}>
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
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Calibration</h3>
          <div className="flex items-center gap-6">
            <div>
              <p className="text-xs text-zinc-500">Overall Multiplier</p>
              <p className={clsx(
                'text-2xl font-bold font-mono',
                calibration.overall < 0.8 ? 'text-red-400' : calibration.overall > 1.1 ? 'text-emerald-400' : 'text-zinc-200'
              )}>
                {calibration.overall.toFixed(2)}x
              </p>
            </div>
            <div className="text-sm text-zinc-400">
              {calibration.overall < 1.0
                ? `When you feel 80% confident, you're actually right ${(80 * calibration.overall).toFixed(0)}% of the time.`
                : `You tend to underestimate — you hit more often than you predict.`
              }
            </div>
          </div>
          {Object.keys(calibration.per_category).length > 1 && (
            <div className="mt-4 flex flex-wrap gap-3">
              {Object.entries(calibration.per_category)
                .sort((a, b) => a[1] - b[1])
                .map(([cat, cal]) => (
                  <div key={cat} className={clsx(
                    'px-3 py-1.5 rounded text-xs font-mono',
                    cal < 0.7 ? 'bg-red-900/30 text-red-400' :
                    cal > 1.1 ? 'bg-emerald-900/30 text-emerald-400' :
                    'bg-zinc-800 text-zinc-400'
                  )}>
                    {cat}: {cal.toFixed(2)}x
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Dimension Heatmap */}
      {Object.keys(dimScores).length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6 overflow-x-auto">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Dimension Analysis</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(dimScores).map(([dim, buckets]) => (
              <div key={dim}>
                <p className="text-xs text-zinc-500 mb-2">{dim.replace(/_/g, ' ')}</p>
                <div className="space-y-1">
                  {Object.entries(buckets)
                    .sort((a, b) => b[1].win_rate - a[1].win_rate)
                    .map(([bucket, data]) => (
                      <div key={bucket} className="flex items-center gap-2">
                        <span className="text-xs text-zinc-400 w-20 shrink-0">{bucket.replace(/_/g, ' ')}</span>
                        <div className="flex-1 h-4 bg-zinc-800 rounded-sm overflow-hidden relative">
                          <div
                            className={clsx(
                              'h-full rounded-sm',
                              data.win_rate >= 0.6 ? 'bg-emerald-600' :
                              data.win_rate >= 0.4 ? 'bg-amber-600' :
                              'bg-red-600'
                            )}
                            style={{ width: `${data.win_rate * 100}%` }}
                          />
                          <span className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-300 font-mono">
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
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Cross-Patterns</h3>
          <div className="space-y-2">
            {patterns.slice(0, 10).map((p, i) => (
              <div key={i} className="flex items-center justify-between text-sm py-1.5 border-b border-zinc-800/50 last:border-0">
                <span className="text-zinc-300">{p.description}</span>
                <div className="flex items-center gap-3 shrink-0 ml-3">
                  <span className={clsx(
                    'font-mono text-xs',
                    p.win_rate >= 0.6 ? 'text-emerald-400' : p.win_rate <= 0.35 ? 'text-red-400' : 'text-zinc-400'
                  )}>
                    {pctStr(p.win_rate)}
                  </span>
                  <span className="text-zinc-600 text-xs font-mono">
                    sig: {p.signal_strength.toFixed(2)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Priors (if still using borrowed wisdom) */}
      {priors && Object.keys(priors).length > 0 && mode === 'explore' && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Base Rate Priors</h3>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {Object.entries(priors).map(([cat, p]) => (
              <div key={cat} className="text-sm">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-zinc-200 font-medium">{cat}</span>
                  <span className={clsx(
                    'text-[10px] px-1.5 py-0.5 rounded',
                    p.validated ? 'bg-emerald-900/50 text-emerald-400' : 'bg-zinc-800 text-zinc-500'
                  )}>
                    {p.source}
                  </span>
                </div>
                <p className="text-xs text-zinc-500">
                  {pctStr(p.win_rate)} win, {roiStr(p.avg_roi)} ROI
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* History Timeline */}
      {history.length > 1 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
          <h3 className="text-sm font-medium text-zinc-300 mb-3">Instinct Evolution</h3>
          <div className="space-y-3">
            {history.slice().reverse().slice(0, 10).map((h, i) => (
              <div key={i} className="border-l-2 border-zinc-700 pl-3">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs text-zinc-500">
                    {new Date(h.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
                  </span>
                  <span className="text-xs text-zinc-600 font-mono">
                    cal: {h.overall_calibration.toFixed(2)} | {h.action_count} actions
                  </span>
                </div>
                {h.sentences.slice(0, 2).map((s, j) => (
                  <p key={j} className="text-xs text-zinc-400">{s}</p>
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
