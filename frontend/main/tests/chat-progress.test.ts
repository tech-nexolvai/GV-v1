import assert from 'node:assert/strict';
import { chatProgress } from '../src/components/chat/chatProgress.js';
assert.equal(chatProgress().label, 'Waiting for the review response');
assert.equal(chatProgress({ stage: 'unknown', model_id: 'model' }).modelId, null);
assert.equal(chatProgress({ stage: 'narrating', model_id: 'configured-model' }).modelId, 'configured-model');
assert.doesNotMatch(chatProgress().label, /Running|Reading|Composing/);
console.log('chat progress truthfulness tests passed');
