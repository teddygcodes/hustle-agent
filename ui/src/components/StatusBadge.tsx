import clsx from 'clsx';

const colors: Record<string, string> = {
  active: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  planning: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  paused: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  exploring: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  retired: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
  closed_won: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  closed_lost: 'bg-red-500/15 text-red-400 border-red-500/30',
  recurring: 'bg-violet-500/15 text-violet-400 border-violet-500/30',
  triggered: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  expired: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
  pending: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  approved: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  rejected: 'bg-red-500/15 text-red-400 border-red-500/30',
  completed: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
};

export function StatusBadge({ status }: { status: string }) {
  const style = colors[status] || colors.retired;
  return (
    <span className={clsx('text-[10px] font-medium px-2 py-0.5 rounded-full border', style)}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}
