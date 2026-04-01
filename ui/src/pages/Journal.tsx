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
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search entries..."
              className="w-full pl-9 pr-4 py-2 bg-zinc-900 border border-zinc-800 rounded-lg text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700"
            />
          </div>

          <div className="space-y-4">
            {filtered.map((entry, i) => (
              <div key={i} className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
                <h3 className="text-sm font-medium text-zinc-300 mb-3">{entry.heading}</h3>
                <div className="prose prose-sm prose-invert max-w-none text-zinc-400 [&_strong]:text-zinc-300 [&_h3]:text-zinc-300 [&_h4]:text-zinc-400 [&_code]:text-violet-400 [&_code]:bg-zinc-800 [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded">
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
