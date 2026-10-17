// @vitest-environment jsdom
/**
 * Claude Fable 5.1 on the chat screen (#1577): the picker offers it with an honest price line and
 * thinking locked on; an answer Anthropic's refusal fallback wrote says so in one quiet line, live
 * and when saved. Synthetic data only.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatMessage } from '@/lib/chat-api';
import { applyChatEvent, fallbackWords, fromSaved, localAnswer, type ViewMessage } from '@/lib/chat-messages';
import { chooseModel, chooseThinking, modelBlurb, resolveChoice } from '@/lib/chat-settings';
import { modelName } from '@/lib/usage';
import { FABLE, OPUS, SAVED_ANSWER, SONNET } from './fixtures/chat';

const MODELS = [OPUS, SONNET, FABLE];
const SWITCH = { from_model: 'claude-fable-5-1', to_model: 'claude-opus-4-8' };

describe('Claude Fable 5.1 in the picker', () => {
  it('says its price honestly and keeps Opus the default', () => {
    expect(modelBlurb(FABLE)).toBe('Hardest tasks · about 2.5× Opus cost');
    expect(modelBlurb(OPUS)).toBe('Most capable for complex work');
    expect(resolveChoice(MODELS, null)?.model).toBe('claude-opus-5-5');
  });

  it('always thinks: picking it turns thinking on and it cannot be turned off', () => {
    const offOnSonnet = { model: 'claude-sonnet-5-5', effort: 'high' as const, thinking: false };
    const onFable = chooseModel(MODELS, offOnSonnet, 'claude-fable-5-1');
    expect(onFable).toEqual({ model: 'claude-fable-5-1', effort: 'high', thinking: true });
    expect(chooseThinking(MODELS, onFable, false).thinking).toBe(true);
  });

  it('a remembered Fable choice falls back to the default where the server does not offer it', () => {
    expect(resolveChoice([OPUS, SONNET], { model: 'claude-fable-5-1', effort: 'max', thinking: true })?.model).toBe('claude-opus-5-5');
  });

  it('the Usage page names Fable and the fallback models', () => {
    expect(modelName('anthropic.claude-fable-5-1')).toBe('Claude Fable 5.1');
    expect(modelName('anthropic.claude-opus-4-8')).toBe('Claude Opus 4.8');
    expect(modelName('anthropic.claude-opus-5')).toBe('Claude Opus 5');
    expect(modelName('anthropic.claude-opus-5-5')).toBe('Claude Opus 5.5');
  });
});

describe('an answer the refusal fallback wrote', () => {
  it('the fallback event sets the line and message_end keeps the stored one', () => {
    let answer: ViewMessage = localAnswer('a-1', { model: 'claude-fable-5-1', effort: 'high', thinking: true });
    answer = applyChatEvent(answer, { event: 'fallback', data: { index: 2, fallback: SWITCH } });
    expect(answer.fallback).toEqual(SWITCH);
    const stored: ChatMessage = { ...SAVED_ANSWER, model: 'claude-fable-5-1', blocks: [{ type: 'text', text: 'Two items differ.' }, { type: 'fallback', fallback: SWITCH }] };
    answer = applyChatEvent(answer, { event: 'message_end', data: { message: stored } });
    expect(answer.fallback).toEqual(SWITCH);
    expect(fromSaved({ ...SAVED_ANSWER }).fallback).toBeNull();
  });

  it('shows one quiet line naming both models', () => {
    expect(fallbackWords(SWITCH, MODELS)).toBe('Claude Fable 5.1 declined this request, so Claude Opus 4.8 answered it instead.');
    const saved = fromSaved({ ...SAVED_ANSWER, model: 'claude-fable-5-1', blocks: [{ type: 'text', text: 'Two items differ.' }, { type: 'fallback', fallback: SWITCH }] });
    render(<AssistantMessage message={saved} chatId="c-1" models={MODELS} messageCapUsd="5.00" />);
    const line = document.querySelector('[data-slot="chat-fallback"]');
    expect(line?.textContent).toBe('Claude Fable 5.1 declined this request, so Claude Opus 4.8 answered it instead.');
    expect(line?.className).toContain('text-muted-foreground');
    expect(screen.getByText(/Claude Fable 5\.1 · High effort/)).toBeTruthy();
  });
});
