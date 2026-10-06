import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { PageDrawingView } from '../src/components/measure/PageDrawingView.js';

// Before the picture arrives the pane says so, and names the page it is showing.
const loading = renderToStaticMarkup(<PageDrawingView pageNumber={2} />);
assert.match(loading, /aria-label="Drawing, page 2"/);
assert.match(loading, /Loading the drawing/);
const shown = renderToStaticMarkup(<PageDrawingView pageNumber={2} url="blob:x" />);
assert.match(shown, /<img[^>]*src="blob:x"/);
assert.doesNotMatch(shown, /Loading/);
const failed = renderToStaticMarkup(<PageDrawingView pageNumber={2} error="HTTP 404" />);
assert.match(failed, /role="alert"/);
assert.match(failed, /HTTP 404/);
console.log('the drawing pane names its page and says when it is still loading');
