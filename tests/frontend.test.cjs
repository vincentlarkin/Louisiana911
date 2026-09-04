const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
function loadFunctions(names, globals = {}) {
  const context = vm.createContext({ console, ...globals });
  for (const name of names) {
    const match = new RegExp(`    (?:async )?function ${name}\\(`).exec(html);
    assert.ok(match, `Missing function ${name}`);
    const end = html.indexOf('\n    }', match.index) + 6;
    vm.runInContext(html.slice(match.index, end), context);
  }
  return context;
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const incident = { id: 1, source: 'caddo', latitude: 32.5, longitude: -93.7,
  description: 'TRAFFIC STOP', street: 'MAIN ST', units: 1, time: '1200' };

test('triangle preference renders one transparent shape without a nested circle', () => {
  const ctx = loadFunctions(['markerIcon'], {
    triangleMarkers: true, getSeverity: () => 'low', getSeverityColor: () => '#3b8bff',
    _markerSymbolSizePixels: () => 16, isApproximateLocation: () => false,
    _incidentSeed: () => 1, L: { divIcon: options => options },
  });
  const triangle = ctx.markerIcon(incident);
  assert.equal((triangle.html.match(/<polygon/g) || []).length, 1);
  assert.doesNotMatch(triangle.html, /<circle/);
  assert.match(triangle.html, /fill-opacity="0.28"/);
  ctx.triangleMarkers = false;
  const circle = ctx.markerIcon(incident);
  assert.equal((circle.html.match(/<circle/g) || []).length, 1);
  assert.doesNotMatch(circle.html, /<polygon/);
});

test('changes to incident details invalidate the rendered list', () => {
  const ctx = loadFunctions(['buildIncidentListSignature']);
  const before = ctx.buildIncidentListSignature([incident]);
  for (const changes of [{ description: 'FIRE' }, { street: 'OAK ST' },
    { latitude: 32.6 }, { agency: 'SFD' }, { geocode_quality: 'street-only' }]) {
    assert.notEqual(ctx.buildIncidentListSignature([{ ...incident, ...changes }]), before);
  }
  assert.equal(ctx.buildIncidentListSignature([{ ...incident }]), before);
});

test('existing markers move, restyle, and refresh open details without duplicating', () => {
  const element = { setAttribute() {} };
  const marker = {
    setLatLng(value) { this.coordinates = value; },
    setIcon(value) { this.icon = value; },
    getElement: () => element,
    getPopup: () => ({}),
    setPopupContent(value) { this.popup = value; },
  };
  const ctx = loadFunctions(['buildIncidentListSignature', 'setMapMarkers'], {
    markers: new Map([[1, marker]]), mapMode: 'live',
    map: { hasLayer: () => true }, hasValidLocation: () => true,
    incidentCoordinatePair: i => [i.latitude, i.longitude],
    markerIcon: i => i.description, markerLabel: i => i.description,
    buildIncidentPopupHtml: i => i.description, syncUnitPulseRing() {},
  });
  marker.renderSignature = ctx.buildIncidentListSignature([incident], 'live');
  const changed = { ...incident, latitude: 32.6, description: 'FIRE' };
  ctx.setMapMarkers([changed]);
  assert.deepEqual(marker.coordinates, [32.6, -93.7]);
  assert.equal(marker.icon, 'FIRE');
  assert.equal(marker.popup, 'FIRE');
  assert.equal(marker.incidentData, changed);
  assert.equal(ctx.markers.size, 1);
});

function liveContext() {
  const requests = [];
  const ctx = loadFunctions(['updateIncidents'], {
    liveRequestId: 0, currentSource: 'caddo', currentIncidents: [], sharedIncidentView: null,
    fetch: () => { const r = deferred(); requests.push(r); return r.promise; },
    sourceQueryParam: () => 'source=caddo', normalizeSource: s => s,
    updateNewOrleansDelayBanner() {}, updateFilterButtonVisibility() {},
    getFilteredIncidents: items => items,
    document: { getElementById: () => ({ classList: { contains: () => false } }) },
    setMapMarkers: items => { ctx.rendered = items; }, renderIncidentList() {},
  });
  return { ctx, requests };
}
const response = data => ({ ok: true, json: async () => data });

test('a slow old feed response cannot replace the newest response', async () => {
  const { ctx, requests } = liveContext();
  const first = ctx.updateIncidents();
  const second = ctx.updateIncidents();
  requests[1].resolve(response([{ ...incident, description: 'NEW' }]));
  await second;
  requests[0].resolve(response([{ ...incident, description: 'OLD' }]));
  await first;
  assert.equal(ctx.rendered[0].description, 'NEW');
});

test('a source switch discards the previous source response', async () => {
  const { ctx, requests } = liveContext();
  const pending = ctx.updateIncidents();
  ctx.currentSource = 'lafayette';
  requests[0].resolve(response([incident]));
  await pending;
  assert.equal(ctx.rendered, undefined);
});

test('routine status polling preserves today’s History page and reloads at midnight', async () => {
  let centralDate = '2026-09-04';
  const selected = [];
  const ctx = loadFunctions(['updateStatus'], {
    currentSource: 'all', isoToday: centralDate, historySelectedDate: centralDate,
    fetch: async () => response({ centralDate, feedRefreshedAt: '12:00', scrapeInterval: 60000 }),
    document: { getElementById: () => ({ classList: { contains: () => false } }) },
    setHistoryDate: (...args) => selected.push(args), renderDatePicker() {},
  });
  await ctx.updateStatus();
  assert.equal(selected.length, 0);
  centralDate = '2026-09-05';
  await ctx.updateStatus();
  assert.equal(selected.length, 1);
  assert.equal(selected[0][0], centralDate);
  assert.equal(selected[0][1].load, true);
});

test('failed History requests clear old incidents and show a retry action', async () => {
  const list = { innerHTML: '' };
  let mapItems;
  const ctx = loadFunctions(['loadHistory'], {
    historyRequestId: 0, currentSource: 'caddo', historySelectedDate: '2026-09-04',
    isoToday: '2026-09-04', historyDayCache: new Map(), historyBatchSize: 100,
    URLSearchParams, console: { error() {} },
    currentHistoryIncidents: [incident], allHistoryIncidentsForDay: [incident],
    document: { getElementById: id => id === 'history-list' ? list :
      { classList: { contains: () => false, add() {} } } },
    updateHistoryPagination() {}, setMapMarkers: items => { mapItems = items; },
    fetchHistoryApi: async () => ({ ok: false, status: 503 }),
  });
  await ctx.loadHistory('2026-09-04');
  assert.equal(ctx.currentHistoryIncidents.length, 0);
  assert.equal(mapItems.length, 0);
  assert.match(list.innerHTML, /Try again/);
  assert.equal(ctx.historyDayCache.size, 0);
});

test('superseded History requests stop fetching additional pages', async () => {
  const request = deferred();
  let calls = 0;
  const ctx = loadFunctions(['loadHistory'], {
    historyRequestId: 0, currentSource: 'caddo', historySelectedDate: '2026-09-04',
    isoToday: '2026-09-04', historyDayCache: new Map(), historyBatchSize: 100,
    URLSearchParams, document: { getElementById: () =>
      ({ classList: { contains: () => false, add() {} } }) },
    updateHistoryPagination() {}, setMapMarkers() {},
    fetchHistoryApi: () => { calls++; return request.promise; },
  });
  const pending = ctx.loadHistory('2026-09-04');
  ctx.historySelectedDate = '2026-09-03';
  request.resolve(response({ incidents: [incident], total: 500 }));
  await pending;
  assert.equal(calls, 1);
  assert.equal(ctx.historyDayCache.size, 0);
});

test('basemap authentication covers CARTO assets without leaking to other hosts', () => {
  const ctx = loadFunctions(['cartoRequest'], { URL, cartoKey: 'test key&value' });
  for (const host of ['basemaps.cartocdn.com', 'tiles.basemaps.cartocdn.com']) {
    const url = new URL(ctx.cartoRequest(`https://${host}/tile.pbf?existing=1`).url);
    assert.equal(url.searchParams.get('key'), 'test key&value');
    assert.equal(url.searchParams.get('existing'), '1');
  }
  for (const url of ['https://example.com/file', 'https://cartocdn.com.example.com/file',
    'https://evilcartocdn.com/file', 'http://basemaps.cartocdn.com/file']) {
    assert.equal(ctx.cartoRequest(url).url, url);
  }
});

test('vector styles share a renderer and unavailable WebGL falls back', () => {
  let creates = 0;
  const styles = [];
  const renderer = { on() {}, getCanvas: () => ({ addEventListener() {} }),
    setStyle(style) { styles.push(style); } };
  const layer = { addTo() {}, getMaplibreMap: () => renderer };
  const ctx = loadFunctions(['showVectorBasemap', 'fallBackToRaster'], {
    cartoKey: 'test-key', vectorFailed: false, vectorLayer: null, vectorMode: null,
    vectorLoadTimer: null, vectorStyles: { dark: 'dark-style', light: 'light-style' },
    cartoAttribution: 'credits', cartoRequest: url => ({ url }),
    L: { maplibreGL() { creates++; return layer; } },
    map: { hasLayer: () => true, removeLayer() {} },
    setTimeout: () => 1, clearTimeout() {}, queueMicrotask: fn => fn(),
    basemapMode: 'dark', setBasemapMode() {},
  });
  assert.equal(ctx.showVectorBasemap('dark'), true);
  assert.equal(ctx.showVectorBasemap('dark'), true);
  assert.equal(ctx.showVectorBasemap('light'), true);
  assert.equal(creates, 1);
  assert.deepEqual(styles, ['light-style']);
  ctx.fallBackToRaster();
  assert.equal(ctx.vectorLayer, null);
  assert.equal(ctx.showVectorBasemap('dark'), false);
  ctx.vectorFailed = false;
  ctx.L.maplibreGL = () => { throw new Error('WebGL unavailable'); };
  assert.equal(ctx.showVectorBasemap('dark'), false);
  assert.equal(ctx.vectorFailed, true);
});

test('street labels appear at incident focus zoom with readable dark and light colors', () => {
  const zooms = {}, colors = {};
  const renderer = {
    getLayer: () => true,
    setLayerZoomRange(id, min) { zooms[id] = min; },
    setLayoutProperty() {},
    setPaintProperty(id, name, color) { colors[id] = color; },
  };
  const ctx = loadFunctions(['improveStreetLabels']);
  ctx.improveStreetLabels(renderer, 'dark');
  assert.equal(zooms.roadname_minor, 14);
  assert.equal(colors.roadname_minor, '#bdc5cf');
  ctx.improveStreetLabels(renderer, 'light');
  assert.equal(colors.roadname_minor, '#525c69');
});

test('failed WebGL initialization still removes the adapter and activates raster', () => {
  let removed = false, fallback = false;
  const layer = { getMaplibreMap: () => null, getContainer: () => ({ remove() { removed = true; } }) };
  const ctx = loadFunctions(['fallBackToRaster'], {
    vectorFailed: false, vectorLayer: layer, vectorLoadTimer: null,
    clearTimeout() {}, queueMicrotask: fn => fn(), basemapMode: 'dark',
    map: { hasLayer: () => true, removeLayer(value) { value.onRemove(); } },
    setBasemapMode() { fallback = true; },
  });
  ctx.fallBackToRaster();
  assert.equal(removed, true);
  assert.equal(fallback, true);
  assert.equal(ctx.vectorLayer, null);
});

test('new visitors get triangles and saved shape/view choices survive initialization', () => {
  const start = html.indexOf('    const preferences =');
  const end = html.indexOf('    // Initialize map', start);
  const initialize = (values, mobile = false, blocked = false) => {
    const context = vm.createContext({
      localStorage: { getItem: key => { if (blocked) throw new Error('blocked'); return values[key] ?? null; },
        setItem: (key, value) => { if (blocked) throw new Error('blocked'); values[key] = value; } },
      window: { matchMedia: () => ({ matches: mobile }) },
    });
    vm.runInContext(html.slice(start, end), context);
    return { context, state: vm.runInContext('({triangleMarkers, viewMode})', context) };
  };
  assert.equal(initialize({}).state.triangleMarkers, true);
  assert.equal(initialize({}, true).state.viewMode, 'list');
  assert.equal(initialize({ louisiana911ViewMode: 'map' }, true).state.viewMode, 'map');
  assert.equal(initialize({ louisiana911TriangleMarkers: 'false' }).state.triangleMarkers, false);
  const values = {};
  const first = initialize(values);
  vm.runInContext("preferences.set('louisiana911TriangleMarkers', 'false'); preferences.set('louisiana911BasemapMode', 'light'); preferences.set('showAllHistoryOnMap', 'false')", first.context);
  const second = initialize(values);
  assert.equal(second.state.triangleMarkers, false);
  assert.equal(vm.runInContext("preferences.get('louisiana911BasemapMode')", second.context), 'light');
  assert.equal(vm.runInContext("preferences.get('showAllHistoryOnMap')", second.context), 'false');
  const restricted = initialize({}, true, true);
  assert.equal(restricted.state.triangleMarkers, true);
  assert.doesNotThrow(() => vm.runInContext("preferences.set('test','value')", restricted.context));
});

test('five-unit pulse follows marker shape and stops for history or lower unit counts', () => {
  const layers = new Set();
  const ctx = loadFunctions(['shouldPulseForUnits', 'unitPulseIcon', 'createUnitPulseRing', 'removeUnitPulseRing', 'syncUnitPulseRing'], {
    UNIT_PULSE_THRESHOLD: 5, triangleMarkers: true,
    incidentCoordinatePair: i => i.latitude == null ? null : [i.latitude, i.longitude],
    _markerSymbolSizePixels: () => 19, getSeverity: () => 'high', getSeverityColor: () => '#ff3b3b',
    L: { divIcon: options => options, marker: (coords, options) => ({
      coordinates: coords, options, setIcon(icon) { this.icon = icon; },
      setLatLng(coords) { this.coordinates = coords; }, addTo() { layers.add(this); },
    }) },
    map: { hasLayer: layer => layers.has(layer), removeLayer: layer => layers.delete(layer) },
  });
  const marker = {}, call = { ...incident, units: '5' };
  ctx.syncUnitPulseRing(marker, call, 'live');
  const pulse = marker._unitPulseRing;
  assert.equal(layers.size, 1);
  assert.match(pulse.icon.html, /<polygon/);
  assert.doesNotMatch(pulse.icon.html, /<circle/);
  ctx.triangleMarkers = false;
  ctx.syncUnitPulseRing(marker, { ...call, latitude: 32.6 }, 'live');
  assert.equal(marker._unitPulseRing, pulse);
  assert.equal(layers.size, 1);
  assert.match(pulse.icon.html, /<circle/);
  assert.equal(pulse.coordinates[0], 32.6);
  ctx.syncUnitPulseRing(marker, { ...call, units: 4 }, 'live');
  assert.equal(layers.size, 0);
  assert.equal(marker._unitPulseRing, null);
  ctx.syncUnitPulseRing(marker, call, 'history');
  assert.equal(layers.size, 0);
  ctx.syncUnitPulseRing(marker, call, 'live');
  assert.equal(layers.size, 1);
  ctx.syncUnitPulseRing(marker, { ...call, latitude: null }, 'live');
  assert.equal(layers.size, 0);
});
