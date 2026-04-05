import { useState, useRef, useEffect } from 'react';
import { usePolling } from '../lib/usePolling';
import type { Conversation } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { Send } from 'lucide-react';
import { relativeTime } from '../lib/utils';

export default function Chat() {
  const { data: conversations, lastUpdated, refresh } = usePolling<Conversation[]>('/api/conversations');
  const { data: inbox } = usePolling<{ timestamp: string; content: string }[]>('/api/inbox');
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const messages = conversations || [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  async function sendMessage() {
    if (!message.trim() || sending) return;
    setSending(true);
    try {
      await fetch('/api/inbox', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message.trim() }),
      });
      setMessage('');
      refresh();
    } finally {
      setSending(false);
    }
  }

  const pendingInbox = inbox?.filter(m => m.content) || [];

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)]">
      <PageHeader title="Chat" lastUpdated={lastUpdated} onRefresh={refresh} />

      <div className="flex-1 overflow-y-auto space-y-3 mb-4">
        {messages.length === 0 && pendingInbox.length === 0 ? (
          <EmptyState message="No messages yet. Say hello to your agent." />
        ) : (
          <>
            {messages.map((m, i) => (
              <div
                key={i}
                className={`flex ${m.from === 'tyler' ? 'justify-end' : 'justify-start'}`}
              >
                <div className={`max-w-[75%] rounded-xl px-4 py-2.5 ${
                  m.from === 'tyler'
                    ? ''
                    : ''
                }`}
                  style={{
                    background: m.from === 'tyler'
                      ? 'linear-gradient(135deg, rgba(124, 58, 237, 0.5), rgba(0, 153, 255, 0.3))'
                      : 'var(--nest-bg-card)',
                    border: m.from === 'tyler' ? 'none' : '1px solid var(--nest-border)',
                    color: 'var(--nest-text)',
                  }}>
                  <p className="text-sm whitespace-pre-wrap">{m.message}</p>
                  <p className="text-[10px] mt-1" style={{ color: m.from === 'tyler' ? 'rgba(255,255,255,0.4)' : 'var(--nest-text-ghost)' }}>
                    {m.from === 'tyler' ? 'Tyler' : 'Agent'} &middot; {relativeTime(m.timestamp)}
                  </p>
                </div>
              </div>
            ))}

            {pendingInbox.length > 0 && (
              <div className="pt-3 mt-3" style={{ borderTop: '1px solid var(--nest-border)' }}>
                <p className="text-xs mb-2" style={{ color: 'var(--nest-text-ghost)' }}>Pending (agent hasn't read yet):</p>
                {pendingInbox.map((m, i) => (
                  <div key={`inbox-${i}`} className="flex justify-end mb-2">
                    <div className="max-w-[75%] rounded-xl px-4 py-2.5"
                      style={{
                        background: 'rgba(124, 58, 237, 0.15)',
                        border: '1px solid rgba(124, 58, 237, 0.2)',
                        color: 'var(--nest-text-muted)',
                      }}>
                      <p className="text-sm whitespace-pre-wrap">{m.content}</p>
                      <p className="text-[10px] mt-1" style={{ color: 'rgba(124, 58, 237, 0.4)' }}>Queued &middot; {relativeTime(m.timestamp)}</p>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div ref={bottomRef} />
          </>
        )}
      </div>

      {/* Input */}
      <div className="pt-3" style={{ borderTop: '1px solid var(--nest-border)' }}>
        <div className="flex gap-2">
          <textarea
            value={message}
            onChange={e => setMessage(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder="Message your agent..."
            rows={2}
            className="flex-1 rounded-lg px-4 py-2.5 text-sm focus:outline-none resize-none"
            style={{
              background: 'var(--nest-bg-card)',
              border: '1px solid var(--nest-border)',
              color: 'var(--nest-text)',
            }}
          />
          <button
            onClick={sendMessage}
            disabled={!message.trim() || sending}
            className="px-4 rounded-lg transition-all flex items-center disabled:opacity-30"
            style={{
              background: 'linear-gradient(135deg, var(--nest-purple), var(--nest-blue))',
              color: 'white',
            }}
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
