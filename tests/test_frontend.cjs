const { test } = require('node:test');
const assert = require('node:assert/strict');
const { dailyRainfall, lineSegments, fetchAPI, number, escapeHTML, dateKey } = require('../script.js');

test('missing numbers remain blank; zero remains a valid measurement', () => {
  assert.equal(number(null), '—');
  assert.equal(number(undefined), '—');
  assert.equal(number(NaN), '—');
  assert.equal(number(0), '0.0');
});

test('rainfall groups by Philippine dates and records partial coverage', () => {
  const days = dailyRainfall([
    { timestamp_ph: '2026-10-15T15:00:00Z', precipitation_mm: 0 },
    { timestamp_ph: '2026-10-15T16:00:00Z', precipitation_mm: 2.5 },
    { timestamp_ph: '2026-10-15T16:00:00Z', precipitation_mm: 2.5 },
    { timestamp_ph: '2026-10-15T17:00:00Z', precipitation_mm: null },
    { timestamp_ph: '2026-10-16T16:00:00Z', precipitation_mm: null },
  ]);
  assert.equal(days[0].date, '2026-10-15');
  assert.equal(days[0].total, 0);
  assert.equal(days[1].total, 2.5);
  assert.equal(days[1].hours, 1);
  assert.equal(days[1].wet, 1);
  assert.equal(days[1].complete, false);
  assert.equal(days[2].total, null);
  assert.equal(dateKey('invalid'), null);
});

test('24 valid precipitation hours make a complete local day', () => {
  const rows = Array.from({ length: 24 }, (_, i) => ({
    timestamp_ph: `2026-10-15T${String(i).padStart(2, '0')}:00:00+08:00`, precipitation_mm: 1,
  }));
  assert.deepEqual(dailyRainfall(rows)[0], { date: '2026-10-15', hours: 24, wet: 24, total: 24, complete: true });
});

test('line charts break at missing values and absent hours', () => {
  const rows = [0, 1, 2, 3, 6].map((hour, i) => ({
    timestamp_ph: `2026-10-15T0${hour}:00:00Z`, temperature_c: i === 2 ? null : 30 + i,
  }));
  const segments = lineSegments(rows, 'temperature_c', '2026-10-15T00:00:00Z', '2026-10-16T00:00:00Z');
  assert.deepEqual(segments.map(segment => segment.length), [2, 1, 1]);
});

test('requests use the configured API and reject error/invalid responses', async () => {
  let url;
  const result = await fetchAPI('https://philippine-weather-analytics.onrender.com/api/v1/', 'cities', { fetchImpl: async input => {
    url = String(input); return { ok: true, json: async () => ({ data: [] }) };
  } });
  assert.equal(url, 'https://philippine-weather-analytics.onrender.com/api/v1/cities');
  assert.deepEqual(result.data, []);
  await assert.rejects(fetchAPI('http://localhost/', 'cities', { fetchImpl: async () => ({ ok: false, status: 503 }) }), /database unavailable/);
  await assert.rejects(fetchAPI('http://localhost/', 'cities', { fetchImpl: async () => ({ ok: true, json: async () => ({ wrong: [] }) }) }), /Unexpected/);
});

test('request timeout ends loading and caller cancellation is preserved', async () => {
  const pending = (input, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  });
  await assert.rejects(fetchAPI('http://localhost/', 'cities', { timeout: 5, fetchImpl: pending }), /timed out/);
  const controller = new AbortController();
  const request = fetchAPI('http://localhost/', 'cities', { signal: controller.signal, fetchImpl: pending });
  controller.abort();
  await assert.rejects(request, error => error.name === 'AbortError');
});

test('API-supplied labels are escaped before HTML rendering', () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
});
