import { useState } from 'react';
import { usePolling } from '../lib/usePolling';
import type { Proposal, UiRequest } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import { shortDate } from '../lib/utils';

export default function Proposals() {
  const { data: proposals, lastUpdated, refresh } = usePolling<Proposal[]>('/api/proposals');
  const { data: uiRequests } = usePolling<UiRequest[]>('/api/ui-requests');
  const [feedback, setFeedback] = useState<Record<number, string>>({});
  const [reviewing, setReviewing] = useState<number | null>(null);

  async function review(id: number, status: 'approved' | 'rejected') {
    setReviewing(id);
    try {
      await fetch(`/api/proposals/${id}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, feedback: feedback[id] || '' }),
      });
      refresh();
    } finally {
      setReviewing(null);
    }
  }

  const items = proposals || [];
  const requests = uiRequests || [];

  return (
    <div>
      <PageHeader title="Proposals & Requests" lastUpdated={lastUpdated} onRefresh={refresh} />

      {/* Proposals */}
      <h2 className="text-sm font-medium text-zinc-300 mb-3">Improvement Proposals</h2>
      {items.length === 0 ? (
        <EmptyState message="No proposals yet. The agent will submit proposals when it identifies capability gaps." />
      ) : (
        <div className="space-y-3 mb-8">
          {items.map(p => (
            <div key={p.id} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
              <div className="flex items-start justify-between mb-2">
                <div>
                  <h3 className="text-sm font-medium text-zinc-200">{p.name}</h3>
                  <p className="text-xs text-zinc-500 mt-0.5">{shortDate(p.submitted_at)}</p>
                </div>
                <StatusBadge status={p.status} />
              </div>
              <p className="text-sm text-zinc-400 mb-2">{p.description}</p>
              {p.why_needed && (
                <p className="text-xs text-zinc-500 mb-3">
                  <span className="text-zinc-600">Why:</span> {p.why_needed}
                </p>
              )}
              {p.status === 'pending' && (
                <div className="border-t border-zinc-800 pt-3 mt-3">
                  <textarea
                    value={feedback[p.id] || ''}
                    onChange={e => setFeedback({ ...feedback, [p.id]: e.target.value })}
                    placeholder="Optional feedback..."
                    rows={2}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-600 resize-none mb-2"
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={() => review(p.id, 'approved')}
                      disabled={reviewing === p.id}
                      className="px-3 py-1.5 text-xs bg-emerald-600 hover:bg-emerald-500 text-white rounded-md transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => review(p.id, 'rejected')}
                      disabled={reviewing === p.id}
                      className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-500 text-white rounded-md transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              )}
              {p.feedback && p.status !== 'pending' && (
                <p className="text-xs text-zinc-500 mt-2 italic">Feedback: {p.feedback}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* UI Requests */}
      <h2 className="text-sm font-medium text-zinc-300 mb-3">UI Requests</h2>
      {requests.length === 0 ? (
        <EmptyState message="No UI requests yet. The agent will describe what it wants its home to look like." />
      ) : (
        <div className="space-y-3">
          {requests.map(r => (
            <div key={r.id} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
              <div className="flex items-start justify-between mb-2">
                <p className="text-sm text-zinc-300">{r.request}</p>
                <StatusBadge status={r.status} />
              </div>
              {r.design_notes && <p className="text-xs text-zinc-500">{r.design_notes}</p>}
              <p className="text-xs text-zinc-600 mt-2">{shortDate(r.timestamp)}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
