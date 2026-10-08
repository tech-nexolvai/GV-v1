// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { UiKitPage } from '@/pages/ui-kit/UiKitPage';

describe('UI kit', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.setAttribute('data-theme', 'light');
  });

  it('opens a result in a side sheet and closes it with Escape', async () => {
    const user = userEvent.setup();
    render(<UiKitPage />);

    expect(screen.queryByRole('dialog')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Open result' }));

    const sheet = await screen.findByRole('dialog');
    expect(sheet.textContent).toContain('Countertop row 2.1');
    expect(sheet.textContent).toContain('Needs correction');

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('switches the whole app between light and dark and remembers the choice', async () => {
    const user = userEvent.setup();
    render(<UiKitPage />);

    await user.click(screen.getByRole('radio', { name: 'Dark theme' }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(localStorage.getItem('gv-theme')).toBe('dark');

    await user.click(screen.getByRole('radio', { name: 'Light theme' }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('asks before signing off, and keeping on reviewing changes nothing', async () => {
    const user = userEvent.setup();
    render(<UiKitPage />);

    // The first "Sign off" is the plain button specimen; the dialog trigger is in Overlays.
    const triggers = screen.getAllByRole('button', { name: 'Sign off' });
    await user.click(triggers[triggers.length - 1]);
    expect(await screen.findByRole('dialog', { name: 'Sign off this review?' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Keep reviewing' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('shows every outcome with its agreed word, in both themes', () => {
    render(<UiKitPage />);
    const outcomes = document.querySelectorAll('#outcomes [data-slot="outcome-badge"]');
    expect(outcomes).toHaveLength(10); // five outcomes × light and dark panels
    for (const badge of outcomes) {
      // Never colour alone: every badge carries the outcome glyph.
      expect(badge.querySelector('[data-outcome-icon]')).not.toBeNull();
    }
    expect(screen.getAllByText('Waiting on a value').length).toBeGreaterThan(0);
  });
});
