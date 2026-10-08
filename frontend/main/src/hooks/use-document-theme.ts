import { useSyncExternalStore } from 'react';

/**
 * The theme on screen, read from `data-theme` on <html> (set by index.html and src/app/theme.ts),
 * and kept current when the reviewer switches it anywhere in the app. For new components that
 * must know the theme in JavaScript (the toast container); CSS should use the `dark:` variant.
 */
export function useDocumentTheme(): 'light' | 'dark' {
  return useSyncExternalStore(subscribe, read, () => 'light');
}

function read(): 'light' | 'dark' {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}
