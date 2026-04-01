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
                <div className={`max-w-[75%] rounded-lg px-4 py-2.5 ${
                  m.from === 'tyler'
                    ? 'bg-violet-600/80 text-zinc-100'
                    : 'bg-zinc-800 text-zinc-300'
                }`}>
                  <p className="text-sm whitespace-pre-wrap">{m.message}</p>
                  <p className={`text-[10px] mt-1 ${m.from === 'tyler' ? 'text-violet-300/60' : 'text-zinc-600'}`}>
                    {m.from === 'tyler' ? 'Tyler' : 'Agent'} &middot; {relativeTime(m.timestamp)}
                  </p>
                </div>
              </div>
            ))}

            {/* Pending inbox messages */}
            {pendingInbox.length > 0 && (
              <div className="border-t border-zinc-800 pt-3 mt-3">
                <p className="text-xs text-zinc-600 mb-2">Pending (agent hasn't read yet):</p>
                {pendingInbox.map((m, i) => (
                  <div key={`inbox-${i}`} className="flex justify-end mb-2">
                    <div className="max-w-[75%] rounded-lg px-4 py-2.5 bg-violet-600/40 text-zinc-300 border border-violet-500/20">
                      <p className="text-sm whitespace-pre-wrap">{m.content}</p>
                      <p className="text-[10px] text-violet-300/40 mt-1">Queued &middot; {relativeTime(m.timestamp)}</p>
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
      <div className="border-t border-zinc-800 pt-3">
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
            className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-2.5 text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700 resize-none"
          />
          <button
            onClick={sendMessage}
            disabled={!message.trim() || sending}
            className="px-4 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-800 disabled:text-zinc-600 text-white rounded-lg transition-colors flex items-center"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
