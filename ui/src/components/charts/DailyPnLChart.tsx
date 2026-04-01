import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import type { Transaction } from '../../lib/types';

export function DailyPnLChart({ ledger }: { ledger: Transaction[] }) {
  if (!ledger.length) return null;

  const byDay: Record<string, number> = {};
  for (const t of ledger) {
    const day = new Date(t.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const delta = t.type === 'income' || t.type === 'return' ? t.amount : -t.amount;
    byDay[day] = (byDay[day] || 0) + delta;
  }

  const data = Object.entries(byDay).map(([date, pnl]) => ({ date, pnl: Math.round(pnl * 100) / 100 }));

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
      <h3 className="text-sm font-medium text-zinc-300 mb-4">Daily P&L</h3>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data}>
          <XAxis dataKey="date" tick={{ fill: '#71717a', fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: '#71717a', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `$${v}`} />
          <Tooltip
            contentStyle={{ background: '#18181b', border: '1px solid #27272a', borderRadius: 8, fontSize: 12 }}
            formatter={(v) => [`$${Number(v).toFixed(2)}`, 'P&L']}
          />
          <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.pnl >= 0 ? '#10b981' : '#ef4444'} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
