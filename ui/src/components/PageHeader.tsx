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
        <h1 className="text-xl font-semibold" style={{ color: 'var(--nest-text-bright)' }}>
          {title}
        </h1>
        {subtitle && (
          <p className="text-sm mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>
            {subtitle}
          </p>
        )}
      </div>
      <div className="flex items-center gap-3">
        {lastUpdated && (
          <span className="text-[11px] font-mono" style={{ color: 'var(--nest-text-ghost)' }}>
            {lastUpdated.toLocaleTimeString()}
          </span>
        )}
        {onRefresh && (
          <button
            onClick={onRefresh}
            className="p-1.5 rounded-md transition-colors hover:bg-white/[0.05]"
            style={{ color: 'var(--nest-text-dim)' }}
          >
            <RefreshCw size={14} />
          </button>
        )}
      </div>
    </div>
  );
}
