import { useState, useEffect, useCallback, useRef } from 'react';

export function usePolling<T>(url: string, interval = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${res.status}`);
      const json = await res.json();
      if (mounted.current) {
        setData(json);
        setError(null);
        setLastUpdated(new Date());
        setLoading(false);
      }
    } catch (e) {
      if (mounted.current) {
        setError(e instanceof Error ? e.message : 'fetch failed');
        setLoading(false);
      }
    }
  }, [url]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    const id = setInterval(refresh, interval);
    return () => { mounted.current = false; clearInterval(id); };
  }, [refresh, interval]);

  return { data, loading, error, refresh, lastUpdated };
}
