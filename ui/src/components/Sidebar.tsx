import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Wallet, Target, TrendingUp, Kanban,
  Sparkles, BookOpen, MessageCircle, Lightbulb, Activity, Brain, FileText
} from 'lucide-react';
import { usePolling } from '../lib/usePolling';
import type { AgentState } from '../lib/types';
import { money } from '../lib/utils';

const links = [
  { to: '/', icon: LayoutDashboard, label: 'Nest' },
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
  { to: '/reports', icon: FileText, label: 'Reports' },
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

  const statusColor = status === 'active'
    ? 'bg-[var(--nest-success)]'
    : status === 'planning'
    ? 'bg-[var(--nest-warning)]'
    : 'bg-[var(--nest-text-ghost)]';

  const statusGlow = status === 'active'
    ? 'shadow-[0_0_6px_rgba(16,185,129,0.5)]'
    : '';

  return (
    <aside className="w-60 flex flex-col h-screen fixed left-0 top-0 z-40 max-md:hidden overflow-hidden"
      style={{ background: 'var(--nest-bg)' }}>
      {/* Accent edge line */}
      <div className="absolute left-0 top-0 bottom-0 w-[2px] sidebar-edge opacity-60" />

      {/* Agent identity header */}
      <div className="p-5 border-b relative" style={{ borderColor: 'var(--nest-border)' }}>
        <div className="flex items-center gap-2.5 mb-1.5">
          {/* Magpie avatar */}
          <div className="avatar-float relative">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center text-base"
              style={{
                background: 'linear-gradient(135deg, var(--nest-blue-dim), var(--nest-purple-dim))',
                boxShadow: '0 0 12px var(--nest-blue-glow)',
              }}>
              🐦‍⬛
            </div>
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${statusColor} ${statusGlow} shrink-0`} />
              <h1 className="text-sm font-semibold truncate" style={{ color: 'var(--nest-text-bright)' }}>
                {name}
              </h1>
            </div>
            {mood && (
              <p className="text-[11px] truncate mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>
                {mood}
              </p>
            )}
          </div>
        </div>
        {state?.avatar?.creature && (
          <p className="text-[10px] ml-[42px] -mt-0.5" style={{ color: 'var(--nest-text-ghost)' }}>
            the {state.avatar.creature}
          </p>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-2">
        {links.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-5 py-2 text-[13px] transition-all duration-200 relative ${
                isActive
                  ? 'font-medium'
                  : 'hover:bg-white/[0.03]'
              }`
            }
            style={({ isActive }) => ({
              color: isActive ? 'var(--nest-blue)' : 'var(--nest-text-dim)',
              background: isActive ? 'rgba(0, 153, 255, 0.06)' : undefined,
            })}
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[2px] h-5 rounded-r"
                    style={{ background: 'var(--nest-blue)' }} />
                )}
                <Icon size={15} style={{ opacity: isActive ? 1 : 0.6 }} />
                {label}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      {/* Footer: Balance + GPU Fund */}
      <div className="p-4 space-y-3 border-t" style={{ borderColor: 'var(--nest-border)' }}>
        <div>
          <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>
            Balance
          </p>
          <p className="text-lg font-semibold font-mono" style={{ color: 'var(--nest-text-bright)' }}>
            {money(balance)}
          </p>
        </div>
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>
              GPU Fund
            </p>
            <p className="text-[10px] font-mono" style={{ color: 'var(--nest-text-dim)' }}>
              {gpuPct.toFixed(1)}%
            </p>
          </div>
          <div className="w-full h-2 rounded-full overflow-hidden"
            style={{ background: 'var(--nest-bg-surface)' }}>
            <div className="h-full dream-shimmer rounded-full transition-all duration-1000"
              style={{ width: `${gpuPct}%` }} />
          </div>
          <p className="text-[10px] mt-1.5 font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
            {money(gpuFund)} / {money(gpuCost)}
          </p>
        </div>
      </div>
    </aside>
  );
}
