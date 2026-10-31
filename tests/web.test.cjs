const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'gold_watch/static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'gold_watch/static/app.js'), 'utf8');

async function waitFor(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('UI did not reach expected state');
}

async function createUI({trends = {}, latest = {}, target = null, targetUnit = 'original'} = {}) {
  const dom = new JSDOM(html, {url: 'http://admin:temporary-test-password@localhost:8089/', runScripts: 'outside-only'});
  const win = dom.window;
  const schedule = time => ({enabled: true, time, title: '金价简报', template: '{price} {unit}', target_price: null, note: ''});
  let settings = {
    scheduler_enabled: false, international: schedule('08:00'), domestic: schedule('09:30'),
    international_provider: 'gold_api', domestic_provider: 'sina', catchup_minutes: 30,
    domestic_premium: '0', allow_fallback: true,
    webhook_configured: false, dingtalk_secret_configured: false,
    goldapi_key_configured: false, juhe_key_configured: false,
  };
  settings.international.target_price = target;
  settings.international.target_unit = targetUnit;
  const calls = [];
  let refresh;
  win.setInterval = fn => {refresh = fn; return 1;};
  win.fetch = async (url, options) => {
    // Regression: relative fetch failed when the page URL contained Basic credentials.
    assert.equal(url.username, '');
    assert.equal(url.password, '');
    assert.equal(url.origin, 'http://localhost:8089');
    calls.push({path: url.pathname, method: options.method, body: options.body});
    let data;
    if (url.pathname === '/api/state') {
      data = {settings, latest, trends, next_runs: {}, history: [], server_time: '2026-10-30T08:00:00+08:00', scheduler: {error: ''}};
    } else if (url.pathname === '/api/settings') {
      const update = JSON.parse(options.body);
      settings = {...settings, ...update};
      data = {status: 'saved'};
    } else if (url.pathname === '/api/preview/international') {
      data = {title: '国际金价早报', message: '国际现货黄金：3900.25 美元/金衡盎司', status: '近期参考报价', quote: {
        price: '3900.25', quote_time: '2026-10-30T08:00:00+08:00', source: 'Test source', warnings: [],
      }};
    } else {
      assert.fail('Unexpected API action: ' + url.pathname);
    }
    return {ok: true, json: async () => structuredClone(data)};
  };
  win.eval(script);
  await waitFor(() => win.document.getElementById('international-time').value === '08:00');
  return {dom, win, calls, getSettings: () => settings, refresh: () => refresh()};
}

test('configuration form saves independent schedules', async t => {
  const ui = await createUI();
  t.after(() => ui.dom.window.close());
  const {document, Event} = ui.win;
  document.querySelector('[data-page="settings"]').click();
  assert.equal(document.getElementById('page-settings').classList.contains('hidden'), false);
  const input = document.getElementById('international-time');
  input.value = '07:45';
  input.dispatchEvent(new Event('input', {bubbles: true}));
  document.getElementById('domestic-time').value = '10:00';
  document.getElementById('settings-form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
  await waitFor(() => ui.getSettings().international.time === '07:45');
  await waitFor(() => document.getElementById('save-settings').textContent === '保存配置');
  assert.equal(ui.getSettings().domestic.time, '10:00');
  assert.equal(ui.getSettings().scheduler_enabled, false);
});

test('weekly chart leaves gaps, supports point inspection and shows daily sample table', async t => {
  const prices = ['100', '102', null, '105', '104', null, '110'];
  const trend = {
    points: prices.map((price, index) => ({date: `2026-10-${25 + index}`, price,
      quote_time: price === null ? null : `2026-10-${25 + index}T22:00:00+08:00`})),
    summary: '近7日采样：+10.00 美元/金衡盎司（+10.00%）；5/7天有记录',
    source: 'Test source', unit: '美元/金衡盎司', low: '100', high: '110',
    note: '每天取最后一条采集报价，缺失日期留空。',
  };
  const ui = await createUI({trends: {international: trend}});
  t.after(() => ui.dom.window.close());
  const root = ui.win.document.getElementById('trend-international');
  assert.equal(root.querySelectorAll('polyline').length, 2);
  assert.equal(root.querySelectorAll('circle').length, 5);
  assert.equal(root.querySelectorAll('tbody tr').length, 7);
  assert.equal(root.querySelectorAll('tbody tr')[2].textContent.includes('无记录'), true);
  root.querySelector('circle').dispatchEvent(new ui.win.Event('focus'));
  assert.match(root.querySelector('.trend-readout').textContent, /100\.00/);
  assert.match(root.querySelector('.trend-summary').textContent, /5\/7天/);
});

test('target distance is shown using target price as the percentage baseline', async t => {
  const ui = await createUI({target: '100', latest: {international: {status: '周末参考', quote: {
    price: '110', unit: '美元/金衡盎司', quote_time: '2026-10-31T22:00:00+08:00', source: 'Test', warnings: [],
  }, target: {text: '观察目标：100.00 美元/金衡盎司；距观察目标还差 10.00 美元/金衡盎司，约 10.00%'}}}});
  t.after(() => ui.dom.window.close());
  const target = ui.win.document.getElementById('target-gap-international');
  assert.equal(target.classList.contains('hidden'), false);
  assert.match(target.textContent, /距观察目标还差 10\.00 美元\/金衡盎司，约 10\.00%/);
  assert.equal(ui.win.document.getElementById('target-gap-domestic').classList.contains('hidden'), true);
});

test('international target unit and amount save together and survive rehydration', async t => {
  const ui = await createUI();
  t.after(() => ui.dom.window.close());
  const {document, Event} = ui.win;
  const select = document.getElementById('international-target-unit');
  select.value = 'cny_per_gram';
  select.dispatchEvent(new Event('input', {bubbles: true}));
  select.dispatchEvent(new Event('change', {bubbles: true}));
  document.getElementById('international-target').value = '800';
  assert.match(document.getElementById('international-target-label').textContent, /人民币\/克/);
  assert.match(document.getElementById('international-target-help').textContent, /不计商家加价/);
  document.getElementById('settings-form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
  await waitFor(() => ui.getSettings().international.target_unit === 'cny_per_gram');
  await waitFor(() => document.getElementById('save-settings').textContent === '保存配置');
  assert.equal(ui.getSettings().international.target_price, '800');
  assert.equal(select.value, 'cny_per_gram');
  assert.equal(document.getElementById('international-target').value, '800');
  select.value = 'original';
  select.dispatchEvent(new Event('change', {bubbles: true}));
  assert.match(document.getElementById('international-target-label').textContent, /美元\/金衡盎司/);
  assert.equal(document.getElementById('international-target').value, '800');
});

test('overview uses server target result rather than comparing dollars against yuan', async t => {
  const text = '观察目标：800.00 元/克（国际金价人民币换算）；距观察目标还差 31.22 元/克，约 3.90%';
  const ui = await createUI({target: '800', targetUnit: 'cny_per_gram', latest: {international: {
    status: '周末参考', quote: {price: '3876.40', unit: '美元/金衡盎司', quote_time: '2026-10-31T23:30:00+08:00',
      source: 'Test', warnings: []}, target: {text},
  }}});
  t.after(() => ui.dom.window.close());
  assert.equal(ui.win.document.getElementById('target-gap-international').textContent, text);
  assert.equal(ui.win.document.getElementById('international-target-unit').value, 'cny_per_gram');
});

test('international quote shows yuan per gram with FX source and update time', async t => {
  const ui = await createUI({latest: {international: {status: '周末参考', quote: {
    price: '3876.40', unit: '美元/金衡盎司', quote_time: '2026-10-31T23:30:00+08:00', source: 'Test', warnings: [],
  }, conversion: {available: true, price: '831.22', rate: '6.669566', updated_at: '2026-10-31T08:02:31+08:00',
    source: 'ExchangeRate-API（日更参考）', source_url: 'https://www.exchangerate-api.com/', notes: []}}}});
  t.after(() => ui.dom.window.close());
  const document = ui.win.document;
  assert.equal(document.getElementById('conversion-international').classList.contains('hidden'), false);
  assert.match(document.getElementById('cny-price-international').textContent, /831\.22 元\/克/);
  assert.match(document.getElementById('exchange-rate-international').textContent, /1 美元 ≈ 6\.669566 元人民币/);
  assert.equal(document.getElementById('exchange-source-international').href, 'https://www.exchangerate-api.com/');
});

test('background refresh preserves unsaved edits', async t => {
  const ui = await createUI();
  t.after(() => ui.dom.window.close());
  const input = ui.win.document.getElementById('international-title');
  input.value = '尚未保存的新标题';
  input.dispatchEvent(new ui.win.Event('input', {bubbles: true}));
  await ui.refresh();
  assert.equal(input.value, '尚未保存的新标题');
  assert.equal(ui.win.document.getElementById('save-settings').textContent, '保存配置 · 有修改');
});

test('preview renders price and message without sending to DingTalk', async t => {
  const ui = await createUI();
  t.after(() => ui.dom.window.close());
  ui.win.document.querySelector('[data-preview="international"]').click();
  await waitFor(() => ui.win.document.getElementById('message-preview').textContent.includes('3900.25'));
  assert.equal(ui.win.document.getElementById('price-international').textContent, '3,900.25');
  assert.equal(ui.calls.some(call => call.path.startsWith('/api/send/')), false);
});
