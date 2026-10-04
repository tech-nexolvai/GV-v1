import assert from 'node:assert/strict';
import { describeUploadFailure, IncompletePackageUpload } from '../src/api/uploadState.js';

const saved = describeUploadFailure(new IncompletePackageUpload('package-1', new Error('upload refused')));
assert.deepEqual(saved, { detail: 'upload refused', savedPackageId: 'package-1' });

const unknown = describeUploadFailure(new Error('connection lost'));
assert.deepEqual(unknown, { detail: 'connection lost', savedPackageId: null });

console.log('upload failure preserves saved package identity');
