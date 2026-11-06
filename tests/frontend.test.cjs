const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
function loadFunctions(names, globals = {}) {
  const context = vm.createContext({
    console, window: {}, AbortController, clearTimeout,
    // Pending request timeouts must not keep the test process alive.
    setTimeout: (fn, ms) => { const timer = setTimeout(fn, ms); timer.unref?.(); return timer; },
    ...globals,
  });
  // Shared source sets the loaded functions read; defined once at script top level.
  for (const name of ['DELAYED_CLOSED_SOURCES', 'SOURCE_GROUPS', 'SOURCE_ALIASES', 'POLL_REQUEST_TIMEOUT_MS']) {
    const line = new RegExp(`    const ${name} = .*;`).exec(html);
    assert.ok(line, `Missing ${name}`);
    vm.runInContext(line[0], context);
  }
  // Every polling request goes through this wrapper; it calls the context's fetch.
  for (const name of ['fetchWithTimeout', ...names]) loadInto(context, name);
  return context;
}
function loadInto(context, name) {
  const match = new RegExp(`    (?:async )?function ${name}\\(`).exec(html);
  assert.ok(match, `Missing function ${name}`);
  const end = html.indexOf('\n    }', match.index) + 6;
  vm.runInContext(html.slice(match.index, end), context);
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const incident = { id: 1, source: 'caddo', latitude: 32.5, longitude: -93.7,
  description: 'TRAFFIC STOP', street: 'MAIN ST', units: 1, time: '1200' };

test('parish landing pages open the map despite saved list or mobile defaults and select the route source', () => {
  const start = html.indexOf('    const storedViewMode =');
  const end = html.indexOf('    // Initialize map', start);
  const sourceStart = html.indexOf('    const sourceFromUrl =');
  const sourceEnd = html.indexOf('    let currentLafayetteUnit', sourceStart);
  for (const source of ['caddo', 'batonrouge', 'lafayette', 'neworleans', null]) {
    for (const mobile of [true, false]) {
      for (const saved of ['list', 'map', null]) {
        const ctx = vm.createContext({
          preferences: { get: () => saved }, URLSearchParams,
          document: { getElementById: () => source ? { dataset: { source } } : null },
          window: { matchMedia: () => ({ matches: mobile }), location: { search: '?source=lakecharles' } },
        });
        vm.runInContext(html.slice(start, end) + html.slice(sourceStart, sourceEnd), ctx);
        assert.equal(vm.runInContext('viewMode', ctx), source ? 'map' : saved || (mobile ? 'list' : 'map'));
        // Lake Charles and Sulphur links open the combined Calcasieu tab.
        assert.equal(vm.runInContext('currentSource', ctx), source || 'calcasieu');
      }
    }
  }
});

test('coverage card dismisses and reopens with keyboard focus and accessible state', () => {
  const intro = {};
  const reopen = { focus() { this.focused = true; }, setAttribute(name, value) { this[name] = value; } };
  const close = { focus() { this.focused = true; } };
  const ctx = loadFunctions(['setCoverageIntroOpen'], {
    coverageIntro: intro, document: { getElementById: id => id === 'coverage-reopen' ? reopen : close },
  });
  ctx.setCoverageIntroOpen(false);
  assert.equal(intro.hidden, true);
  assert.equal(reopen.hidden, false);
  assert.equal(reopen['aria-expanded'], 'false');
  assert.equal(reopen.focused, true);
  ctx.setCoverageIntroOpen(true);
  assert.equal(intro.hidden, false);
  assert.equal(reopen.hidden, true);
  assert.equal(reopen['aria-expanded'], 'true');
  assert.equal(close.focused, true);
});

test('sharing reports successful native/copy outcomes without counting cancellation or failure', async () => {
  for (const outcome of ['native', 'cancel', 'copy', 'failed']) {
    const events = [];
    const ctx = loadFunctions(['shareIncident'], {
      window: { louisiana911Analytics: { track: (name, params) => events.push({name, params}) } },
      navigator: outcome === 'native' ? { share: async () => {} }
        : outcome === 'cancel' ? { share: async () => { throw { name: 'AbortError' }; } } : {},
      incidentShareUrl: () => 'https://louisiana911.com/incident/private-id',
      incidentActivityState: () => ({ label: 'ACTIVE' }),
      incidentLocationText: () => 'private location',
      normalizeSource: () => 'caddo', showIncidentShareNotice() {},
      copyTextToClipboard: async () => { if (outcome === 'failed') throw new Error('denied'); },
      console: { error() {} },
    });
    const result = await ctx.shareIncident(incident);
    const success = ['native', 'copy'].includes(outcome);
    assert.equal(result, success);
    assert.equal(events.length, success ? 1 : 0);
    if (success) {
      assert.equal(events[0].name, 'share');
      assert.equal(events[0].params.method, outcome === 'native' ? 'native' : 'copy_link');
      assert.equal(JSON.stringify(events).includes('private'), false);
    }
  }
});

test('hang-up and open-line calls are blue; suicide attempts are solid red', () => {
  const ctx = loadFunctions(['_normalizeDesc', '_includesAny', '_includesWholeTerm', 'getSeverity', 'getSeverityColor']);
  const start = html.indexOf('    const FORCE_HIGH_SEVERITY_TERMS =');
  const end = html.indexOf('    function getSeverity(', start);
  vm.runInContext(html.slice(start, end), ctx);
  for (const description of ['HANG UP CALL', 'HANG-UP CALL', '911 HANGUP', 'OPEN LINE TO 911', 'OPEN 911 LINE', '911 OPEN LINE']) {
    const severity = ctx.getSeverity({ description, agency: 'LCPD' });
    assert.equal(severity, 'low', description);
    assert.equal(ctx.getSeverityColor(severity), '#3b8bff');
  }
  for (const agency of ['LCPD', 'EMS']) {
    for (const description of ['SUICIDE ATTEMPT', 'ATTEMPTED SUICIDE']) {
      const severity = ctx.getSeverity({ description, agency });
      assert.equal(severity, 'high', `${agency}: ${description}`);
      assert.equal(ctx.getSeverityColor(severity), '#ff3b3b');
    }
  }
  assert.equal(ctx.getSeverity({ description: 'HANG UP CALL - SHOTS FIRED', agency: 'LCPD' }), 'high');
});

test('delayed calls sort by original date across midnight and display Louisiana dates', () => {
  const ctx = loadFunctions(['_incidentTimeValue', 'incidentTimeLabel'], { formatTime: value => value });
  const older = { source: 'lakecharles', time: '2355', first_seen: '2026-09-03T04:55:00Z' };
  const newer = { source: 'lakecharles', time: '0010', first_seen: '2026-09-03T05:10:00Z' };
  assert.ok(ctx._incidentTimeValue(newer) > ctx._incidentTimeValue(older));
  assert.match(ctx.incidentTimeLabel(older), /Sep-2/);
  assert.match(ctx.incidentTimeLabel(newer), /Sep-3/);
});

test('Lake Charles has its own source and cannot be labeled active or breaking', () => {
  const ctx = loadFunctions(['normalizeSource', 'sourceLabel', 'incidentActivityState', 'formatUnitCount'], { UNIT_PULSE_THRESHOLD: 5 });
  assert.equal(ctx.normalizeSource('lakecharles'), 'lakecharles');
  assert.match(ctx.sourceLabel('lakecharles'), /24h delay/);
  const state = ctx.incidentActivityState({ source: 'lakecharles', is_active: 1, units: 10 });
  assert.equal(state.active, false);
  assert.equal(state.veryActive, false);
  assert.match(state.label, /CLOSED CALL/);
  assert.equal(ctx.formatUnitCount(0), 'Units not listed');
});

test('Calcasieu tab shows Lake Charles and Sulphur together, in one group of the All view', () => {
  const ctx = loadFunctions(['normalizeSource', 'sourceLabel', 'sourceMembers', 'matchesSourceFilter', 'groupIncidentsBySource']);
  const lake = { source: 'lakecharles' }, sulphur = { source: 'sulphur' }, caddo = { source: 'caddo' };
  assert.equal(ctx.matchesSourceFilter(lake, 'calcasieu'), true);
  assert.equal(ctx.matchesSourceFilter(sulphur, 'calcasieu'), true);
  assert.equal(ctx.matchesSourceFilter(caddo, 'calcasieu'), false);
  assert.equal(ctx.matchesSourceFilter(sulphur, 'lakecharles'), false);
  assert.equal(ctx.matchesSourceFilter(caddo, 'caddo'), true);
  assert.equal(ctx.matchesSourceFilter(caddo, 'all'), true);
  assert.match(ctx.sourceLabel('calcasieu'), /^Calcasieu .*24h delay/);
  const grouped = ctx.groupIncidentsBySource([lake, sulphur, caddo]);
  assert.equal(grouped.get('calcasieu').length, 2);
  assert.equal(grouped.has('lakecharles'), false);
  assert.equal(lake.source, 'lakecharles');
});

test('Sulphur is its own delayed source and cannot be labeled active or breaking', () => {
  const ctx = loadFunctions(['normalizeSource', 'sourceLabel', 'incidentActivityState', 'formatTime', 'incidentTimeLabel'], { UNIT_PULSE_THRESHOLD: 5 });
  assert.equal(ctx.normalizeSource('sulphur'), 'sulphur');
  assert.match(ctx.sourceLabel('sulphur'), /^Sulphur .*24h delay/);
  const state = ctx.incidentActivityState({ source: 'sulphur', is_active: 1, units: 10 });
  assert.equal(state.active, false);
  assert.equal(state.veryActive, false);
  assert.match(state.label, /CLOSED CALL/);
  assert.equal(ctx.incidentTimeLabel({ source: 'sulphur', time: '2345', first_seen: '2026-10-31T04:45:43Z' }), 'Oct-30 23:45');
});

test('Caddo keeps the reported clock and anchored date across midnight in every view', () => {
  const ctx = loadFunctions(['formatTime', '_incidentTimeValue', 'incidentTimeLabel']);
  const older = { source: 'caddo', time: '2350', reported_at: '2026-09-05T04:50:00+00:00',
    first_seen: '2026-09-05T05:16:47.404955+00:00', last_seen: '2026-09-06T07:00:00Z' };
  const newer = { source: 'caddo', time: '0010', reported_at: '2026-09-05T05:10:00+00:00' };
  assert.equal(ctx.incidentTimeLabel(older), 'Sep-4 23:50');
  assert.equal(ctx.incidentTimeLabel(newer), 'Sep-5 00:10');
  assert.ok(ctx._incidentTimeValue(newer) > ctx._incidentTimeValue(older));
  assert.equal(ctx.incidentTimeLabel({ source: 'caddo', time: '1234' }), '12:34');
  assert.equal(ctx.incidentTimeLabel({ source: 'batonrouge', time: '1234' }), '12:34');
  const otherSource = { source: 'batonrouge', time: '0020', first_seen: '2026-09-05T05:20:00Z' };
  assert.ok(ctx._incidentTimeValue(otherSource) > ctx._incidentTimeValue(newer));
});

test('every feed displays its reported date and keeps the source clock unchanged', () => {
  const ctx = loadFunctions(['formatTime', 'incidentTimeLabel']);
  for (const source of ['caddo', 'batonrouge', 'lafayette', 'neworleans', 'lakecharles', 'sulphur']) {
    const call = { source, time: '2350', reported_at: '2026-09-02T04:50:00Z',
      first_seen: '2026-09-05T05:10:00Z', last_seen: '2026-09-06T12:00:00Z' };
    assert.equal(ctx.incidentTimeLabel(call), 'Sep-1 23:50', source);
  }
});

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
  const ctx = loadFunctions(['sourceMembers', 'matchesSourceFilter', 'updateIncidents'], {
    liveRequestId: 0, currentSource: 'caddo', currentIncidents: [], sharedIncidentView: null,
    fetch: () => { const r = deferred(); requests.push(r); return r.promise; },
    sourceQueryParam: () => 'source=caddo', normalizeSource: s => s,
    updateSourceDelayBanner() {}, updateFilterButtonVisibility() {},
    getFilteredIncidents: items => items,
    document: { getElementById: () => ({ classList: { contains: () => false } }) },
    setMapMarkers: items => { ctx.rendered = items; }, renderIncidentList() {},
    mobileStatusState: {}, renderMobileStatus() {},
  });
  return { ctx, requests };
}
const response = data => ({ ok: true, json: async () => data });

test('a removed Caddo call leaves the live map even when the response is empty', async () => {
  const { ctx, requests } = liveContext();
  const first = ctx.updateIncidents();
  requests[0].resolve(response([incident]));
  await first;
  assert.equal(ctx.rendered.length, 1);
  const next = ctx.updateIncidents();
  requests[1].resolve(response([]));
  await next;
  assert.equal(ctx.rendered.length, 0);
  assert.equal(ctx.currentIncidents.length, 0);
});

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

test('feed telemetry reports displayed outcomes and ignores superseded failures', async () => {
  const { ctx, requests } = liveContext();
  const outcomes = [];
  ctx.window.louisiana911Analytics = { feedResult: result => outcomes.push(result) };
  ctx.hasValidLocation = i => i.latitude != null && i.longitude != null;
  ctx.console = { error() {} };
  const old = ctx.updateIncidents();
  const latest = ctx.updateIncidents();
  requests[1].resolve(response([incident, { ...incident, id: 2, latitude: null }]));
  await latest;
  requests[0].resolve({ ok: true, json: async () => { throw new Error('old bad data'); } });
  await old;
  assert.equal(outcomes.length, 1);
  assert.equal(outcomes[0].outcome, 'success');
  assert.equal(outcomes[0].count, 2);
  assert.equal(outcomes[0].mappableCount, 1);
  const empty = ctx.updateIncidents();
  requests[2].resolve(response([]));
  await empty;
  assert.equal(outcomes[1].count, 0);
  const failed = ctx.updateIncidents();
  requests[3].resolve({ ok: true, json: async () => { throw new Error('bad response'); } });
  await failed;
  assert.equal(outcomes[2].outcome, 'error');
});

test('browsing filters releases a shared incident so later polls show the full feed', async () => {
  const shared = { ...incident, id: 9, share_id: 'abc', is_active: 0 };
  const other = { ...incident, id: 2 };
  const requests = [];
  const panels = { 'live-panel': false, 'history-panel': true };
  const element = id => ({ classList: { contains: cls => cls === 'hidden' && Boolean(panels[id]) } });
  const ctx = loadFunctions(['sourceMembers', 'matchesSourceFilter', 'updateIncidents',
    'leaveSharedIncidentView', 'refreshFilteredView'], {
    liveRequestId: 0, currentSource: 'caddo', currentIncidents: [], sharedIncidentView: shared,
    mapMode: 'history', lastLiveListSignature: 'live|[]',
    fetch: () => { const r = deferred(); requests.push(r); return r.promise; },
    sourceQueryParam: () => 'source=caddo', normalizeSource: s => s,
    updateSourceDelayBanner() {}, updateFilterButtonVisibility() {},
    getFilteredIncidents: items => items, getIncidentShareId: i => i.share_id || String(i.id),
    document: { getElementById: element },
    setMapMarkers() {}, mobileStatusState: {}, renderMobileStatus() {},
    renderSharedIncidentView() { ctx.sharedRenders = (ctx.sharedRenders || 0) + 1; },
    renderIncidentList: items => { ctx.listed = items; },
  });
  const poll = async data => {
    const pending = ctx.updateIncidents();
    requests.at(-1).resolve(response(data));
    await pending;
  };
  await poll([other]);
  assert.equal(ctx.sharedRenders, 1);
  assert.equal(ctx.listed, undefined);

  ctx.refreshFilteredView();
  assert.deepEqual(ctx.listed, [other]);
  assert.equal(vm.runInContext('sharedIncidentView', ctx), null);
  // The shared card replaced the list without updating its signature, and an
  // inactive shared incident left the map in History mode.
  assert.equal(vm.runInContext('lastLiveListSignature', ctx), '');
  assert.equal(vm.runInContext('mapMode', ctx), 'live');

  ctx.listed = undefined;
  await poll([other]);
  assert.deepEqual(ctx.listed, [other]);
  assert.equal(ctx.sharedRenders, 1);
});

test('source, sort and tab changes also release a shared incident', () => {
  for (const marker of ['    function applySourceFilter(', '    function setSort(',
    "    document.querySelectorAll('.tab').forEach(tab => {"]) {
    const start = html.indexOf(marker);
    assert.ok(start >= 0, `Missing ${marker}`);
    const body = html.slice(start, html.indexOf('\n    }', start));
    assert.match(body, /leaveSharedIncidentView\(\)/, `${marker.trim()} keeps the shared incident pinned`);
  }
});

test('a source switch discards the previous source response', async () => {
  const { ctx, requests } = liveContext();
  const pending = ctx.updateIncidents();
  ctx.currentSource = 'lafayette';
  requests[0].resolve(response([incident]));
  await pending;
  assert.equal(ctx.rendered, undefined);
});

test('a failed location watch is cleared so retrying never runs two watchers', () => {
  const watches = new Map();
  let nextId = 0;
  const geolocation = {
    getCurrentPosition() {},
    watchPosition(onPosition, onError) { nextId += 1; watches.set(nextId, onError); return nextId; },
    clearWatch(id) { watches.delete(id); },
  };
  const waiterErrors = [];
  const ctx = loadFunctions(['startLocationTracking'], {
    navigator: { geolocation }, geolocationWatchId: null, followUserLocation: true,
    hasPrimedHighAccuracy: true, locationTrackingStartedAt: 0, bestAccuracyMeters: 0,
    bestLocationLatLng: null, waitingForBetterFixLogged: false, Number, Date,
    logLocationEvent() {}, setLocationButtonState: (state) => { ctx.buttonState = state; },
    userLocationWaiters: [{ reject: error => waiterErrors.push(error) }],
    updateUserLocation() {},
  });
  ctx.startLocationTracking();
  assert.equal(watches.size, 1);
  // A timeout does not stop a browser watch on its own.
  watches.get(1)({ code: 3, message: 'timeout' });
  assert.equal(watches.size, 0);
  assert.equal(vm.runInContext('geolocationWatchId', ctx), null);
  assert.equal(vm.runInContext('followUserLocation', ctx), false);
  assert.equal(ctx.buttonState, 'error');
  assert.equal(waiterErrors.length, 1);
  ctx.startLocationTracking();
  assert.equal(watches.size, 1);
  assert.equal(vm.runInContext('geolocationWatchId', ctx), 2);
});

test('follow mode survives its own recentering but stops when the visitor moves the map', () => {
  const ctx = loadFunctions(['flyToUserLocation', 'stopFollowingOnUserMove'], {
    followUserLocation: true, recenteringOnUser: false, Math,
  });
  // Leaflet's flyTo fires zoomstart synchronously, before any animation frame.
  ctx.map = { getZoom: () => 12, flyTo: (latLng, zoom) => { ctx.flown = [latLng, zoom]; ctx.stopFollowingOnUserMove(); } };
  ctx.flyToUserLocation([32.5, -93.7]);
  assert.deepEqual(ctx.flown, [[32.5, -93.7], 16]);
  assert.equal(vm.runInContext('followUserLocation', ctx), true);
  assert.equal(vm.runInContext('recenteringOnUser', ctx), false);
  ctx.stopFollowingOnUserMove();
  assert.equal(vm.runInContext('followUserLocation', ctx), false);
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

test('Caddo and combined header use an unambiguous Central update time at night', async () => {
  const lastUpdate = {};
  const ctx = loadFunctions(['updateStatus'], {
    currentSource: 'caddo', isoToday: '2026-09-05', historySelectedDate: '2026-09-05',
    fetch: async () => response({ centralDate: '2026-09-05', centralTzAbbr: 'CDT',
      feedRefreshedAt: 'September 05 09:08', feedRefreshedBySource: { caddo: 'September 05 09:08' },
      lastUpdateDisplay: '21:08:15 CDT', lastUpdateTooltip: 'September 5, 2026 21:08 CDT' }),
    document: { getElementById: () => lastUpdate },
  });
  for (const source of ['caddo', 'all']) {
    ctx.currentSource = source;
    await ctx.updateStatus();
    assert.equal(lastUpdate.textContent, '21:08:15 CDT');
  }
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
    vectorLoadTimer: null, vectorProvider: null, oldMapStyle: false,
    BASEMAP_PROVIDERS: {
      colorful: { styles: { dark: 'dark-style', light: 'light-style' }, attribution: 'credits' },
      carto: { styles: { dark: 'carto-dark', light: 'carto-light' }, attribution: 'carto credits' },
    },
    cartoRequest: url => ({ url }),
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

test('colorful maps are the default and old map style switches providers', () => {
  const created = [];
  const removed = [];
  const makeLayer = (options) => ({ options, addTo() {},
    getMaplibreMap: () => ({ on() {}, getCanvas: () => ({ addEventListener() {} }), setStyle() {} }) });
  const ctx = loadFunctions(['showVectorBasemap'], {
    cartoKey: '', vectorFailed: false, vectorLayer: null, vectorMode: null, vectorLoadTimer: null,
    vectorProvider: null, oldMapStyle: false,
    BASEMAP_PROVIDERS: {
      colorful: { styles: { dark: 'colorful-dark', light: 'colorful' }, attribution: 'VersaTiles' },
      carto: { styles: { dark: 'carto-dark', light: 'carto-light' }, attribution: 'CARTO' },
    },
    cartoRequest: url => ({ url }),
    L: { maplibreGL(options) { const layer = makeLayer(options); created.push(layer); return layer; } },
    map: { hasLayer: () => true, removeLayer(layer) { removed.push(layer); } },
    setTimeout: () => 1, clearTimeout() {}, fallBackToRaster() {},
  });
  // Colorful needs no key.
  assert.equal(ctx.showVectorBasemap('light'), true);
  assert.equal(created[0].options.style, 'colorful');
  assert.equal(ctx.vectorProvider, 'colorful');
  // Old map style without a CARTO key can't load; it falls back to raster.
  ctx.oldMapStyle = true;
  assert.equal(ctx.showVectorBasemap('light'), false);
  ctx.cartoKey = 'test-key';
  assert.equal(ctx.showVectorBasemap('dark'), true);
  assert.equal(removed[0], created[0]);
  assert.equal(created[1].options.style, 'carto-dark');
  assert.equal(created[1].options.attributionControl.customAttribution, 'CARTO');
  assert.equal(ctx.vectorProvider, 'carto');
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
      document: { getElementById: () => null },
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

test('five-unit pulse follows marker shape and preserves historical peaks', () => {
  const layers = new Set();
  const ctx = loadFunctions(['isMajorEvent', 'shouldPulseForUnits', 'unitPulseIcon', 'createUnitPulseRing', 'removeUnitPulseRing', 'syncUnitPulseRing'], {
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
  ctx.syncUnitPulseRing(marker, { ...call, is_active: 0, units: 1, peak_units: 6 }, 'history');
  assert.equal(layers.size, 1);
  ctx.syncUnitPulseRing(marker, { ...call, units: 1, peak_units: 6 }, 'live');
  assert.equal(layers.size, 0);
  ctx.syncUnitPulseRing(marker, call, 'live');
  assert.equal(layers.size, 1);
  ctx.syncUnitPulseRing(marker, { ...call, latitude: null }, 'live');
  assert.equal(layers.size, 0);
});

test('major history filter composes with severity, paginates matches and leaves live unchanged', () => {
  const ctx = loadFunctions(['isMajorEvent', 'matchesHistoryMajorFilter', 'matchesFilters',
    'getFilteredIncidents', 'getHistoryFilteredTotal', 'getHistoryPageData', 'incidentActivityState',
    'incidentUnitLabel', 'formatUnitCount'], {
    UNIT_PULSE_THRESHOLD: 5, mapMode: 'history', historyMajorEventsOnly: true,
    currentFilter: 'all', currentUrgencyFilter: 'all', historyHasAllIncidents: true,
    historyPage: 9, historyPageSize: 2, currentSort: 'recent',
    matchesSourceFilter: i => i.source === 'caddo', matchesActiveUnitFilters: () => true,
    matchesUrgencyFilter: (i, severity) => severity === 'all' || severity === i.severity,
    sortIncidents: items => [...items].sort((a, b) => b.id - a.id),
  });
  const items = [
    { id: 1, source: 'caddo', units: 1, peak_units: 6, is_active: 0, severity: 'low' },
    { id: 2, source: 'caddo', units: '5', is_active: 0, severity: 'high' },
    { id: 3, source: 'caddo', units: 8, is_active: 0, severity: 'high' },
    { id: 4, source: 'caddo', units: 4, severity: 'high' },
    { id: 5, source: 'lafayette', units: 10, severity: 'high' },
  ];
  ctx.currentHistoryIncidents = items;
  assert.equal(ctx.getHistoryFilteredTotal(), 3);
  assert.deepEqual(Array.from(ctx.getHistoryPageData(items).pageData, i => i.id), [1]);
  assert.equal(ctx.historyPage, 2);
  ctx.currentUrgencyFilter = 'high';
  assert.deepEqual(Array.from(ctx.getHistoryPageData(items).pageData, i => i.id), [3, 2]);
  assert.equal(ctx.historyPage, 1);
  const historical = ctx.incidentActivityState(items[0], 'history');
  assert.equal(historical.active, false);
  assert.equal(historical.veryActive, false);
  assert.match(ctx.incidentUnitLabel(items[0]), /1 unit · peak 6/);
  ctx.mapMode = 'live';
  assert.equal(ctx.getFilteredIncidents(items).length, 3);
  ctx.mapMode = 'history';
  ctx.historyMajorEventsOnly = false;
  assert.equal(ctx.getFilteredIncidents(items).length, 3);
  for (const units of [undefined, null, '', 'unknown', 0, 4, Infinity]) {
    assert.equal(ctx.isMajorEvent({ units }, 'history'), false);
  }
});

test('an empty major-event result preserves selected severity and agency filters', () => {
  const ctx = loadFunctions(['updateFilterButtonVisibility'], {
    mapMode: 'history', historyMajorEventsOnly: true,
    currentFilter: 'police', currentBatonRougeAgency: 'law', currentLafayetteUnit: 'fire',
    currentUrgencyFilter: 'high', caddoFilterButtons: [], batonRougeFilterButtons: [],
    lafayetteFilterButtons: [], urgencyFilterButtons: [],
    matchesHistoryMajorFilter: () => false,
  });
  ctx.updateFilterButtonVisibility([{ units: 1 }]);
  assert.equal(ctx.currentFilter, 'police');
  assert.equal(ctx.currentBatonRougeAgency, 'law');
  assert.equal(ctx.currentLafayetteUnit, 'fire');
  assert.equal(ctx.currentUrgencyFilter, 'high');
});

test('history notices respect hour-long Retry-After values', () => {
  const text = {}, progress = { style: {} };
  const ctx = loadFunctions(['updateHistoryRequestNotice', 'showHistoryRequestNotice'], {
    historyRequestNoticeDeadline: 0, historyRequestNoticeDuration: 1,
    historyRequestNoticeTimer: null, historyRequestNoticeHideTimer: null,
    historyRequestNotice: { classList: { add() {} }, setAttribute() {} },
    historyRequestNoticeText: text, historyRequestNoticeProgress: progress,
    Date: { now: () => 1000000 }, setInterval: () => 1, clearInterval() {}, clearTimeout() {},
  });
  ctx.showHistoryRequestNotice(7200);
  assert.equal(ctx.historyRequestNoticeDeadline, 8200000);
  assert.match(text.textContent, /2 hours/);
});

test('map severity matches the shared cases used by report counts', () => {
  const start = html.indexOf('    function _normalizeDesc(');
  const end = html.indexOf('    function getSeverityClass(');
  const ctx = vm.createContext({});
  vm.runInContext(html.slice(start, end).replace(/^    const /gm, '    var '), ctx);
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'severity_cases.json'), 'utf8'));
  for (const { description, agency, severity } of cases) {
    assert.equal(ctx.getSeverity({ description, agency }), severity, `${description} / ${agency}`);
  }
});

test('plain wording: mixed case, spelled-out agencies and areas, plain severity words', () => {
  const ctx = loadFunctions(['normalizeSource', 'getIncidentAgencyUnits', 'formatAgencyDisplay', 'plainCase',
    'agencyPlainName', 'areaPlainName', 'severityLabel']);
  assert.equal(ctx.plainCase('CADDO EMS EVENT'), 'Caddo EMS Event');
  assert.equal(ctx.plainCase('N MARKET ST & I-49'), 'N Market St & I-49');
  assert.equal(ctx.agencyPlainName({ source: 'caddo', agency: 'SPD' }), 'Shreveport Police');
  assert.equal(ctx.agencyPlainName({ source: 'sulphur', agency: 'SPD' }), 'Sulphur Police');
  assert.equal(ctx.agencyPlainName({ source: 'caddo', agency: 'CFD3' }), 'Caddo Fire District 3');
  assert.equal(ctx.agencyPlainName({ source: 'lafayette', agency: 'POLICE FIRE' }), 'Police / Fire');
  assert.equal(ctx.agencyPlainName({ source: 'caddo', agency: 'NCMC' }), 'NCMC');
  assert.equal(ctx.areaPlainName({ source: 'caddo', municipality: 'SHV' }), 'Shreveport');
  assert.equal(ctx.areaPlainName({ source: 'caddo', municipality: 'CADD' }), '');
  assert.equal(ctx.areaPlainName({ source: 'lafayette', municipality: 'LAFAYETTE' }), 'Lafayette');
  assert.equal(ctx.severityLabel('high'), 'Serious');
  assert.equal(ctx.severityLabel('low'), 'Minor');
});

test('friendly times are 12-hour Central with Today / Yesterday / date', () => {
  const ctx = loadFunctions(['_chicagoParts', 'friendlyTimeLabel']);
  const now = new Date('2026-11-05T02:40:00Z'); // 8:40 PM CST, Nov 4
  assert.deepEqual({ ...ctx.friendlyTimeLabel({ source: 'caddo', time: '2032' }, now) }, { short: '8:32 PM', long: 'Today at 8:32 PM' });
  assert.equal(ctx.friendlyTimeLabel({ source: 'caddo', time: '0005' }, now).short, '12:05 AM');
  // Undated live feed showing a clock later than now happened yesterday.
  assert.equal(ctx.friendlyTimeLabel({ source: 'caddo', time: '2350' }, now).short, 'Yesterday 11:50 PM');
  assert.equal(ctx.friendlyTimeLabel({ source: 'sulphur', time: '2345', first_seen: '2026-10-31T04:45:43Z' }, now).short, 'Oct 30, 11:45 PM');
  assert.equal(ctx.friendlyTimeLabel({ source: 'neworleans', time: '0554', first_seen: '2026-11-04T11:54:00Z' }, now).long, 'Today at 5:54 AM');
});

test('near me keeps calls inside the circle, closest first', () => {
  const ctx = loadFunctions(['distanceMiles', 'incidentsNear', 'formatMiles'], {
    isUnknownLocation: incident => incident.street === 'Unknown',
    incidentCoordinatePair: incident => Number.isFinite(incident.latitude) ? [incident.latitude, incident.longitude] : null,
  });
  const center = [32.5, -93.75];
  const rows = ctx.incidentsNear([
    { id: 'far', latitude: 32.6, longitude: -93.75, street: 'A' },          // ~6.9 mi
    { id: 'near', latitude: 32.505, longitude: -93.75, street: 'B' },       // ~0.35 mi
    { id: 'mid', latitude: 32.52, longitude: -93.75, street: 'C' },         // ~1.4 mi
    { id: 'nomap', latitude: null, longitude: null, street: 'D' },
    { id: 'unknown', latitude: 32.5, longitude: -93.75, street: 'Unknown' },
  ], center, 3);
  assert.deepEqual(rows.map(row => row.incident.id), ['near', 'mid']);
  assert.ok(Math.abs(rows[1].miles - 1.38) < 0.05);
  assert.equal(ctx.incidentsNear([], [NaN, 1], 3).length, 0);
  assert.equal(ctx.formatMiles(0.05), 'Under 0.1 mi');
  assert.equal(ctx.formatMiles(2.345), '2.3 mi');
});

test('feed and status requests that never answer are aborted so polling continues', async () => {
  const hang = (url, options) => new Promise((_, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  const timers = [];
  const fakeTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };

  const { ctx } = liveContext();
  const outcomes = [];
  Object.assign(ctx, { fetch: hang, setTimeout: fakeTimeout, console: { error() {} } });
  ctx.window.louisiana911Analytics = { feedResult: result => outcomes.push(result) };
  const feed = ctx.updateIncidents();
  assert.equal(timers.at(-1).ms, 30000);
  timers.at(-1).fn();
  await feed;
  assert.equal(outcomes.at(-1).outcome, 'error');

  const status = loadFunctions(['updateStatus'], {
    fetch: hang, setTimeout: fakeTimeout, console: { error() {}, warn() {} },
    document: { getElementById: () => ({ classList: { contains: () => false } }) },
  });
  const pending = status.updateStatus();
  timers.at(-1).fn();
  await pending;
});

test('the poll loop always reschedules and a visibility change cannot start a second loop', async () => {
  const scheduled = [];
  let release;
  let feedCalls = 0;
  const ctx = loadFunctions(['pollLoop'], {
    pollTimer: null, pollIntervalMs: 60000, lastPollAt: 0, pollInFlight: false,
    document: { hidden: false }, console: { error() {} },
    clearTimeout() {}, setTimeout: (fn, ms) => { scheduled.push(ms); return scheduled.length; },
    updateIncidents: () => { feedCalls += 1; return new Promise(done => { release = done; }); },
    updateStatus: async () => { throw new Error('status broke'); },
  });
  const first = ctx.pollLoop();
  await ctx.pollLoop();
  assert.equal(feedCalls, 1);
  assert.equal(scheduled.length, 0);
  release();
  await first;
  assert.deepEqual(scheduled, [60000]);
  assert.equal(vm.runInContext('pollInFlight', ctx), false);
});

test('parish outlines load once from the local file, never the national county data', async () => {
  const fs = require('node:fs');
  const outlines = JSON.parse(fs.readFileSync(path.join(__dirname, '../public/parish-outlines.json'), 'utf8'));
  assert.deepEqual(outlines.features.map(f => f.id).sort(), ['22017', '22033', '22055', '22071']);
  const requested = [];
  const added = [];
  const ctx = loadFunctions(['fetchOutlineGeoJson', 'loadParishOutline'], {
    parishOutlineLayer: null, parishOutlineRequest: null, map: {},
    fetch: async url => { requested.push(url); return { ok: true, json: async () => outlines }; },
    getParishOutlineStyle: () => ({}),
    L: { geoJSON: data => ({ addTo() { added.push(data); } }) },
  });
  // Switching to the map view twice quickly must not add the outline twice.
  await Promise.all([ctx.loadParishOutline(), ctx.loadParishOutline()]);
  assert.deepEqual(requested, ['parish-outlines.json']);
  assert.equal(added.length, 1);
  assert.doesNotMatch(html, /geojson-counties-fips|data\.nola\.gov\/resource\/tu2j-8k42/);
});
