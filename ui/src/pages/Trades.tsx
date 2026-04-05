import { useMemo } from 'react';
import { usePolling } from '../lib/usePolling';
import type { PaperTrade, BotState } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { money, relativeTime, shortDate } from '../lib/utils';
import clsx from 'clsx';

const PAPER_STARTING_BALANCE = 500;

function computeBalance(trades: PaperTrade[]): number {
  let balance = PAPER_STARTING_BALANCE;
  for (const t of trades) {
    const entry_cost = t.contracts * t.entry_price;
    if (t.status === 'open') {
      balance -= entry_cost;
    } else if (t.status === 'won') {
      balance -= entry_cost;
      balance += t.contracts * 1.0;
    } else if (t.status === 'lost') {
      balance -= entry_cost;
    } else if (t.status === 'exited_early') {
      balance -= entry_cost;
      balance += t.contracts * (t.exit_price ?? 0);
    }
  }
  return Math.round(balance * 100) / 100;
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    open: 'bg-blue-500/15 text-blue-400',
    won: 'bg-emerald-500/15 text-emerald-400',
    lost: 'bg-red-500/15 text-red-400',
    exited_early: 'bg-amber-500/15 text-amber-400',
  };
  const labels: Record<string, string> = {
    open: 'Open',
    won: 'Won',
    lost: 'Lost',
    exited_early: 'Exited',
  };
  return (
    <span className={clsx('text-xs font-medium px-2 py-0.5 rounded-full', styles[status] ?? 'bg-zinc-500/15 text-zinc-400')}>
      {labels[status] ?? status}
    </span>
  );
}

function PnlCell({ trade }: { trade: PaperTrade }) {
  const pnl = trade.pnl;
  if (pnl == null) {
    if (trade.status !== 'open') return <span style={{ color: 'var(--nest-text-dim)' }}>—</span>;
    return <span style={{ color: 'var(--nest-text-dim)' }}>pending</span>;
  }
  return (
    <span className={clsx('font-mono font-medium', pnl >= 0 ? 'text-emerald-400' : 'text-red-400')}>
      {pnl >= 0 ? '+' : ''}{money(pnl)}
    </span>
  );
}

export default function Trades() {
  const { data: rawTrades, lastUpdated, refresh } = usePolling<PaperTrade[]>('/api/bot/paper-trades');
  const { data: botState } = usePolling<BotState>('/api/bot/state');

  const trades = rawTrades ?? [];

  const { open, closed, stats } = useMemo(() => {
    const open = trades.filter(t => t.status === 'open');
    const closed = trades.filter(t => t.status !== 'open');

    const resolved = closed.filter(t => t.pnl != null);
    const totalPnl = resolved.reduce((s, t) => s + (t.pnl ?? 0), 0);
    const won = closed.filter(t => t.status === 'won').length;
    const winRate = closed.length > 0 ? (won / closed.length) * 100 : 0;
    const balance = computeBalance(trades);
    const netPnl = balance - PAPER_STARTING_BALANCE;

    return {
      open,
      closed: [...closed].sort((a, b) => {
        const at = a.resolved_at ?? a.created_at;
        const bt = b.resolved_at ?? b.created_at;
        return new Date(bt).getTime() - new Date(at).getTime();
      }),
      stats: { totalPnl, winRate, won, closed: closed.length, balance, netPnl },
    };
  }, [trades]);

  const lastScan = botState?.last_scan ? relativeTime(botState.last_scan) : '—';

  return (
    <div>
      <PageHeader title="Paper Trades" lastUpdated={lastUpdated} onRefresh={refresh} />

      {/* Bot status bar */}
      <div className="flex items-center gap-4 mb-4 text-xs" style={{ color: 'var(--nest-text-dim)' }}>
        <span>Last scan: {lastScan}</span>
        {botState?.dk_disabled && <span className="text-amber-400">DK disabled</span>}
        {botState?.fd_disabled && <span className="text-amber-400">FD disabled</span>}
        <span className="ml-auto">Paper mode · $500 start</span>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="nest-card p-4">
          <div className="text-xs mb-1" style={{ color: 'var(--nest-text-dim)' }}>Balance</div>
          <div className={clsx('text-xl font-mono font-semibold', stats.balance >= PAPER_STARTING_BALANCE ? 'text-emerald-400' : 'text-red-400')}>
            {money(stats.balance)}
          </div>
          <div className={clsx('text-xs font-mono mt-0.5', stats.netPnl >= 0 ? 'text-emerald-400' : 'text-red-400')}>
            {stats.netPnl >= 0 ? '+' : ''}{money(stats.netPnl)} net
          </div>
        </div>

        <div className="nest-card p-4">
          <div className="text-xs mb-1" style={{ color: 'var(--nest-text-dim)' }}>Win Rate</div>
          <div className={clsx('text-xl font-mono font-semibold', stats.winRate >= 55 ? 'text-emerald-400' : stats.winRate >= 45 ? 'text-amber-400' : 'text-red-400')}>
            {stats.closed > 0 ? `${stats.winRate.toFixed(0)}%` : '—'}
          </div>
          <div className="text-xs mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>
            {stats.won}W / {stats.closed - stats.won}L
          </div>
        </div>

        <div className="nest-card p-4">
          <div className="text-xs mb-1" style={{ color: 'var(--nest-text-dim)' }}>Open Positions</div>
          <div className="text-xl font-mono font-semibold" style={{ color: 'var(--nest-text)' }}>
            {open.length}
          </div>
          <div className="text-xs mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>
            {trades.length} total trades
          </div>
        </div>

        <div className="nest-card p-4">
          <div className="text-xs mb-1" style={{ color: 'var(--nest-text-dim)' }}>Realized P&L</div>
          <div className={clsx('text-xl font-mono font-semibold', stats.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400')}>
            {stats.closed > 0 ? (stats.totalPnl >= 0 ? '+' : '') + money(stats.totalPnl) : '—'}
          </div>
          <div className="text-xs mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>closed trades</div>
        </div>
      </div>

      {trades.length === 0 ? (
        <EmptyState message="No paper trades yet. The bot will record trades here once it finds edges and places paper orders." />
      ) : (
        <>
          {/* Open positions */}
          {open.length > 0 && (
            <div className="mb-6">
              <h2 className="text-sm font-semibold mb-3" style={{ color: 'var(--nest-text)' }}>
                Open Positions ({open.length})
              </h2>
              <div className="nest-card overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b" style={{ borderColor: 'var(--nest-border)' }}>
                      {['Market', 'Side', 'Contracts', 'Entry', 'Strategy', 'Opened'].map(h => (
                        <th key={h} className="text-left px-4 py-3 text-xs font-medium" style={{ color: 'var(--nest-text-dim)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {open.map((t, i) => (
                      <tr
                        key={t.id ?? i}
                        className="border-b last:border-0 hover:bg-white/5 transition-colors"
                        style={{ borderColor: 'var(--nest-border)' }}
                      >
                        <td className="px-4 py-3">
                          <div className="font-mono text-xs font-medium" style={{ color: 'var(--nest-text)' }}>{t.ticker}</div>
                          {t.title && (
                            <div className="text-xs mt-0.5 max-w-[200px] truncate" style={{ color: 'var(--nest-text-dim)' }}>{t.title}</div>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <span className={clsx('text-xs font-semibold uppercase', t.side === 'yes' ? 'text-emerald-400' : 'text-red-400')}>
                            {t.side}
                          </span>
                        </td>
                        <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--nest-text)' }}>{t.contracts}</td>
                        <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--nest-text)' }}>
                          {Math.round(t.entry_price * 100)}¢
                        </td>
                        <td className="px-4 py-3 text-xs" style={{ color: 'var(--nest-text-dim)' }}>{t.strategy}</td>
                        <td className="px-4 py-3 text-xs" style={{ color: 'var(--nest-text-dim)' }}>{relativeTime(t.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Closed trades */}
          {closed.length > 0 && (
            <div>
              <h2 className="text-sm font-semibold mb-3" style={{ color: 'var(--nest-text)' }}>
                Trade History ({closed.length})
              </h2>
              <div className="nest-card overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b" style={{ borderColor: 'var(--nest-border)' }}>
                      {['Market', 'Side', 'Contracts', 'Entry', 'Exit', 'P&L', 'Status', 'Closed'].map(h => (
                        <th key={h} className="text-left px-4 py-3 text-xs font-medium" style={{ color: 'var(--nest-text-dim)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {closed.map((t, i) => (
                      <tr
                        key={t.id ?? i}
                        className="border-b last:border-0 hover:bg-white/5 transition-colors"
                        style={{ borderColor: 'var(--nest-border)' }}
                      >
                        <td className="px-4 py-3">
                          <div className="font-mono text-xs font-medium" style={{ color: 'var(--nest-text)' }}>{t.ticker}</div>
                          {t.title && (
                            <div className="text-xs mt-0.5 max-w-[180px] truncate" style={{ color: 'var(--nest-text-dim)' }}>{t.title}</div>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <span className={clsx('text-xs font-semibold uppercase', t.side === 'yes' ? 'text-emerald-400' : 'text-red-400')}>
                            {t.side}
                          </span>
                        </td>
                        <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--nest-text)' }}>{t.contracts}</td>
                        <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--nest-text)' }}>
                          {Math.round(t.entry_price * 100)}¢
                        </td>
                        <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--nest-text)' }}>
                          {t.exit_price != null ? `${Math.round(t.exit_price * 100)}¢` : '—'}
                        </td>
                        <td className="px-4 py-3"><PnlCell trade={t} /></td>
                        <td className="px-4 py-3"><StatusBadge status={t.status} /></td>
                        <td className="px-4 py-3 text-xs" style={{ color: 'var(--nest-text-dim)' }}>
                          {t.resolved_at ? shortDate(t.resolved_at) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
