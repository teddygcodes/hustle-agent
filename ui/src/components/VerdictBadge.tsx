import clsx from 'clsx';

const styles: Record<string, string> = {
  strong_buy: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  lean_yes: 'bg-green-500/15 text-green-400 border-green-500/30',
  coin_flip: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  lean_no: 'bg-orange-500/15 text-orange-400 border-orange-500/30',
  hard_pass: 'bg-red-500/15 text-red-400 border-red-500/30',
};

export function VerdictBadge({ verdict }: { verdict: string }) {
  const style = styles[verdict] || styles.coin_flip;
  return (
    <span className={clsx('text-xs font-semibold px-2.5 py-1 rounded-md border', style)}>
      {verdict.replace(/_/g, ' ').toUpperCase()}
    </span>
  );
}
