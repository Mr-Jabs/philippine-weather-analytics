/* PH Weather: API-backed rendering. No sample readings or offline numeric fallbacks. */
(() => {
  "use strict";
  const METRICS = {
    temperature_c: { label: "Hourly temperature", unit: "°C" },
    precipitation_mm: { label: "Hourly rainfall", unit: "mm" },
    heat_index_c: { label: "Hourly heat index", unit: "°C" },
  };
  const validNumber = value => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 1) => validNumber(value) ? value.toFixed(digits) : "—";
  const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const dateKey = value => {
    const stamp = new Date(value);
    return Number.isFinite(stamp.getTime()) ? new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Manila", year: "numeric", month: "2-digit", day: "2-digit",
    }).format(stamp) : null;
  };
  const dateLabel = (value, withTime = false) => {
    const stamp = new Date(value);
    if (!Number.isFinite(stamp.getTime())) return "Time unavailable";
    return new Intl.DateTimeFormat("en-PH", {
      timeZone: "Asia/Manila", month: "short", day: "numeric",
      ...(withTime ? { hour: "numeric", minute: "2-digit" } : {}),
    }).format(stamp);
  };
  function dailyRainfall(rows) {
    const days = new Map();
    const seen = new Set();
    for (const row of [...rows].sort((a, b) => Date.parse(a.timestamp_ph) - Date.parse(b.timestamp_ph))) {
      const key = dateKey(row.timestamp_ph);
      const instant = Date.parse(row.timestamp_ph);
      if (!key || seen.has(instant)) continue;
      seen.add(instant);
      if (!days.has(key)) days.set(key, { date: key, total: 0, hours: 0, wet: 0 });
      const day = days.get(key);
      if (validNumber(row.precipitation_mm) && row.precipitation_mm >= 0) {
        day.total += row.precipitation_mm;
        day.hours++;
        if (row.precipitation_mm > 0) day.wet++;
      }
    }
    return [...days.values()].map(day => ({ ...day, total: day.hours ? day.total : null, complete: day.hours === 24 }));
  }
  function lineSegments(rows, field, start, end) {
    const lower = Date.parse(start), upper = Date.parse(end);
    const selected = rows.filter(row => {
      const t = Date.parse(row.timestamp_ph);
      return t >= lower && t < upper;
    }).sort((a, b) => Date.parse(a.timestamp_ph) - Date.parse(b.timestamp_ph));
    const segments = [];
    let segment = [], previous = null;
    for (const row of selected) {
      const t = Date.parse(row.timestamp_ph);
      if (!validNumber(row[field]) || (previous !== null && t - previous > 5400000)) {
        if (segment.length) segments.push(segment);
        segment = [];
      }
      if (validNumber(row[field])) segment.push({ time: t, value: row[field] });
      previous = t;
    }
    if (segment.length) segments.push(segment);
    return segments;
  }
  async function fetchAPI(base, path, { signal, timeout = 12000, fetchImpl = globalThis.fetch } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) controller.abort();
    const timer = setTimeout(abort, timeout);
    try {
      const response = await fetchImpl(new URL(path, base.endsWith("/") ? base : base + "/"), {
        signal: controller.signal, headers: { Accept: "application/json" }, cache: "no-store",
      });
      if (!response.ok) throw new Error(response.status === 503 ? "Weather database unavailable" : "Weather request failed (" + response.status + ")");
      const payload = await response.json();
      if (!payload || !Array.isArray(payload.data)) throw new Error("Unexpected weather response");
      return payload;
    } catch (error) {
      if (error.name === "AbortError" && !signal?.aborted) throw new Error("Weather request timed out");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  // Pure helpers are exported for dependency-free Node regression checks.
  if (typeof module !== "undefined" && module.exports) module.exports = { dailyRainfall, lineSegments, fetchAPI, number, escapeHTML, dateKey };
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const API = document.querySelector('meta[name="weather-api-base"]').content;
  const state = { cities: [], city: null, metric: "temperature_c", latest: null, history: null,
    comparison: null, errors: {}, loading: true, sequence: 0, controller: null };
  const currentCity = () => state.cities.find(city => city.city_id === state.city);
  const initials = name => name.split(/\s+/).slice(0, 2).map(word => word[0]).join("").toUpperCase();
  function metricHTML(value, unit) {
    const formatted = number(value);
    const [whole, fraction] = formatted.split(".");
    return escapeHTML(whole) + (fraction ? '<span class="decimal">.' + fraction + "</span>" : "") +
      '<span class="unit">' + escapeHTML(unit) + "</span>";
  }
  function message(target, title, detail = "", loading = false) {
    $(target).replaceChildren();
    const wrapper = document.createElement("div");
    wrapper.className = "chart-state";
    if (loading) {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      wrapper.append(spinner);
    }
    const label = document.createElement("span");
    label.textContent = title;
    wrapper.append(label);
    if (detail) {
      const caption = document.createElement("small");
      caption.textContent = detail;
      wrapper.append(caption);
    }
    $(target).append(wrapper);
  }
  function setBusy() {
    for (const id of ["current-panel", "trends", "comparison", "cities-panel", "rainfall-panel"]) {
      $(id).setAttribute("aria-busy", String(state.loading));
    }
  }
  function renderCities() {
    const query = $("city-search").value.trim().toLowerCase();
    const filtered = state.cities.filter(city => city.city_name.toLowerCase().includes(query));
    for (const [target, tab] of [["city-tabs", true], ["city-navigation", false]]) {
      const fragment = document.createDocumentFragment();
      for (const city of filtered) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = (tab ? "city-tab" : "city-nav-button") + (city.city_id === state.city ? " active" : "");
        button.dataset.city = String(city.city_id);
        button.setAttribute("aria-pressed", String(city.city_id === state.city));
        if (tab) {
          const badge = document.createElement("span");
          badge.className = "city-initial";
          badge.textContent = initials(city.city_name);
          badge.setAttribute("aria-hidden", "true");
          button.append(badge);
        }
        button.append(document.createTextNode(city.city_name));
        fragment.append(button);
      }
      $(target).replaceChildren(fragment);
      if (!filtered.length) {
        const empty = document.createElement("span");
        empty.className = "nav-placeholder";
        empty.textContent = state.cities.length ? "No matching cities" : "City list unavailable";
        $(target).append(empty);
      }
    }
    $("city-count").textContent = String(state.cities.length);
    const options = state.cities.map(city => new Option(city.city_name, city.city_id, false, city.city_id === state.city));
    $("city-select").replaceChildren(...options);
    if (!options.length) $("city-select").append(new Option(state.loading ? "Loading cities…" : "Unavailable", ""));
    $("city-select").disabled = !options.length;
    $("selected-city-label").textContent = currentCity()?.city_name || "Select a city";
    $("trend-city").textContent = currentCity()?.city_name || "Selected city";
  }
  function renderCurrent() {
    const row = state.latest?.data.find(row => row.city_id === state.city);
    $("temperature-value").innerHTML = metricHTML(row?.temperature_c, "°C");
    $("humidity-value").innerHTML = metricHTML(row?.humidity_pct, "%");
    $("heat-value").innerHTML = metricHTML(row?.heat_index_c, "°C");
    $("rain-value").innerHTML = metricHTML(row?.precipitation_mm, "mm");
    $("wind-value").textContent = "Wind " + number(row?.wind_speed_kmh) + " km/h";
    $("reading-time").textContent = row ? dateLabel(row.timestamp_ph, true) + " PHT · stored model reading" : "Hourly model conditions · Philippine time";
    const old = row && Date.now() - Date.parse(row.timestamp_ph) > 3 * 3600000;
    $("weather-status").textContent = state.loading ? "Loading conditions" : !row ? "No current reading" :
      old ? "Older reading" : validNumber(row.precipitation_mm) ? row.precipitation_mm > 0 ? "Rain recorded" : "No recorded rainfall" : "Model conditions";
    $("current-feedback").replaceChildren();
    if (state.loading) {
      const spinner = document.createElement("span"); spinner.className = "spinner";
      $("current-feedback").append(spinner, "Loading latest readings");
    } else {
      $("current-feedback").textContent = state.errors.latest ? "Current weather is unavailable. Try refreshing." :
        !row ? "No stored weather for this city yet." : old ? "This reading is over 3 hours old." :
          "Missing measurements are shown as —.";
    }
    if (state.loading) return message("city-readings", "Loading city readings", "", true);
    if (state.errors.latest) return message("city-readings", "City readings unavailable", "The API could not return current conditions.");
    const fragment = document.createDocumentFragment();
    for (const city of state.cities) {
      const reading = state.latest?.data.find(row => row.city_id === city.city_id);
      const button = document.createElement("button");
      button.className = "city-reading" + (city.city_id === state.city ? " selected" : "");
      button.dataset.city = String(city.city_id);
      button.setAttribute("aria-pressed", String(city.city_id === state.city));
      button.innerHTML = '<span class="city-initial">' + escapeHTML(initials(city.city_name)) + '</span><span class="city-reading-label"><strong>' +
        escapeHTML(city.city_name) + "</strong><small>" + (reading ? escapeHTML(dateLabel(reading.timestamp_ph, true)) + " PHT" : "No stored reading") +
        '</small></span><span class="city-reading-value">' + number(reading?.temperature_c) + "<span>°C</span></span>";
      fragment.append(button);
    }
    $("city-readings").replaceChildren(fragment);
  }
  function renderTrend() {
    const metric = METRICS[state.metric];
    $("trend-description").textContent = metric.label + " · " + metric.unit;
    if (state.loading) return message("trend-chart", "Loading hourly trends", "", true);
    if (state.errors.history) return message("trend-chart", "Trend unavailable", "Current conditions may still be available. Try refreshing.");
    const history = state.history;
    if (!history?.data.length) return message("trend-chart", "No historical readings yet", "There are no stored readings for this city in the last 7 days.");
    const segments = lineSegments(history.data, state.metric, history.start, history.end);
    const points = segments.flat();
    if (!points.length) return message("trend-chart", "No readings for this metric", "Try another metric or city. Missing values are not replaced with zero.");
    const lower = Date.parse(history.start), upper = Date.parse(history.end);
    if (!Number.isFinite(lower) || !Number.isFinite(upper) || upper <= lower) return message("trend-chart", "Invalid chart window", "Try refreshing the historical data.");
    let min = Math.min(...points.map(point => point.value)), max = Math.max(...points.map(point => point.value));
    if (state.metric === "precipitation_mm") min = 0;
    const pad = state.metric === "precipitation_mm" ? 0 : Math.max((max - min) * .15, 1);
    min -= pad; max += Math.max(pad, state.metric === "precipitation_mm" ? .5 : 0);
    if (max === min) max = min + 1;
    const x = t => 43 + (t - lower) / (upper - lower) * 608;
    const y = value => 184 - (value - min) / (max - min) * 156;
    let svg = '<svg class="weather-chart" viewBox="0 0 680 230" role="img" aria-label="' + escapeHTML(metric.label + " in " + (currentCity()?.city_name || "") + ", last 7 days") + '">';
    svg += "<title>" + escapeHTML(metric.label + ". Gaps indicate unavailable readings. A table is available below.") + "</title>";
    for (let i = 0; i < 4; i++) {
      const value = min + (max - min) * i / 3, pos = y(value);
      svg += '<line class="grid-line" x1="43" x2="651" y1="' + pos + '" y2="' + pos + '"/><text x="34" y="' + (pos + 3) + '" text-anchor="end">' + number(value, 0) + "</text>";
    }
    for (let i = 0; i < 7; i++) {
      const time = lower + (upper - lower) * i / 6;
      svg += '<text x="' + x(time) + '" y="216" text-anchor="middle">' + escapeHTML(dateLabel(time)) + "</text>";
    }
    if (state.metric !== "precipitation_mm") {
      for (const segment of segments) {
        const path = segment.map((point, i) => (i ? "L" : "M") + x(point.time).toFixed(2) + " " + y(point.value).toFixed(2)).join(" ");
        svg += '<path class="chart-line" d="' + path + '"/>';
      }
    }
    for (const point of points) {
      const label = escapeHTML(dateLabel(point.time, true) + " PHT: " + number(point.value) + " " + metric.unit);
      if (state.metric === "precipitation_mm") {
        svg += '<rect class="rain-bar" tabindex="0" x="' + (x(point.time) - 1.5) + '" y="' + y(point.value) + '" width="3" height="' + Math.max(1, 184 - y(point.value)) + '" aria-label="' + label + '"><title>' + label + "</title></rect>";
      } else {
        svg += '<circle class="chart-point" tabindex="0" cx="' + x(point.time) + '" cy="' + y(point.value) + '" r="2" aria-label="' + label + '"><title>' + label + "</title></circle>";
      }
    }
    $("trend-chart").innerHTML = svg + "</svg>";
    if (!$("trend-table").hidden) renderTable();
  }
  function renderTable() {
    if (state.loading || state.errors.history || !state.history?.data.length) {
      $("trend-table").textContent = "No hourly data is available.";
      return;
    }
    const metric = METRICS[state.metric];
    const rows = [...state.history.data].sort((a, b) => Date.parse(a.timestamp_ph) - Date.parse(b.timestamp_ph));
    $("trend-table").innerHTML = '<table><caption>' + escapeHTML(currentCity()?.city_name || "") + " · hourly data</caption><thead><tr><th scope=col>Time (PHT)</th><th scope=col>" + escapeHTML(metric.label + " (" + metric.unit + ")") +
      "</th></tr></thead><tbody>" + rows.map(row => "<tr><td>" + escapeHTML(dateLabel(row.timestamp_ph, true)) + "</td><td>" + number(row[state.metric]) + "</td></tr>").join("") + "</tbody></table>";
  }
  function renderComparison() {
    if (state.loading) return message("comparison-chart", "Comparing cities", "", true);
    if (state.errors.comparison) return message("comparison-chart", "Comparison unavailable", "The comparison endpoint could not be reached.");
    const rows = state.comparison?.data || [];
    const values = rows.map(row => row.avg_temperature_c).filter(validNumber);
    if (!rows.length || !values.length) return message("comparison-chart", "No comparison data yet", "Load hourly weather into PostgreSQL to compare your cities.");
    const low = Math.min(0, ...values), high = Math.max(1, ...values), scale = 145 / (high - low);
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const hasValue = validNumber(row.avg_temperature_c);
      const button = document.createElement("button");
      button.className = "city-bar" + (row.city_id === state.city ? " selected" : "");
      button.dataset.city = String(row.city_id);
      button.setAttribute("aria-label", row.city_name + ": " + (hasValue ? number(row.avg_temperature_c) + " °C average, " + row.temperature_hours + " available hours" : "no temperature data") + ". Select city.");
      button.setAttribute("aria-pressed", String(row.city_id === state.city));
      const height = hasValue ? Math.abs(row.avg_temperature_c) * scale : 0;
      const bottom = hasValue ? (Math.min(0, row.avg_temperature_c) - low) * scale : 0;
      button.innerHTML = '<span class="bar-value">' + number(row.avg_temperature_c) + (hasValue ? "°" : "") + '</span><span class="bar-track">' +
        (hasValue ? '<span class="bar-fill" style="height:' + height + "px;bottom:" + bottom + 'px"></span>' : "") +
        '</span><span class="bar-label">' + escapeHTML(row.city_name) + '</span><span class="bar-caption">' + (hasValue ? escapeHTML(row.temperature_hours) + " hrs" : "No data") + "</span>";
      fragment.append(button);
    }
    $("comparison-chart").replaceChildren(fragment);
  }
  function renderRain() {
    const days = state.history ? dailyRainfall(state.history.data) : [];
    const hours = days.reduce((total, day) => total + day.hours, 0);
    const wet = days.reduce((total, day) => total + day.wet, 0);
    const total = hours ? days.reduce((sum, day) => sum + (day.total ?? 0), 0) : null;
    $("rain-total").innerHTML = metricHTML(total, " mm");
    $("rain-hours").textContent = hours ? hours + " / 168" : "—";
    $("wet-hours").textContent = hours ? String(wet) : "—";
    $("rain-coverage").textContent = state.loading ? "Loading rainfall coverage…" : state.errors.history ?
      "Rainfall data is unavailable. Try refreshing." : !hours ? "No valid rainfall readings in this window." :
        (hours < 168 ? "Partial coverage. " : "Full 7-day coverage. ") + "Sum of available hourly precipitation, not a forecast for the week ahead.";
    if (state.loading) return message("rain-chart", "Loading rainfall", "", true);
    if (state.errors.history) return message("rain-chart", "Rainfall unavailable", "Try refreshing.");
    if (!hours) return message("rain-chart", "No rainfall readings", "Missing records are not treated as dry weather.");
    const max = Math.max(1, ...days.map(day => day.total ?? 0));
    const wrapper = document.createElement("div");
    wrapper.className = "rain-bars";
    for (const day of days) {
      const item = document.createElement("div");
      item.className = "rain-day" + (day.complete ? "" : " partial");
      item.tabIndex = 0;
      item.setAttribute("aria-label", day.date + ": " + number(day.total) + " mm, " + day.hours + " of 24 hours");
      item.title = day.hours + "/24 hours with precipitation data";
      item.innerHTML = "<strong>" + number(day.total) + '</strong><span class="rain-day-track">' +
        (day.hours ? '<i style="height:' + ((day.total ?? 0) / max * 90) + '%"></i>' : "") +
        "</span><span>" + escapeHTML(dateLabel(day.date + "T12:00:00+08:00")) + "</span>";
      wrapper.append(item);
    }
    $("rain-chart").replaceChildren(wrapper);
  }
  function renderStatus() {
    const banner = $("connection-banner");
    banner.querySelector(":scope > .spinner")?.remove();
    $("refresh-button").disabled = state.loading;
    $("retry-button").hidden = state.loading || !Object.keys(state.errors).length;
    if (state.loading) {
      banner.dataset.state = "loading";
      const spinner = document.createElement("span"); spinner.className = "spinner"; banner.prepend(spinner);
      $("connection-message").textContent = "Loading weather for " + (currentCity()?.city_name || "your cities") + "…";
      return;
    }
    const failures = Object.keys(state.errors);
    if (failures.length) {
      const allFailed = !state.latest && !state.history && !state.comparison;
      banner.dataset.state = allFailed ? "error" : "partial";
      $("connection-message").textContent = allFailed ?
        "Weather service unavailable. Check that the local API and its database are running, then try again." :
        "Some weather panels could not load. Available readings are shown; refresh to retry.";
      $("last-refresh").textContent = allFailed ? "Unable to retrieve weather" : "Partial update · " + dateLabel(Date.now(), true) + " PHT";
    } else {
      banner.dataset.state = "ready";
      $("connection-message").textContent = state.cities.length ?
        "Connected to weather data · 7-day window · Times shown in Philippine Standard Time" : "Connected · No cities are configured yet.";
      $("last-refresh").textContent = "Retrieved " + dateLabel(Date.now(), true) + " PHT";
    }
  }
  function render() {
    renderCurrent(); renderTrend(); renderComparison(); renderRain(); renderTable(); renderStatus(); setBusy();
  }
  async function loadDashboard({ reloadCities = false } = {}) {
    const sequence = ++state.sequence;
    state.controller?.abort();
    const controller = new AbortController(); state.controller = controller;
    state.loading = true; state.errors = {}; state.latest = state.history = state.comparison = null;
    render();
    if (reloadCities || !state.cities.length) {
      try {
        const response = await fetchAPI(API, "cities", { signal: controller.signal });
        if (sequence !== state.sequence) return;
        state.cities = response.data.filter(city => Number.isInteger(city.city_id) && typeof city.city_name === "string");
        if (!state.cities.some(city => city.city_id === state.city)) {
          state.city = (state.cities.find(city => city.city_name === "Manila") || state.cities[0])?.city_id ?? null;
        }
      } catch (error) {
        if (sequence !== state.sequence) return;
        state.errors.cities = error.message;
      }
    }
    if (sequence !== state.sequence) return;
    renderCities();
    if (!currentCity()) {
      state.loading = false;
      if (state.errors.cities) state.errors.latest = state.errors.history = state.errors.comparison = "City list unavailable";
      render(); return;
    }
    const selectedName = currentCity().city_name;
    const endpoints = [["latest", "weather/latest"], ["history", "weather/historical?city=" + encodeURIComponent(selectedName) + "&days=7"], ["comparison", "weather/comparison?days=7"]];
    await Promise.all(endpoints.map(async ([key, path]) => {
      try {
        const result = await fetchAPI(API, path, { signal: controller.signal });
        if (sequence === state.sequence) state[key] = result;
      } catch (error) {
        if (sequence === state.sequence) state.errors[key] = error.message;
      }
    }));
    if (sequence !== state.sequence) return;
    state.loading = false; render();
  }
  function closeMenu() {
    document.body.classList.remove("menu-open");
    $("mobile-menu").setAttribute("aria-expanded", "false");
    $("mobile-menu").setAttribute("aria-label", "Open city navigation");
  }
  document.addEventListener("click", event => {
    const cityButton = event.target.closest("[data-city]");
    if (cityButton) {
      const id = Number(cityButton.dataset.city);
      if (!state.cities.some(city => city.city_id === id)) return;
      closeMenu();
      if (id !== state.city) { state.city = id; renderCities(); loadDashboard(); }
    }
    const metricButton = event.target.closest("[data-metric]");
    if (metricButton && METRICS[metricButton.dataset.metric]) {
      state.metric = metricButton.dataset.metric;
      document.querySelectorAll("[data-metric]").forEach(button => {
        const active = button.dataset.metric === state.metric;
        button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
      });
      renderTrend(); renderTable();
    }
    const sectionLink = event.target.closest('a[href^="#"]');
    if (sectionLink) {
      closeMenu();
      document.querySelectorAll(".rail-button").forEach(link => link.classList.toggle("active", link.hash === sectionLink.hash));
    }
  });
  $("city-select").addEventListener("change", event => {
    state.city = Number(event.target.value); renderCities(); loadDashboard();
  });
  $("city-search").addEventListener("input", renderCities);
  $("refresh-button").addEventListener("click", () => loadDashboard({ reloadCities: true }));
  $("retry-button").addEventListener("click", () => loadDashboard({ reloadCities: true }));
  $("toggle-table").addEventListener("click", () => {
    const hidden = !$("trend-table").hidden;
    $("trend-table").hidden = hidden;
    $("toggle-table").textContent = hidden ? "View data ↗" : "Hide data ↑";
    $("toggle-table").setAttribute("aria-expanded", String(!hidden));
    renderTable();
  });
  $("mobile-menu").addEventListener("click", () => {
    const open = document.body.classList.toggle("menu-open");
    $("mobile-menu").setAttribute("aria-expanded", String(open));
    $("mobile-menu").setAttribute("aria-label", open ? "Close city navigation" : "Open city navigation");
    if (open) $("city-navigation").querySelector("button")?.focus();
  });
  $("menu-backdrop").addEventListener("click", closeMenu);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.body.classList.contains("menu-open")) { closeMenu(); $("mobile-menu").focus(); }
  });
  loadDashboard({ reloadCities: true });
})();
