export function money(n: number): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
}

export function pct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
}

export function relativeTime(ts: string): string {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function shortDate(ts: string): string {
  if (!ts) return '';
  return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

export function getRiskPosture(balance: number): { label: string; color: string } {
  if (balance >= 90) return { label: 'Aggressive', color: 'text-emerald-400' };
  if (balance >= 70) return { label: 'Normal', color: 'text-amber-400' };
  return { label: 'Preservation', color: 'text-red-400' };
}

export function getSurvivalEstimate(balance: number, avgCycleCost: number): string {
  if (avgCycleCost <= 0) return 'N/A';
  const cycles = Math.floor(balance / avgCycleCost);
  return `~${cycles} cycles`;
}
