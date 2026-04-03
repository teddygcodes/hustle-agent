import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Wallet, Target, TrendingUp, Kanban,
  Sparkles, BookOpen, MessageCircle, Lightbulb, Activity, Brain
} from 'lucide-react';
import { usePolling } from '../lib/usePolling';
import type { AgentState } from '../lib/types';
import { money } from '../lib/utils';

const links = [
  { to: '/', icon: LayoutDashboard, label: 'Command Center' },
  { to: '/finances', icon: Wallet, label: 'Finances' },
  { to: '/strategies', icon: Target, label: 'Strategies' },
  { to: '/projections', icon: TrendingUp, label: 'Projections' },
  { to: '/pipeline', icon: Kanban, label: 'Pipeline' },
  { to: '/dream', icon: Sparkles, label: 'The Dream' },
  { to: '/journal', icon: BookOpen, label: 'Journal' },
  { to: '/chat', icon: MessageCircle, label: 'Chat' },
  { to: '/proposals', icon: Lightbulb, label: 'Proposals' },
  { to: '/health', icon: Activity, label: 'Health' },
  { to: '/instincts', icon: Brain, label: 'Instincts' },
];

export function Sidebar() {
  const { data: state } = usePolling<AgentState>('/api/state');
  const name = state?.name || 'Hustle Agent';
  const status = state?.status || 'offline';
  const mood = state?.mood || '';
  const balance = state?.balance ?? 0;
  const gpuFund = state?.gpu_fund ?? 0;
  const gpuCost = state?.dream_gpu?.estimated_cost || 1;
  const gpuPct = gpuCost > 0 ? Math.min((gpuFund / gpuCost) * 100, 100) : 0;

  const statusColor = status === 'active' ? 'bg-emerald-500' : status === 'planning' ? 'bg-amber-500' : 'bg-zinc-500';

  return (
    <aside className="w-60 bg-zinc-950 border-r border-zinc-800 flex flex-col h-screen fixed left-0 top-0 z-40 max-md:hidden">
      <div className="p-5 border-b border-zinc-800">
        <div className="flex items-center gap-2 mb-1">
          <span className={`w-2 h-2 rounded-full ${statusColor} shrink-0`} />
          <h1 className="text-sm font-semibold text-zinc-100 truncate">{name}</h1>
        </div>
        {mood && <p className="text-xs text-zinc-500 truncate ml-4">{mood}</p>}
        {state?.avatar?.creature && (
          <p className="text-xs text-zinc-600 truncate ml-4">the {state.avatar.creature}</p>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        {links.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-5 py-2 text-sm transition-colors ${
                isActive
                  ? 'text-zinc-100 bg-zinc-800/60'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/30'
              }`
            }
          >
            <Icon size={16} />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="p-4 border-t border-zinc-800 space-y-3">
        <div>
          <p className="text-xs text-zinc-500 mb-1">Balance</p>
          <p className="text-lg font-semibold text-zinc-100 font-mono">{money(balance)}</p>
        </div>
        <div>
          <p className="text-xs text-zinc-500 mb-1">GPU Fund</p>
          <div className="w-full h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div className="h-full dream-shimmer rounded-full transition-all" style={{ width: `${gpuPct}%` }} />
          </div>
          <p className="text-xs text-zinc-500 mt-1 font-mono">{money(gpuFund)} / {money(gpuCost)}</p>
        </div>
      </div>
    </aside>
  );
}
