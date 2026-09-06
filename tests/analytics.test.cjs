const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../public/analytics.js'), 'utf8');

function tracker(url = 'https://louisiana911.com/', height = 1200) {
  const handlers = {};
  const scripts = [];
  const selected = { dataset: { source: 'all' } };
  const window = {
    innerHeight: 800, innerWidth: 1000, scrollY: 0,
    addEventListener: (name, fn) => { handlers[name] = fn; },
  };
  const document = {
    title: 'Incident title', referrer: 'https://example.com/page?private=value#secret',
    visibilityState: 'visible', documentElement: { scrollHeight: height },
    head: { appendChild: script => scripts.push(script) },
    createElement: () => ({ dataset: {} }),
    querySelector: selector => selector === '.source-tab.active' ? selected : null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  };
  const ctx = vm.createContext({
    window, document, location: new URL(url), URL, URLSearchParams,
    performance: { now: () => 0, getEntriesByType: () => [], getEntriesByName: () => [] },
    setInterval() {}, setTimeout() {}, requestAnimationFrame: fn => fn(),
  });
  vm.runInContext(source, ctx);
  return { window, handlers, scripts, selected, ctx,
    events: () => window.dataLayer.filter(row => row[0] === 'event') };
}

test('GA initializes once, removes incident IDs and query strings, and identifies shared pages', () => {
  const app = tracker('https://louisiana911.com/incident/abcdef0123456789?address=private#secret');
  const config = app.window.dataLayer.find(row => row[0] === 'config')[2];
  assert.equal(config.page_type, 'shared_incident');
  assert.equal(config.page_location, 'https://louisiana911.com/incident/shared');
  assert.equal(config.page_referrer, 'https://example.com/page');
  assert.equal(config.page_title, 'Shared incident | Louisiana911');
  assert.equal(app.scripts.length, 1);
  vm.runInContext(source, app.ctx);
  assert.equal(app.scripts.length, 1);
});

test('local previews queue events without loading Google and use the current selected source', () => {
  const app = tracker('http://127.0.0.1:5051/');
  assert.equal(app.scripts.length, 0);
  app.selected.dataset.source = 'lakecharles';
  app.window.louisiana911Analytics.track('history_view', { result_count: 8 });
  assert.equal(app.events()[0][2].source_view, 'lakecharles');
  app.window.louisiana911Analytics.track('invalid event');
  assert.equal(app.events().length, 1);
});

test('URL redaction preserves standard campaign and paid-click attribution', () => {
  const app = tracker('https://louisiana911.com/?utm_source=newsletter&utm_medium=email&gclid=sample&address=private');
  const config = app.window.dataLayer.find(row => row[0] === 'config')[2];
  assert.equal(config.page_location, 'https://louisiana911.com/?utm_source=newsletter&utm_medium=email&gclid=sample');
});

test('non-scrollable pages do not manufacture scroll milestones; real scroll fires each once', () => {
  const fixed = tracker(undefined, 800);
  fixed.handlers.scroll();
  assert.equal(fixed.events().length, 0);
  const app = tracker(undefined, 1600);
  app.window.scrollY = 400;
  app.handlers.scroll(); app.handlers.scroll();
  assert.deepEqual(Array.from(app.events(), row => row[2].percent_scrolled), [10, 25, 50]);
});

test('feature click events use bounded source, view and filter values', () => {
  const events = [];
  const start = source.indexOf('  function trackFeatureClick(');
  const end = source.indexOf('\n  }', start) + 4;
  const ctx = vm.createContext({ track: (name, params) => events.push({name, params}),
    cleanToken: (value, max) => String(value).slice(0, max) });
  vm.runInContext(source.slice(start, end), ctx);
  const click = (dataset, disabled = false) => ctx.trackFeatureClick({
    disabled, getAttribute: () => null, closest: () => ({ dataset }),
  });
  click({source: 'lakecharles'}); click({viewMode: 'list'}); click({tab: 'history'});
  click({urgency: 'high'}); click({source: 'private text'}); click({source: 'caddo'}, true);
  assert.deepEqual(events.map(event => event.name), ['source_select', 'view_mode_select', 'incident_tab_select', 'incident_filter']);
  assert.equal(events[0].params.source_view, 'lakecharles');
});
