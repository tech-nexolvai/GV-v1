import { useSyncExternalStore } from 'react';

/**
 * #1744: whether the browser says it is online, kept up to date (the `online` / `offline` events).
 * Assumed online where there is no `navigator` (a test, the server).
 */
export function useOnline(): boolean {
  return useSyncExternalStore(subscribe, read, () => true);
}

function subscribe(change: () => void): () => void {
  window.addEventListener('online', change);
  window.addEventListener('offline', change);
  return () => {
    window.removeEventListener('online', change);
    window.removeEventListener('offline', change);
  };
}

function read(): boolean {
  return typeof navigator === 'undefined' || navigator.onLine !== false;
}
