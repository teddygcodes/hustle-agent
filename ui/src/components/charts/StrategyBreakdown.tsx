import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts';
import type { Transaction } from '../../lib/types';

const COLORS = ['#8b5cf6', '#10b981', '#f59e0b', '#3b82f6', '#ef4444', '#ec4899', '#14b8a6'];

export function StrategyBreakdown({ ledger }: { ledger: Transaction[] }) {
  const byStrategy: Record<string, number> = {};
  for (const t of ledger) {
    if (t.strategy && (t.type === 'expense' || t.type === 'investment')) {
      byStrategy[t.strategy] = (byStrategy[t.strategy] || 0) + t.amount;
    }
  }

  const data = Object.entries(byStrategy).map(([name, value]) => ({ name, value: Math.round(value * 100) / 100 }));
  if (!data.length) return null;

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
      <h3 className="text-sm font-medium text-zinc-300 mb-4">Spend by Strategy</h3>
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie data={data} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" paddingAngle={2}>
            {data.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{ background: '#18181b', border: '1px solid #27272a', borderRadius: 8, fontSize: 12 }}
            formatter={(v) => [`$${Number(v).toFixed(2)}`]}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap gap-3 mt-2 justify-center">
        {data.map((d, i) => (
          <div key={d.name} className="flex items-center gap-1.5 text-xs text-zinc-400">
            <span className="w-2 h-2 rounded-full" style={{ background: COLORS[i % COLORS.length] }} />
            {d.name}
          </div>
        ))}
      </div>
    </div>
  );
}
