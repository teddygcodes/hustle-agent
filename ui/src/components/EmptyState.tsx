import { Inbox } from 'lucide-react';

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-zinc-600">
      <Inbox size={40} className="mb-3 opacity-40" />
      <p className="text-sm text-center max-w-xs">{message}</p>
    </div>
  );
}
