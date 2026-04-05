import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Wallet, Target, TrendingUp, Kanban,
  Sparkles, BookOpen, MessageCircle, Lightbulb, Activity, Brain, FileText, BarChart2
} from 'lucide-react';

const links = [
  { to: '/', icon: LayoutDashboard, label: 'Nest' },
  { to: '/finances', icon: Wallet, label: 'Money' },
  { to: '/strategies', icon: Target, label: 'Strats' },
  { to: '/chat', icon: MessageCircle, label: 'Chat' },
  { to: '/dream', icon: Sparkles, label: 'Dream' },
];

const moreLinks = [
  { to: '/projections', icon: TrendingUp, label: 'Proj' },
  { to: '/pipeline', icon: Kanban, label: 'Pipe' },
  { to: '/journal', icon: BookOpen, label: 'Log' },
  { to: '/proposals', icon: Lightbulb, label: 'Props' },
  { to: '/health', icon: Activity, label: 'Health' },
  { to: '/instincts', icon: Brain, label: 'Brain' },
  { to: '/reports', icon: FileText, label: 'Reports' },
  { to: '/trades', icon: BarChart2, label: 'Trades' },
];

export function MobileNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 z-50 md:hidden border-t"
      style={{ background: 'var(--nest-bg)', borderColor: 'var(--nest-border)' }}>
      <div className="flex overflow-x-auto">
        {[...links, ...moreLinks].map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className="flex flex-col items-center gap-0.5 px-3 py-2 text-[10px] min-w-[56px]"
            style={({ isActive }) => ({
              color: isActive ? 'var(--nest-blue)' : 'var(--nest-text-ghost)',
            })}
          >
            <Icon size={16} />
            {label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
