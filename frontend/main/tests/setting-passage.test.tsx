import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { SettingPassage } from '../src/components/measure/SettingPassage.js';
import { loadPassageImage } from '../src/components/measure/passageImage.js';
import { settingMissingASource, sourceToSend } from '../src/components/measure/settingSources.js';

const pointer = { proposal_id: 'fixture-passage', page_index: 2, document_kind: 'shop', has_crop: true };
const noop = () => undefined;
const render = (image: Parameters<typeof SettingPassage>[0]['image']) => renderToStaticMarkup(
  <SettingPassage pointer={pointer} name="overhang" image={image} onRetry={noop} onImageError={noop} />,
);
assert.match(render(null), /role="status"/);
assert.doesNotMatch(render(null), /<img|Retry passage/);
const failed = render({ error: true });
assert.match(failed, /role="alert"/);
assert.match(failed, /Retry passage image/);
assert.match(failed, /page 3 of the vendor&#x27;s file/);
assert.doesNotMatch(failed, /<img/);
assert.match(render({ url: 'blob:fixture' }), /src="blob:fixture"/);
assert.doesNotMatch(render({ url: 'blob:fixture' }), /Retry passage|<input/);

const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
let resolve!: (blob: Blob) => void;
let creates = 0;
const revoked: string[] = [];
const urls = { createObjectURL: () => `blob:${++creates}`, revokeObjectURL: (url: string) => { revoked.push(url); } };
const shown: string[] = [];
let failures = 0;
const ready = (url: string) => { shown.push(url); };
const fail = () => { failures++; };
// A superseded source can never flash the old passage or leak its object URL.
const disposeOld = loadPassageImage(() => new Promise<Blob>((r) => { resolve = r; }), ready, fail, urls);
await settle();
disposeOld();
resolve(new Blob(['old']));
await settle();
assert.deepEqual(shown, []);
assert.equal(creates, 0);

loadPassageImage(() => Promise.reject(new Error('offline')), ready, fail, urls);
await settle();
assert.equal(failures, 1);
assert.deepEqual(shown, []);
// Explicit retry succeeds, then cleanup revokes the image exactly once.
const disposeRetry = loadPassageImage(() => Promise.resolve(new Blob(['retry'])), ready, fail, urls);
await settle();
assert.deepEqual(shown, ['blob:1']);
disposeRetry(); disposeRetry();
assert.deepEqual(revoked, ['blob:1']);
let reject!: (reason: Error) => void;
const disposeFailed = loadPassageImage(() => new Promise<Blob>((_r, failRequest) => { reject = failRequest; }), ready, fail, urls);
await settle(); disposeFailed(); reject(new Error('late failure')); await settle();
assert.equal(failures, 1);

// Current backend source choices survive integration: no guessed multi-source answer.
const setting = { name: 'overhang', sources: [{ value: 'G.C / Client', guidance: 'fixture' }, { value: 'Company standard', guidance: 'fixture' }] };
assert.equal(sourceToSend(setting, undefined), null);
assert.equal(sourceToSend(setting, 'invented'), null);
assert.equal(settingMissingASource([setting], { overhang: '1"' }, {}), 'overhang');
assert.equal(settingMissingASource([setting], { overhang: '1"' }, { overhang: 'G.C / Client' }), null);
assert.equal(settingMissingASource([setting], { overhang: '' }, {}), null);
console.log('setting-passage: render states, recovery, late responses, URL cleanup and source guards passed');
