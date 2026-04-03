import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, Wallet, Target, TrendingUp, Kanban,
  Sparkles, BookOpen, MessageCircle, Lightbulb, Activity, Brain, FileText
} from 'lucide-react';

const links = [
  { to: '/', icon: LayoutDashboard, label: 'Home' },
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
];

export function MobileNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 bg-zinc-950 border-t border-zinc-800 z-50 md:hidden">
      <div className="flex overflow-x-auto">
        {[...links, ...moreLinks].map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex flex-col items-center gap-0.5 px-3 py-2 text-[10px] min-w-[56px] ${
                isActive ? 'text-violet-400' : 'text-zinc-500'
              }`
            }
          >
            <Icon size={16} />
            {label}
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
