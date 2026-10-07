const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'inline frontend script exists');

class FakeNode {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.value = '';
    this.className = '';
    this.classes = new Set();
    this.listeners = new Map();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
    };
  }
  set textContent(value) { this.value = String(value ?? ''); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.value = ''; this.children = children; }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  dispatchEvent(event) { for (const callback of this.listeners.get(event.type) || []) callback(event); }
  setAttribute() {}
}

function stop(name, lat = null, lon = null) { return { name, lat, lon }; }
function leg(line, names, points, evidence, hops = 1) {
  const stops = names.map((name, index) => stop(name, points[index]?.[0] ?? null, points[index]?.[1] ?? null));
  const complete = points.length === names.length && points.every(point => point && point.length === 2);
  return {
    line_name: line, pattern_id:'internal-pattern', hops,
    origin_stop: stops[0], destination_stop: stops.at(-1), stops,
    geometry_kind: complete ? 'STOP_TO_STOP_APPROXIMATION' : 'UNAVAILABLE',
    geometry: complete ? points.map(([lat, lon]) => ({ lat, lon })) : [],
    map_geometry_source: complete ? 'KAKAO_VERIFIED_BUS_PATH' : 'UNAVAILABLE',
    map_geometry: complete ? points.map(([lat, lon]) => ({ lat, lon })) : [],
    onboard_count: evidence.count, relative_percentile: evidence.percentile,
    congestion_level: evidence.level, boarding_guidance: evidence.guidance,
    evidence_source: evidence.source,
  };
}
function transferData(firstPoints, secondPoints) {
  return {
    success: true, origin: '출발', destination: '도착', routes: [],
    reasoning: '직접 경로가 없어 1회 환승 경로를 안내합니다.', alternatives: [],
    itineraries: [{
      transfer_count: 1, total_hops: 2,
      transfer: {
        official_stop_id: 'SJB123', official_stop_name: '공식 환승점',
        historical_name_leg1: '과거명 A', historical_name_leg2: '과거명 B',
      },
      legs: [
        leg('1000', ['출발', '과거명 A'], firstPoints,
          { count: 4, percentile: 50, level: 'MEDIUM', guidance: '보통', source: 'HISTORICAL_OBSERVATION' }),
        leg('1004', ['과거명 B', '도착'], secondPoints,
          { count: 1, percentile: 10, level: 'LOW', guidance: '여유', source: 'HISTORICAL_PROFILE' }),
      ],
    }],
  };
}

async function page({ mapAvailable = true, apiResponse = null, apiError = null, searchResults = {} } = {}) {
  const nodes = new Map();
  const polylines = [];
  const overlays = [];
  const maps = [];
  let lastRequest = null;
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new FakeNode());
      return nodes.get(id);
    },
    createElement: tag => new FakeNode(tag),
    head: { appendChild(node) { queueMicrotask(() => node.onload()); } },
  };
  class LatLng { constructor(lat, lon) { this.lat = lat; this.lon = lon; } }
  class CustomOverlay {
    constructor(options) { this.options = options; this.position = options.position; overlays.push(this); }
    setMap(map) { this.map = map; }
    getPosition() { return this.position; }
    setPosition(position) { this.position = position; }
  }
  class Polyline {
    constructor(options) { this.options = options; polylines.push(this); }
    setMap(map) { this.map = map; }
  }
  class FakeMap {
    constructor() { maps.push(this); }
    setBounds(bounds, ...padding) { this.bounds = bounds.positions; this.padding = padding; }
    setCenter(position) { this.center = position; }
    setLevel(level) { this.level = level; }
  }
  class LatLngBounds {
    constructor() { this.positions = []; }
    extend(position) { this.positions.push(position); }
  }
  const kakao = { maps: {
    load(callback) { callback(); }, LatLng, CustomOverlay, Polyline,
    Map: FakeMap, LatLngBounds,
    services: {
      Geocoder: class { addressSearch(query, callback) { callback([], 'ZERO_RESULT'); } },
      Places: class { keywordSearch(query, callback) { callback([], 'ZERO_RESULT'); } },
      Status: { OK: 'OK' },
    },
  } };
  const fetch = async (url, options) => {
    if (url === '/api/frontend-config') {
      return mapAvailable
        ? { ok: true, json: async () => ({ kakao_map_javascript_key: 'test-key' }) }
        : { ok: false, json: async () => ({ detail: '지도 설정 없음' }) };
    }
    if (url === '/api/predict') {
      lastRequest = JSON.parse(options.body);
      if (apiError) return { ok: false, status: apiError.status, json: async () => ({ detail: apiError.detail }) };
      return { ok: true, status: 200, json: async () => apiResponse };
    }
    if (url.startsWith('/api/stops/search?')) {
      const q = new URL(url, 'http://localhost').searchParams.get('q');
      return { ok: true, json: async () => ({ stops: searchResults[q] || [] }) };
    }
    throw new Error(`unexpected request: ${url}`);
  };
  const context = vm.createContext({ document, fetch, kakao, URLSearchParams, queueMicrotask,
    setTimeout, clearTimeout, Event, console: { log() {}, warn() {}, error() {} } });
  context.globalThis = context;
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return { context, nodes, polylines, overlays, maps, request: () => lastRequest,
    call(name, arg) { context.__testArg = arg; vm.runInContext(`${name}(__testArg)`, context); },
    async predict() { await vm.runInContext('predictRoute()', context); },
    async select(key, query, index = 0) {
      const input = nodes.get(key === 'origin' ? 'origin-input' : 'dest-input');
      const list = nodes.get(key === 'origin' ? 'origin-candidates' : 'dest-candidates');
      input.value = query;
      input.dispatchEvent({ type: 'input' });
      await new Promise(resolve => setTimeout(resolve, 220));
      assert.ok(list.children[index], `search candidate for ${query}`);
      const label = list.children[index].textContent;
      list.children[index].dispatchEvent({ type: 'click' });
      return label;
    },
  };
}

async function run() {
  const coords1 = [[36.1, 127.1], [36.2, 127.2]];
  const coords2 = [[36.2, 127.2], [36.3, 127.3]];
  const transfer = transferData(coords1, coords2);
  const ui = await page();
  ui.call('displayResults', transfer);
  const result = ui.nodes.get('routes-container');
  assert.equal(result.children.length, 1, 'routes=[] plus itineraries is a success');
  assert.equal(result.children[0].tag, 'article');
  assert.ok(result.textContent.includes('1구간'));
  assert.ok(result.textContent.includes('2구간'));
  assert.ok(result.textContent.includes('환승'));
  for (const value of ['1000', '1004', '재차인원 4명', '재차인원 1명',
    '과거명 A', '과거명 B', '보통', '여유', '2정류장']) {
    assert.ok(result.textContent.includes(value), value);
  }
  for (const value of ['50%', '10%', 'HISTORICAL_OBSERVATION', 'HISTORICAL_PROFILE',
    'SJB123', 'STOP_TO_STOP_APPROXIMATION', 'NEXT', 'internal-pattern']) {
    assert.ok(!result.textContent.includes(value), `hidden detail: ${value}`);
  }
  assert.equal(ui.polylines.filter(line => line.map).length, 2);
  assert.deepEqual(ui.polylines.map(line => line.options.path.length), [2, 2]);
  assert.deepEqual(ui.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent),
    ['출발', '도착', '환승']);
  assert.deepEqual(ui.maps[0].padding, [72, 56, 72, 56]);
  assert.ok(ui.maps[0].bounds.some(point => point.lat === 36.1 && point.lon === 127.1));
  assert.ok(ui.maps[0].bounds.some(point => point.lat === 36.3 && point.lon === 127.3));
  assert.equal(ui.polylines[0].options.strokeColor, '#4f46e5');
  assert.equal(ui.polylines[1].options.strokeColor, '#047857');

  const extendedTransfer = transferData(coords1, coords2);
  extendedTransfer.itineraries[0].legs[0].map_geometry.splice(1, 0, {lat:36.15,lon:127.9});
  extendedTransfer.itineraries[0].legs[1].map_geometry.splice(1, 0, {lat:36.25,lon:128.1});
  const extendedPage = await page();
  extendedPage.call('displayResults', extendedTransfer);
  assert.ok(extendedPage.maps[0].bounds.some(point => point.lat === 36.15 && point.lon === 127.9));
  assert.ok(extendedPage.maps[0].bounds.some(point => point.lat === 36.25 && point.lon === 128.1));

  const direct = { success: true, reasoning: '직접 경로', alternatives: [], itineraries: [],
    routes: [leg('B1', ['대전역', '세종시청'], [],
      { count: 17, percentile: 56, level: 'MEDIUM', guidance: '보통', source: 'HISTORICAL_OBSERVATION' })] };
  ui.call('displayResults', direct);
  assert.equal(result.children[0].tag, 'article', 'direct route uses a compact card');
  assert.ok(result.textContent.includes('B1'));
  assert.ok(!result.textContent.includes('HISTORICAL_OBSERVATION'));
  assert.equal(ui.polylines.filter(line => line.map).length, 0, 'transfer lines are cleared');
  assert.deepEqual(ui.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent), []);

  const unmappedDestination = leg('B1', ['대전역', '중간 정류장', '세종시청.교육청.시의회'],
    [[36.33, 127.43], [36.4, 127.5]],
    { count: 17, percentile: 56, level: 'MEDIUM', guidance: '보통', source: 'HISTORICAL_OBSERVATION' });
  const endpointPage = await page();
  endpointPage.call('displayResults', { routes: [unmappedDestination], itineraries: [], alternatives: [] });
  assert.deepEqual(endpointPage.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent),
    ['출발'], 'an intermediate coordinate cannot stand in for an unmapped destination');
  assert.equal(endpointPage.polylines.filter(line => line.map).length, 0);
  unmappedDestination.origin_stop.lat = null;
  unmappedDestination.origin_stop.lon = null;
  endpointPage.call('displayResults', { routes: [unmappedDestination], itineraries: [], alternatives: [] });
  assert.deepEqual(endpointPage.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent), [],
    'an intermediate coordinate cannot stand in for an unmapped origin');

  const incompleteTransfer = transferData(coords1, coords2);
  incompleteTransfer.itineraries[0].legs[1].origin_stop.lat = null;
  incompleteTransfer.itineraries[0].legs[1].origin_stop.lon = null;
  const transferEndpointPage = await page();
  transferEndpointPage.call('displayResults', incompleteTransfer);
  assert.deepEqual(transferEndpointPage.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent),
    ['출발', '도착'], 'transfer marker requires matching coordinates on both verified leg endpoints');

  const directWithPath = leg('1000', ['출발', '도착'], coords1,
    { count: 7, percentile: 50, level: 'MEDIUM', guidance: '보통', source: 'HISTORICAL_OBSERVATION' });
  ui.call('displayResults', { routes: [directWithPath], itineraries: [], alternatives: [] });
  assert.equal(ui.polylines.filter(line => line.map).length, 1, 'validated direct BUS path is drawn');
  assert.deepEqual(ui.overlays.filter(overlay => overlay.map).map(overlay => overlay.options.content.textContent),
    ['출발', '도착']);
  assert.ok(result.textContent.includes('재차인원 7명'));
  assert.ok(result.textContent.includes('2정류장'));
  assert.ok(!result.textContent.includes('50%'));
  directWithPath.map_geometry_source = 'UNAVAILABLE';
  ui.call('displayResults', { routes: [directWithPath], itineraries: [], alternatives: [] });
  assert.equal(ui.polylines.filter(line => line.map).length, 0, 'approximation never becomes a map line');
  assert.equal(ui.maps[0].bounds.length, 2, 'missing geometry fits only available stop markers');

  const longPath = leg('1000', ['출발', '도착'], coords1,
    { count: 9, percentile: 60, level: 'HIGH', guidance: '혼잡', source: 'HISTORICAL_OBSERVATION' });
  longPath.map_geometry = [
    {lat:36.1,lon:127.1}, {lat:36.9,lon:127.7}, {lat:36.3,lon:127.5}, {lat:36.2,lon:127.2}
  ];
  const longPage = await page();
  longPage.call('displayResults', {routes:[longPath],itineraries:[]});
  assert.equal(longPage.polylines[0].options.path.length, 4);
  assert.equal(longPage.maps[0].bounds.length, 6, 'all path points and both markers fit');
  assert.ok(longPage.maps[0].bounds.some(point => point.lat === 36.9 && point.lon === 127.7));
  assert.ok(longPage.nodes.get('routes-container').textContent.includes('혼잡'));

  for (const [level, label, color] of [
    ['LOW', '여유', 'green'], ['MEDIUM', '보통', 'yellow'],
    ['HIGH', '혼잡', 'orange'], ['VERY_HIGH', '매우 혼잡', 'red'],
    ['UNKNOWN', '정보 없음', 'gray'],
  ]) {
    const statePage = await page();
    const stateLeg = leg('1000', ['출발', '도착'], coords1,
      {count:level === 'UNKNOWN' ? null : 9, percentile:75, level,
       guidance:label, source:'HISTORICAL_OBSERVATION'});
    statePage.call('displayResults', {routes:[stateLeg],itineraries:[]});
    const card = statePage.nodes.get('routes-container').children[0];
    const chip = card.children[2].children[0];
    assert.ok(chip.textContent.includes(label));
    assert.ok(chip.className.includes(color));
    assert.ok(!card.textContent.includes('%'), 'historical percentile is not occupancy');
    if (level === 'UNKNOWN') assert.ok(card.textContent.includes('재차인원 정보 없음'));
  }

  for (const [first, second, expectedLines] of [
    [[], [], 0], [coords1, [], 1], [[], coords2, 1], [coords1, coords2, 2],
  ]) {
    const testPage = await page();
    testPage.call('displayResults', transferData(first, second));
    assert.equal(testPage.polylines.filter(line => line.map).length, expectedLines);
    assert.equal(testPage.nodes.get('routes-container').children.length, 1);
  }
  const malformed = transferData(coords1, coords2);
  malformed.itineraries[0].legs[0].map_geometry_source = 'UNAVAILABLE';
  const noInterpolation = await page();
  noInterpolation.call('displayResults', malformed);
  assert.equal(noInterpolation.polylines.filter(line => line.map).length, 1);

  const empty = await page();
  empty.call('displayResults', { result_status:'NO_SUPPORTED_ROUTE', routes: [], itineraries: [] });
  assert.ok(empty.nodes.get('routes-container').textContent.includes('현재 데이터 범위에서 경로를 찾지 못했습니다'));
  assert.ok(empty.nodes.get('routes-container').textContent.includes('다른 인근 정류장'));
  assert.equal(empty.polylines.filter(line => line.map).length, 0);
  const noMap = await page({ mapAvailable: false });
  noMap.call('displayResults', transfer);
  assert.equal(noMap.nodes.get('routes-container').children.length, 1);
  assert.ok(noMap.nodes.get('map').textContent.includes('지도 설정 없음'));

  const apiPage = await page({ apiResponse: transfer, searchResults: {
    '출발': [{ stop_id: 'origin-id', stop_name: '출발', line_names: ['1000', '1001', '1002', '1003', '1004'] }],
    '현재 환승점': [{ stop_id: 'dest-id', stop_name: '도착', occurrence_id: 'dest-occ',
      match_kind: 'VERIFIED_OFFICIAL_ALIAS', official_stop_name: '현재 환승점', line_names: ['1004'] }],
  } });
  await apiPage.predict();
  assert.ok(apiPage.nodes.get('status').textContent.includes('모두 입력하세요'));
  apiPage.nodes.get('date-input').value = '2025-11-08';
  apiPage.nodes.get('time-input').value = '08:00';
  apiPage.nodes.get('origin-input').value = '아무 카카오 장소';
  apiPage.nodes.get('dest-input').value = '도착';
  await apiPage.predict();
  assert.ok(apiPage.nodes.get('status').textContent.includes('정류장 검색 결과'));
  assert.equal(apiPage.request(), null, 'geocodable text never proves route identity');
  const originLabel = await apiPage.select('origin', '출발');
  assert.ok(originLabel.includes('외 2개 노선'));
  assert.ok(!originLabel.includes('1004'));
  assert.ok(apiPage.nodes.get('origin-candidates').textContent === '');
  const aliasLabel = await apiPage.select('destination', '현재 환승점');
  assert.ok(aliasLabel.includes('현재 명칭: 현재 환승점'));
  await apiPage.predict();
  assert.equal(apiPage.request().origin, '출발');
  assert.equal(apiPage.request().origin_stop_id, 'origin-id');
  assert.equal(apiPage.request().destination_stop_id, 'dest-id');
  assert.equal(apiPage.request().destination_occurrence_id, 'dest-occ');
  assert.equal(apiPage.nodes.get('routes-container').children[0].tag, 'article');
  apiPage.nodes.get('origin-input').value = '출발 변경';
  apiPage.nodes.get('origin-input').dispatchEvent({ type: 'input' });
  assert.equal(vm.runInContext('selectedStops.origin', apiPage.context), null);
  assert.ok(apiPage.nodes.get('results-section').classes.has('hidden'));
  await apiPage.predict();
  assert.ok(apiPage.nodes.get('status').textContent.includes('정류장 검색 결과'));

  const noRoutePage = await page({apiResponse:{success:true,result_status:'NO_SUPPORTED_ROUTE',
    routes:[],itineraries:[],alternatives:[]}});
  noRoutePage.nodes.get('origin-input').value = '출발';
  noRoutePage.nodes.get('dest-input').value = '도착';
  noRoutePage.nodes.get('date-input').value = '2025-11-08';
  noRoutePage.nodes.get('time-input').value = '09:00';
  vm.runInContext("selectedStops.origin = {stop_id:'a'}; selectedStops.destination = {stop_id:'b'}", noRoutePage.context);
  await noRoutePage.predict();
  assert.ok(noRoutePage.nodes.get('routes-container').textContent.includes('현재 데이터 범위'));
  assert.equal(noRoutePage.nodes.get('status').className, 'hidden');

  const invalidPage = await page({ apiError: { status: 422, detail: '잘못된 요청' } });
  invalidPage.nodes.get('origin-input').value = '출발';
  invalidPage.nodes.get('dest-input').value = '도착';
  invalidPage.nodes.get('date-input').value = '2025-11-08';
  invalidPage.nodes.get('time-input').value = '08:00';
  vm.runInContext("selectedStops.origin = {stop_id:'a'}; selectedStops.destination = {stop_id:'b'}", invalidPage.context);
  await invalidPage.predict();
  assert.ok(invalidPage.nodes.get('status').textContent.includes('잘못된 요청'));
  assert.ok(invalidPage.nodes.get('results-section').classes.has('hidden'));

  if (process.argv[2]) {
    const live = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const livePage = await page();
    livePage.call('displayResults', live);
    assert.equal(livePage.nodes.get('routes-container').children[0].tag, 'article');
    assert.ok(livePage.nodes.get('routes-container').textContent.includes('1구간'));
    assert.ok(livePage.nodes.get('routes-container').textContent.includes('2구간'));
    console.log('Live FastAPI response rendered through frontend script');
  }
  console.log('Transfer frontend contract checks passed');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
