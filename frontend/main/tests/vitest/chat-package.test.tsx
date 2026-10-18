// @vitest-environment jsdom
/**
 * #1622: the send-ready package card. It comes right after the cloud drawing card in a full-check
 * answer, leads with the review result decided by code (AWI's words), lists what the zip holds and
 * offers one download. A check that did not finish says plainly it is not for sending. Synthetic
 * data only.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PackageCard } from '@/components/chat/chat-full-check';
import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFile, ChatReviewAction } from '@/lib/chat-api';
import { fullCheckFiles } from '@/lib/chat-full-check';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

const CLOUD: ChatFile = { id: 'f-c', name: 'Set - GV cloud drawing.pdf', size: 2_400_000, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-c' };
const REPORT: ChatFile = { id: 'f-r', name: 'Set - AI check report.pdf', size: 90_000, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-r' };
const PACKAGE: ChatFile = { id: 'f-z', name: 'Set - GV review package.zip', size: 3_100_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };

const RESUBMIT: ChatReviewAction = {
  action: 'revise_resubmit',
  rule: 'S4',
  words: 'Revise and resubmit',
  reason: '1 of 2 corrections changes fabrication.',
  corrections: 2,
  fabrication: 1,
  note: null,
  next_step: 'Please revise the drawings, answer each comment in writing, cloud every change, and resubmit them for review before fabrication.',
  required_by: 'Vendor: revise and resubmit',
  disclaimer: 'This check compares the dimensions…',
};

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-18T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Two values are wrong.' }], files: [], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', mode: 'full_check', ...change,
  };
}

describe('the send-ready package', () => {
  it('finds the newest zip among the files, apart from the drawing and the report', () => {
    const older = { ...PACKAGE, id: 'f-z0' };
    const picked = fullCheckFiles([CLOUD, older, REPORT, PACKAGE]);
    expect(picked.pkg).toBe(PACKAGE);
    expect(picked.cloud).toBe(CLOUD);
    expect(picked.report).toBe(REPORT);
    expect(picked.rest).toEqual([]);
  });

  it('leads with the review result, says who acts, lists the contents and downloads the zip', () => {
    render(<PackageCard pkg={PACKAGE} chatId="c-1" action={RESUBMIT} hasNotCompared />);
    const card = screen.getByRole('region', { name: 'Send-ready package' });
    const result = card.querySelector<HTMLElement>('[data-slot="chat-review-action"]')!;
    expect(result.dataset.action).toBe('revise_resubmit');
    expect(within(result).getByText('Revise and resubmit')).toBeTruthy();
    expect(result.textContent).toContain('1 of 2 corrections changes fabrication.');
    expect(result.textContent).toContain('Action required by: Vendor: revise and resubmit');
    const download = within(card).getByRole('link', { name: `Download package: ${PACKAGE.name}` });
    expect(download.getAttribute('href')).toBe(PACKAGE.url);
    expect(download.getAttribute('download')).toBe(PACKAGE.name);
    expect(download.textContent).toBe('Download package (.zip)');
    const contents = [...card.querySelectorAll('[data-slot="chat-package-contents"] li')].map((li) => li.textContent);
    expect(contents).toEqual([
      'GV cloud drawing · PDF',
      'Comment letter · 2 comments, numbered like the clouds',
      'Issue register · XLSX · one row per item',
      'Not-compared list · PDF and XLSX',
      'Check report · PDF',
    ]);
  });

  it('says an unfinished check is not for sending', () => {
    const unfinished: ChatReviewAction = { ...RESUBMIT, action: 'not_for_sending', rule: 'S0', words: 'Check incomplete: not for sending', reason: 'The check did not finish: it stopped at its fixed price.', required_by: 'GV: finish the check before sending' };
    render(<PackageCard pkg={PACKAGE} chatId="c-1" action={unfinished} hasReport={false} />);
    const card = screen.getByRole('region', { name: 'Send-ready package' });
    expect(within(card).getByRole('alert').textContent).toContain('Check incomplete: not for sending');
    expect(card.textContent).toContain('do not send it yet');
    expect(card.textContent).not.toContain('Check report');
  });

  it('comes right after the cloud drawing card in a full-check answer', () => {
    const findings = [finding('w1', { item: 'Sink base width', status: 'different' })];
    render(
      <AssistantMessage
        message={answer({ findings, files: [CLOUD, REPORT, PACKAGE], review_action: RESUBMIT })}
        chatId="c-1"
        models={[OPUS]}
        fullCheck
      />,
    );
    const node = screen.getByRole('article', { name: 'Answer' });
    const cloud = node.querySelector('[data-slot="chat-cloud-drawing"]')!;
    const pkg = node.querySelector('[data-slot="chat-package"]')!;
    expect(cloud.nextElementSibling).toBe(pkg);
    // The zip is not shown again as a plain file card.
    expect(node.querySelectorAll(`a[href="${PACKAGE.url}"]`)).toHaveLength(1);
  });
});
