import { useState, useMemo } from 'react';
import Markdown from 'react-markdown';
import { usePolling } from '../lib/usePolling';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { Search } from 'lucide-react';

interface JournalEntry {
  heading: string;
  body: string;
}

function parseJournal(content: string): JournalEntry[] {
  if (!content.trim()) return [];
  const parts = content.split(/(?=^## )/m).filter(p => p.trim());
  return parts
    .map(part => {
      const lines = part.split('\n');
      const heading = (lines[0] || '').replace(/^##\s*/, '').trim();
      const body = lines.slice(1).join('\n').trim();
      return { heading, body };
    })
    .filter(e => e.body.length > 0)
    .reverse();
}

export default function Journal() {
  const { data, lastUpdated, refresh } = usePolling<{ content: string }>('/api/journal');
  const [search, setSearch] = useState('');

  const entries = useMemo(() => parseJournal(data?.content || ''), [data]);

  const filtered = useMemo(() => {
    if (!search) return entries;
    const q = search.toLowerCase();
    return entries.filter(e => e.heading.toLowerCase().includes(q) || e.body.toLowerCase().includes(q));
  }, [entries, search]);

  return (
    <div>
      <PageHeader title="Journal" lastUpdated={lastUpdated} onRefresh={refresh} />

      {entries.length === 0 ? (
        <EmptyState message="The journal is empty. Entries appear after the agent completes a cycle." />
      ) : (
        <>
          <div className="relative mb-6">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--nest-text-ghost)' }} />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search entries..."
              className="w-full pl-9 pr-4 py-2 rounded-lg text-sm focus:outline-none"
              style={{
                background: 'var(--nest-bg-card)',
                border: '1px solid var(--nest-border)',
                color: 'var(--nest-text)',
              }}
            />
          </div>

          <div className="space-y-4">
            {filtered.map((entry, i) => (
              <div key={i} className="nest-card p-5 animate-fade-up" style={{ animationDelay: `${i * 40}ms` }}>
                <h3 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>{entry.heading}</h3>
                <div className="prose prose-sm prose-invert max-w-none prose-nest [&_code]:text-[var(--nest-blue)] [&_code]:bg-[var(--nest-bg-surface)] [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded">
                  <Markdown>{entry.body}</Markdown>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
