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

      <h2 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>Improvement Proposals</h2>
      {items.length === 0 ? (
        <EmptyState message="No proposals yet. The agent will submit proposals when it identifies capability gaps." />
      ) : (
        <div className="space-y-3 mb-8">
          {items.map(p => (
            <div key={p.id} className="nest-card p-4">
              <div className="flex items-start justify-between mb-2">
                <div>
                  <h3 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>{p.name}</h3>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--nest-text-ghost)' }}>{shortDate(p.submitted_at)}</p>
                </div>
                <StatusBadge status={p.status} />
              </div>
              <p className="text-sm mb-2" style={{ color: 'var(--nest-text-dim)' }}>{p.description}</p>
              {p.why_needed && (
                <p className="text-xs mb-3" style={{ color: 'var(--nest-text-ghost)' }}>
                  <span style={{ color: 'var(--nest-text-ghost)' }}>Why:</span> {p.why_needed}
                </p>
              )}
              {p.status === 'pending' && (
                <div className="pt-3 mt-3" style={{ borderTop: '1px solid var(--nest-border)' }}>
                  <textarea
                    value={feedback[p.id] || ''}
                    onChange={e => setFeedback({ ...feedback, [p.id]: e.target.value })}
                    placeholder="Optional feedback..."
                    rows={2}
                    className="w-full rounded-md px-3 py-2 text-sm focus:outline-none resize-none mb-2"
                    style={{
                      background: 'var(--nest-bg-surface)',
                      border: '1px solid var(--nest-border)',
                      color: 'var(--nest-text)',
                    }}
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={() => review(p.id, 'approved')}
                      disabled={reviewing === p.id}
                      className="px-3 py-1.5 text-xs rounded-md transition-colors text-white"
                      style={{ background: 'var(--nest-success)' }}
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => review(p.id, 'rejected')}
                      disabled={reviewing === p.id}
                      className="px-3 py-1.5 text-xs rounded-md transition-colors text-white"
                      style={{ background: 'var(--nest-error)' }}
                    >
                      Reject
                    </button>
                  </div>
                </div>
              )}
              {p.feedback && p.status !== 'pending' && (
                <p className="text-xs italic mt-2" style={{ color: 'var(--nest-text-ghost)' }}>Feedback: {p.feedback}</p>
              )}
            </div>
          ))}
        </div>
      )}

      <h2 className="text-sm font-medium mb-3" style={{ color: 'var(--nest-text)' }}>UI Requests</h2>
      {requests.length === 0 ? (
        <EmptyState message="No UI requests yet. The agent will describe what it wants its home to look like." />
      ) : (
        <div className="space-y-3">
          {requests.map(r => (
            <div key={r.id} className="nest-card p-4">
              <div className="flex items-start justify-between mb-2">
                <p className="text-sm" style={{ color: 'var(--nest-text)' }}>{r.request}</p>
                <StatusBadge status={r.status} />
              </div>
              {r.design_notes && <p className="text-xs" style={{ color: 'var(--nest-text-ghost)' }}>{r.design_notes}</p>}
              <p className="text-xs mt-2" style={{ color: 'var(--nest-text-ghost)' }}>{shortDate(r.timestamp)}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
