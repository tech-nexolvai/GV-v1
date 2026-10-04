/**
 * Light or dark, the reviewer's choice remembered on this device.
 *
 * `index.html` sets `data-theme` before the first paint (so dark mode never flashes white); this
 * hook reads what it set and lets the sidebar switch it. Until the reviewer chooses, the system
 * setting decides. Storage can be unavailable — a private window — in which case the choice simply
 * lasts for this visit.
 */

import { useCallback, useState } from 'react';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'gv-theme';

function currentTheme(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(currentTheme);

  const toggle = useCallback(() => {
    const next: Theme = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Storage blocked: the switch still applies for this visit.
    }
    setTheme(next);
  }, []);

  return [theme, toggle];
}
