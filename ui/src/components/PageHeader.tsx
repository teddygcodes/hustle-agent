import { RefreshCw } from 'lucide-react';

interface Props {
  title: string;
  subtitle?: string;
  lastUpdated?: Date | null;
  onRefresh?: () => void;
}

export function PageHeader({ title, subtitle, lastUpdated, onRefresh }: Props) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">{title}</h1>
        {subtitle && <p className="text-sm text-zinc-500 mt-0.5">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-3">
        {lastUpdated && (
          <span className="text-xs text-zinc-600 font-mono">
            {lastUpdated.toLocaleTimeString()}
          </span>
        )}
        {onRefresh && (
          <button
            onClick={onRefresh}
            className="p-1.5 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 transition-colors"
          >
            <RefreshCw size={14} />
          </button>
        )}
      </div>
    </div>
  );
}
