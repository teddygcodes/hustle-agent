import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import type { Transaction } from '../../lib/types';

export function BalanceChart({ ledger }: { ledger: Transaction[] }) {
  if (!ledger.length) return null;

  const data = ledger.map(t => ({
    date: new Date(t.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    balance: t.balance_after,
  }));

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
      <h3 className="text-sm font-medium text-zinc-300 mb-4">Balance Over Time</h3>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={data}>
          <defs>
            <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={0.3} />
              <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="date" tick={{ fill: '#71717a', fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: '#71717a', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `$${v}`} />
          <Tooltip
            contentStyle={{ background: '#18181b', border: '1px solid #27272a', borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: '#a1a1aa' }}
            formatter={(v) => [`$${Number(v).toFixed(2)}`, 'Balance']}
          />
          <Area type="monotone" dataKey="balance" stroke="#10b981" fill="url(#balGrad)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
