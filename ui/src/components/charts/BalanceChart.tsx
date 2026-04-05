import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import type { Transaction } from '../../lib/types';

export function BalanceChart({ ledger }: { ledger: Transaction[] }) {
  if (!ledger.length) return null;

  const data = ledger.map(t => ({
    date: new Date(t.timestamp).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    balance: t.balance_after,
  }));

  return (
    <div className="nest-card p-5">
      <h3 className="text-sm font-medium mb-4" style={{ color: 'var(--nest-text)' }}>Balance Over Time</h3>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={data}>
          <defs>
            <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#0099ff" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#0099ff" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `$${v}`} />
          <Tooltip
            contentStyle={{ background: '#1e1e1e', border: '1px solid #2a2a2a', borderRadius: 8, fontSize: 12, color: '#e5e7eb' }}
            labelStyle={{ color: '#9ca3af' }}
            formatter={(v) => [`$${Number(v).toFixed(2)}`, 'Balance']}
          />
          <Area type="monotone" dataKey="balance" stroke="#0099ff" fill="url(#balGrad)" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
