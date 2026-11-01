/* LoseSound web UI. No framework, no build step. */
(function () {
  "use strict";

  const I18N = window.LOSESOUND_I18N || { en: {}, de: {} };
  const queryLang = (new URLSearchParams(window.location.search).get("lang") || "").toLowerCase();
  const storedLang = localStorage.getItem("losesound.lang");
  const detectedLang = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
  const state = {
    lang: (queryLang === "de" || queryLang === "en") ? queryLang : (storedLang || detectedLang),
    apiToken: localStorage.getItem("losesound.apiToken") || "",
    stations: [],
    features: { orion: false, radioBrowser: false },
    settings: {},
    setup: null,
    speakers: [],
    proxy: null,
    catalog: [],
    health: null,
    tab: "presets",
    proxyTimer: null,
    setupTimer: null,
    busy: new Set()
  };

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function esc(value) {
    return String(value === undefined || value === null ? "" : value).replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
  }

  function t(key, params) {
    const dict = I18N[state.lang] || I18N.en;
    let text = dict[key];
    if (text === undefined) text = I18N.en[key];
    if (text === undefined) text = key;
    if (typeof text === "function") text = text(params || {});
    return String(text).replace(/\{(\w+)\}/g, (match, name) => {
      if (params && params[name] !== undefined && params[name] !== null) return String(params[name]);
      return match;
    });
  }

  function modeLabel(mode) {
    const key = "mode" + String(mode || "").charAt(0).toUpperCase() + String(mode || "").slice(1);
    const label = t(key);
    return label === key ? t("modeUnknown") : label;
  }

  function fmtBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes >= 1024 * 1024 * 1024) return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
    if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
    return bytes + " B";
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) return hours + "h " + minutes + "m";
    if (minutes > 0) return minutes + "m " + secs + "s";
    return secs + "s";
  }

  function fmtTime(unixSeconds) {
    if (!unixSeconds) return "-";
    const date = new Date(Number(unixSeconds) * 1000);
    const pad = (value) => String(value).padStart(2, "0");
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function fmtAge(unixSeconds) {
    if (!unixSeconds) return t("never");
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(unixSeconds)));
    if (seconds < 90) return t("age", { seconds });
    return t("minutesAgo", { minutes: Math.floor(seconds / 60) });
  }

  function showToast(text, type) {
    const node = $("#message");
    if (!node) return;
    node.textContent = text;
    node.className = "toast" + (type ? " " + type : "");
    node.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { node.hidden = true; }, type === "error" ? 8000 : 4000);
  }

  function setHealth(kind, label) {
    const dot = $("#healthDot");
    if (dot) dot.className = "dot " + (kind || "");
    const text = $("#healthLabel");
    if (text) text.textContent = label || "";
  }

  function setToken(token) {
    state.apiToken = token || "";
    if (state.apiToken) localStorage.setItem("losesound.apiToken", state.apiToken);
    else localStorage.removeItem("losesound.apiToken");
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = {};
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (state.apiToken) headers["Authorization"] = "Bearer " + state.apiToken;
    let response;
    try {
      response = await fetch(path, {
        method: opts.method || "GET",
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        cache: "no-store"
      });
    } catch (error) {
      throw new Error(t("requestFailed", { message: error.message }));
    }
    if (response.status === 401 && opts.retry !== false) {
      const token = window.prompt(t("tokenPrompt"));
      if (token !== null) {
        setToken(token.trim());
        return api(path, Object.assign({}, opts, { retry: false }));
      }
      throw new Error(t("unauthorized"));
    }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const error = new Error((data && (data.message || data.status)) || response.statusText || "HTTP " + response.status);
      error.status = response.status;
      error.code = data && data.code;
      throw error;
    }
    return data;
  }

  // -------------------------------------------------------------- i18n / shell
  function applyStaticI18n() {
    document.documentElement.lang = state.lang;
    $$("[data-i18n]").forEach((node) => { node.textContent = t(node.getAttribute("data-i18n")); });
    $$("[data-i18n-placeholder]").forEach((node) => { node.setAttribute("placeholder", t(node.getAttribute("data-i18n-placeholder"))); });
    $$("[data-i18n-title]").forEach((node) => { node.setAttribute("title", t(node.getAttribute("data-i18n-title"))); });
    const languageButton = $("#languageBtn");
    if (languageButton) languageButton.textContent = state.lang === "de" ? "EN" : "DE";
    const footer = $("#footerText");
    if (footer) footer.textContent = t("footer", { version: (state.health && state.health.version) || "1.0.0" });
    renderModeSelectOptions();
  }

  function rebuildSelect(select, entries) {
    if (!select) return;
    const current = select.value;
    select.innerHTML = entries.map(([value, label]) => '<option value="' + esc(value) + '">' + esc(t(label)) + "</option>").join("");
    if (current && entries.some(([value]) => value === current)) select.value = current;
  }

  function populateStaticSelects() {
    rebuildSelect($("#catalogCountry"), [["", "allCountries"], ["DE", "countryGermany"], ["AT", "countryAustria"], ["CH", "countrySwitzerland"], ["GB", "countryUK"], ["US", "countryUSA"], ["FR", "countryFrance"], ["IT", "countryItaly"], ["ES", "countrySpain"], ["NL", "countryNetherlands"]]);
    rebuildSelect($("#catalogTag"), [["", "allCategories"], ["news", "catNews"], ["talk", "catTalk"], ["pop", "catPop"], ["rock", "catRock"], ["jazz", "catJazz"], ["classical", "catClassical"], ["electronic", "catElectronic"], ["80s", "cat80s"], ["90s", "cat90s"]]);
    rebuildSelect($("#catalogOrder"), [["clickcount", "orderClickcount"], ["votes", "orderVotes"], ["bitrate", "orderBitrate"], ["name", "orderName"]]);
  }

  function renderModeSelectOptions() {
    const select = $("#defaultPresetMode");
    if (!select) return;
    const current = state.settings.defaultPresetMode || select.value || "auto";
    select.innerHTML = modeOptions(current, null, true);
  }

  function catalogModeOptions() {
    const modes = ["auto", "relay", "speaker"];
    if (state.features.radioBrowser) modes.push("radioBrowser");
    if (state.features.orion) modes.push("orion");
    return modes.map((mode) => '<option value="' + esc(mode) + '">' + esc(modeLabel(mode)) + "</option>").join("");
  }

  function hostOf(url) {
    try { return new URL(url).hostname || url; } catch (error) { return url; }
  }

  function modeOptions(selected, station, includeAll) {
    const modes = ["auto", "relay", "speaker"];
    if (includeAll) {
      modes.push("radioBrowser");
      modes.push("orion");
    } else {
      if (station && station.radioBrowserUuid && state.features.radioBrowser) modes.push("radioBrowser");
      if (state.features.orion) modes.push("orion");
    }
    if (selected && modes.indexOf(selected) === -1) modes.push(selected);
    return modes.map((mode) => '<option value="' + esc(mode) + '"' + (mode === selected ? " selected" : "") + ">" + esc(modeLabel(mode)) + "</option>").join("");
  }

  // ------------------------------------------------------------------ loaders
  async function loadHealth() {
    try {
      state.health = await api("/api/health");
      const footer = $("#footerText");
      if (footer) footer.textContent = t("footer", { version: state.health.version || "1.0.0" });
      return true;
    } catch (error) {
      setHealth("error", t("offline"));
      return false;
    }
  }

  async function loadSettings() {
    try {
      state.settings = await api("/api/settings");
      const token = $("#apiToken");
      if (token) token.value = state.apiToken;
      renderModeSelectOptions();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function loadStations() {
    try {
      const data = await api("/api/stations");
      state.stations = data.stations || [];
      if (data.features) state.features = data.features;
      renderPresets();
      renderStations();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function loadSetup(refresh) {
    const query = refresh ? "?refresh=1" : "";
    try {
      state.setup = await api("/api/setup/status" + query);
      renderSetup();
    } catch (error) {
      state.setup = { findings: [{ severity: "error", code: "aftertouch_unreachable", message: error.message, hint: "" }], devices: [], aftertouch: {} };
      renderSetup();
    }
  }

  async function loadSpeakers(refresh) {
    try {
      const data = await api("/api/speakers" + (refresh ? "?refresh=1" : ""));
      state.speakers = data.devices || [];
      renderSpeakers();
    } catch (error) {
      state.speakers = [];
      renderSpeakers(error.message);
    }
  }

  async function loadProxy() {
    try {
      state.proxy = await api("/api/proxy/status");
      renderProxy();
    } catch (error) {
      state.proxy = null;
      renderProxy(error.message);
    }
  }

  async function loadAll() {
    await loadHealth();
    await loadSettings();
    await loadSetup(false);
    await loadStations();
    await loadSpeakers(false);
    if (state.tab === "status") await loadProxy();
  }

  // ------------------------------------------------------------------ render
  function renderPresets() {
    const container = $("#presetTable");
    if (!container) return;
    const header = "<table><thead><tr><th>" + esc(t("slot")) + "</th><th>" + esc(t("station")) + "</th><th>" + esc(t("streamUrl")) + "</th><th>" + esc(t("mode")) + "</th><th>" + esc(t("status")) + "</th><th>" + esc(t("actions")) + "</th></tr></thead><tbody>";
    const rows = [1, 2, 3, 4, 5, 6].map((slot) => {
      const assigned = state.stations.find((station) => Array.isArray(station.presets) && station.presets.indexOf(slot) !== -1);
      const stationOptions = state.stations.map((station) => '<option value="' + esc(station.name) + '"' + (assigned && assigned.name === station.name ? " selected" : "") + ">" + esc(station.displayName || station.name) + "</option>").join("");
      const assignedMode = assigned ? ((assigned.presetModes || {})[String(slot)] || state.settings.defaultPresetMode || "auto") : (state.settings.defaultPresetMode || "auto");
      const statusBadge = assigned
        ? '<span class="badge good">' + esc(t("assigned")) + "</span>"
        : '<span class="badge">' + esc(t("empty")) + "</span>";
      return '<tr data-slot="' + slot + '">' +
        '<td class="slot">' + slot + "</td>" +
        '<td><select id="station-select-' + slot + '" data-role="station" data-slot="' + slot + '"><option value="">-- ' + esc(t("empty")) + " --</option>" + stationOptions + "</select></td>" +
        '<td class="wrap" id="url-' + slot + '">' + (assigned ? '<a href="' + esc(assigned.dispatcherUrl) + '" target="_blank" rel="noreferrer">' + esc(assigned.dispatcherUrl) + "</a>" : "&mdash;") + "</td>" +
        '<td><select id="mode-select-' + slot + '" data-role="mode" data-slot="' + slot + '">' + modeOptions(assignedMode, assigned) + "</select></td>" +
        "<td>" + statusBadge + "</td>" +
        '<td><div class="actions">' +
          '<button type="button" class="btn primary small" data-action="preset-set" data-slot="' + slot + '">' + esc(t("set")) + "</button>" +
          (assigned ? '<button type="button" class="btn secondary small" data-action="preset-probe" data-station="' + esc(assigned.name) + '">' + esc(t("probe")) + "</button>" : "") +
          (assigned ? '<button type="button" class="btn secondary small" data-action="preset-play" data-station="' + esc(assigned.name) + '" data-slot="' + slot + '">' + esc(t("play")) + "</button>" : "") +
          (assigned ? '<button type="button" class="btn danger small" data-action="preset-remove" data-slot="' + slot + '">' + esc(t("remove")) + "</button>" : "") +
        "</div></td></tr>";
    }).join("");
    container.innerHTML = header + rows + "</tbody></table>";
  }

  function renderStations() {
    const container = $("#stationList");
    if (!container) return;
    if (!state.stations.length) {
      container.innerHTML = '<p class="hint">' + esc(t("noStations")) + "</p>";
      return;
    }
    const header = "<table><thead><tr><th>" + esc(t("artwork")) + "</th><th>" + esc(t("displayName")) + "</th><th>" + esc(t("streamUrl")) + "</th><th>" + esc(t("presetsColumn")) + "</th><th>" + esc(t("recommendation")) + "</th><th>" + esc(t("actions")) + "</th></tr></thead><tbody>";
    const rows = state.stations.map((station) => {
      const artwork = station.artwork
        ? '<img class="art" src="' + esc(station.artwork) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
        : '<span class="art-placeholder">' + esc((station.displayName || station.name || "?").slice(0, 2).toUpperCase()) + "</span>";
      const presets = (station.presets || []).map((slot) => {
        const mode = (station.presetModes || {})[String(slot)] || "";
        return '<span class="badge on">' + slot + (mode ? " · " + esc(modeLabel(mode)) : "") + "</span>";
      }).join(" ");
      const probe = station.lastProbe;
      let recommendation = '<span class="muted">' + esc(t("never")) + "</span>";
      if (probe) {
        const cls = probe.ok ? (probe.recommendation === "relay" ? "warn" : "good") : "bad";
        recommendation = '<span class="badge ' + cls + '">' + esc(modeLabel(probe.recommendation)) + "</span>" +
          '<div class="hint mono">' + esc(probe.contentType || probe.error || "-") + " · " + esc(fmtAge(probe.at)) + "</div>";
      }
      return "<tr>" +
        "<td>" + artwork + "</td>" +
        '<td><div class="strong">' + esc(station.displayName || station.name) + "</div>" +
          (station.radioBrowserUuid ? '<div class="hint mono">' + esc(station.radioBrowserUuid) + "</div>" : "") + "</td>" +
        '<td class="wrap"><a href="' + esc(station.dispatcherUrl) + '" target="_blank" rel="noreferrer">' + esc(station.dispatcherUrl) + "</a></td>" +
        "<td>" + (presets || '<span class="empty">' + esc(t("empty")) + "</span>") + "</td>" +
        "<td>" + recommendation + "</td>" +
        '<td><div class="actions">' +
          '<button type="button" class="btn secondary small" data-action="station-probe" data-station="' + esc(station.name) + '">' + esc(t("probe")) + "</button>" +
          '<button type="button" class="btn secondary small" data-action="station-play" data-station="' + esc(station.name) + '">' + esc(t("play")) + "</button>" +
          '<button type="button" class="btn danger small" data-action="station-delete" data-station="' + esc(station.name) + '" data-name="' + esc(station.displayName || station.name) + '">' + esc(t("delete")) + "</button>" +
        "</div></td></tr>";
    }).join("");
    container.innerHTML = header + rows + "</tbody></table>";
  }

  function renderCatalog() {
    const container = $("#catalogResults");
    if (!container) return;
    if (!state.catalog.length) {
      container.innerHTML = '<p class="hint">' + esc(t("catalogEmpty")) + "</p>";
      return;
    }
    const presetOptions = '<option value="">' + esc(t("selectPlaceholder")) + "</option>" + [1, 2, 3, 4, 5, 6].map((slot) => '<option value="' + slot + '">' + esc(t("preset")) + " " + slot + "</option>").join("");
    const header = "<table><thead><tr><th>" + esc(t("artwork")) + "</th><th>" + esc(t("displayName")) + "</th><th>" + esc(t("country")) + "</th><th>Tags</th><th>Codec</th><th>Bitrate</th><th>" + esc(t("actions")) + "</th></tr></thead><tbody>";
    const rows = state.catalog.map((station, index) => {
      const artwork = station.favicon
        ? '<img class="art" src="' + esc(station.favicon) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
        : '<span class="art-placeholder">' + esc((station.name || "?").slice(0, 2).toUpperCase()) + "</span>";
      const checked = Number(station.lastcheckok) === 1
        ? '<span class="badge good" title="' + esc(t("lastCheckOk")) + '">OK</span>'
        : '<span class="badge bad" title="' + esc(t("lastCheckBad")) + '">!</span>';
      return "<tr>" +
        "<td>" + artwork + "</td>" +
        '<td><div class="strong">' + esc(station.name || "-") + "</div>" +
          (station.homepage ? '<div class="hint"><a href="' + esc(station.homepage) + '" target="_blank" rel="noreferrer">' + esc(hostOf(station.homepage)) + "</a></div>" : "") + "</td>" +
        "<td>" + esc(station.countrycode || station.country || "-") + " " + checked + "</td>" +
        '<td class="wrap">' + esc(station.tags || "-") + "</td>" +
        "<td>" + esc(station.codec || "-") + "</td>" +
        "<td>" + esc(station.bitrate || "-") + "</td>" +
        '<td><div class="actions">' +
          '<a class="btn secondary small" href="' + esc(station.urlResolved || station.url) + '" target="_blank" rel="noreferrer">' + esc(t("test")) + "</a>" +
          '<button type="button" class="btn secondary small" data-action="catalog-add" data-index="' + index + '">' + esc(t("add")) + "</button>" +
          '<select class="small-select" data-role="catalog-preset" data-index="' + index + '" style="width:auto;min-width:120px">' + presetOptions + "</select>" +
          '<select data-role="catalog-mode" data-index="' + index + '" style="width:auto;min-width:130px">' + catalogModeOptions() + "</select>" +
          '<button type="button" class="btn primary small" data-action="catalog-set" data-index="' + index + '">' + esc(t("set")) + "</button>" +
        "</div></td></tr>";
    }).join("");
    container.innerHTML = header + rows + "</tbody></table>";
  }

  function renderSetup() {
    const banner = $("#setupBanner");
    if (!banner) return;
    const setup = state.setup;
    if (!setup) {
      banner.hidden = true;
      return;
    }
    const findings = setup.findings || [];
    const errors = findings.filter((item) => item.severity === "error").length;
    const warnings = findings.filter((item) => item.severity === "warning").length;
    const kind = errors ? "error" : warnings ? "warn" : "ok";
    setHealth(kind, errors ? t("degraded") : warnings ? t("degraded") : t("ready"));

    if (setup.ready && findings.length === 0) {
      banner.hidden = true;
      banner.innerHTML = "";
      setHealth("ok", t("ready"));
      return;
    }

    const aftertouch = setup.aftertouch || {};
    const version = aftertouch.version || t("unknown");
    const registry = aftertouch.registryReachable ? t("available") : t("unavailable");
    const orion = aftertouch.orion && aftertouch.orion.available ? t("available") : t("unavailable");
    const radioBrowser = aftertouch.radioBrowser && aftertouch.radioBrowser.available ? t("available") : t("unavailable");
    const summary = errors ? t("setupErrors", { count: errors }) : warnings ? t("setupWarnings", { count: warnings }) : t("setupOk");

    const findingHtml = findings.map((finding) => {
      const title = t(finding.code);
      const hint = t("hint_" + finding.code);
      const message = String(finding.message || "");
      const showDetail = title === finding.code || /https?:\/\/|HTTP \d|\.xml|\.json/.test(message);
      return '<li class="finding">' +
        '<div class="finding-title ' + esc(finding.severity) + '">' + esc(title === finding.code ? message : title) + "</div>" +
        (title !== finding.code && showDetail ? '<div class="hint">' + esc(message) + "</div>" : "") +
        '<div class="finding-hint"><span class="muted">' + esc(t("hint")) + ":</span> " + esc(hint === "hint_" + finding.code ? (finding.hint || "") : hint) + "</div>" +
        "</li>";
    }).join("");

    banner.className = "banner " + kind;
    banner.hidden = false;
    banner.innerHTML =
      '<div class="banner-head">' +
        '<div><div class="banner-title">' + esc(summary) + '</div><div class="banner-meta">' + esc(setup.aftertouchUrl || "") + "</div></div>" +
        '<div class="actions">' +
          '<button type="button" class="btn secondary small" data-action="setup-refresh">' + esc(t("refreshSetup")) + "</button>" +
          '<a class="btn secondary small" href="' + esc(setup.aftertouchUrl || "#") + '" target="_blank" rel="noreferrer">' + esc(t("openAfterTouch")) + "</a>" +
        "</div>" +
      "</div>" +
      (findings.length ? '<ul class="findings">' + findingHtml + "</ul>" : '<div class="hint">' + esc(t("setupOk")) + "</div>") +
      '<div class="facts">' +
        "<div>" + esc(t("aftertouchVersion")) + ": <b>" + esc(version) + "</b></div>" +
        "<div>" + esc(t("registry")) + ": <b>" + esc(registry) + "</b></div>" +
        "<div>" + esc(t("orion")) + ": <b>" + esc(orion) + "</b></div>" +
        "<div>" + esc(t("radioBrowser")) + ": <b>" + esc(radioBrowser) + "</b></div>" +
        "<div>" + esc(t("device")) + ": <b>" + esc((setup.speakerIps || []).join(", ") || "-") + "</b></div>" +
      "</div>";
  }

  function renderSpeakers(errorMessage) {
    const container = $("#speakerStatus");
    if (!container) return;
    if (errorMessage) {
      container.innerHTML = '<p class="error-text">' + esc(errorMessage) + "</p>";
      return;
    }
    if (!state.speakers.length) {
      container.innerHTML = '<p class="hint">' + esc(t("loading")) + "</p>";
      return;
    }
    container.innerHTML = state.speakers.map((device) => {
      const reachable = device.speakerReachable
        ? '<span class="badge good">' + esc(t("reachable")) + "</span>"
        : '<span class="badge bad">' + esc(t("unreachable")) + "</span>";
      const margeClass = device.margeReachable === null ? "" : device.margeReachable ? "good" : "bad";
      const margeBadge = device.margeReachable === null ? "" : '<span class="badge ' + margeClass + '">HTTP ' + esc(device.margeStatus || "?") + "</span>";
      const sourcesBadge = device.sourcesReady
        ? '<span class="badge good">' + esc(t("sourcesReady")) + "</span>"
        : '<span class="badge bad">' + esc(t("sourcesMissing", { sources: (device.missingSources || []).join(", ") })) + "</span>";
      const playing = device.nowPlaying || {};
      let now = '<div class="now">' + esc(t("nothingPlaying")) + "</div>";
      if (playing.stationName || playing.track) {
        now = '<div class="now">' + esc(t("nowPlaying")) + ': <b>' + esc(playing.stationName || playing.track) + "</b>" +
          (playing.playStatus ? " · " + esc(playing.playStatus) : "") +
          (playing.source ? " · " + esc(playing.source) : "") + "</div>";
      }
      return '<div class="speaker-card">' +
        '<div class="speaker-head"><div><div class="speaker-name">' + esc(device.name || t("device")) + "</div>" +
          '<div class="hint mono">' + esc(device.speakerIp || "") + "</div></div><div class=\"actions\">" + reachable + sourcesBadge + "</div></div>" +
        '<div class="kv">' +
          "<div>" + esc(t("firmware")) + ": <b>" + esc(device.firmware || "-") + "</b></div>" +
          "<div>" + esc(t("marge")) + ": <b class=\"mono\">" + esc(device.currentMargeUrl || "-") + "</b> " + margeBadge + "</div>" +
        "</div>" + now +
        (device.missingSources && device.missingSources.length ? '<div class="hint">' + esc(t("sourcesMissing", { sources: device.missingSources.join(", ") })) + "</div>" : "") +
        "</div>";
    }).join("");
  }

  function renderProxy(errorMessage) {
    const container = $("#proxyStatus");
    if (!container) return;
    if (errorMessage) {
      container.innerHTML = '<p class="error-text">' + esc(errorMessage) + "</p>";
      return;
    }
    if (!state.proxy) {
      container.innerHTML = '<p class="hint">' + esc(t("loading")) + "</p>";
      return;
    }
    const active = state.proxy.activeStreams || [];
    const recent = state.proxy.stationStats || [];
    const reconnects = active.concat(recent).reduce((sum, item) => sum + (Number(item.reconnects) || 0), 0);
    const errors = recent.filter((item) => item.lastError).length;
    const sent = recent.reduce((sum, item) => sum + (Number(item.bytesOut) || 0), 0);

    const summary = '<div class="grid-cards">' +
      statCard(t("activeStreams"), active.length) +
      statCard(t("reconnects"), reconnects) +
      statCard(t("errors"), errors) +
      statCard(t("sent"), fmtBytes(sent)) +
      "</div>";

    const activeTable = active.length ? proxyTable(active, true) : '<p class="hint">' + esc(t("noActive")) + "</p>";
    const recentTable = recent.length ? proxyTable(recent, false) : '<p class="hint">' + esc(t("noRecent")) + "</p>";
    container.innerHTML = summary + "<h3>" + esc(t("current")) + "</h3>" + activeTable + "<h3>" + esc(t("recent")) + "</h3>" + recentTable;
  }

  function statCard(label, value) {
    return '<div class="stat"><div class="stat-label">' + esc(label) + '</div><div class="stat-value">' + esc(value) + "</div></div>";
  }

  function proxyTable(items, active) {
    const header = "<table><thead><tr><th>" + esc(t("station")) + "</th><th>" + esc(t("state")) + "</th><th>" +
      (active ? esc(t("duration")) : esc(t("started"))) + "</th><th>" + esc(t("bytes")) + "</th><th>" + esc(t("buffer")) + "</th><th>" + esc(t("cause")) + "</th></tr></thead><tbody>";
    const rows = items.map((item) => {
      const stateClass = item.state === "streaming" ? "good" : (item.state === "buffer-underrun" || item.state === "error" || item.state === "client-disconnect") ? "bad" : "warn";
      const buffer = (item.bufferFillPercent !== undefined ? Number(item.bufferFillPercent).toFixed(1) + "%" : "-") +
        " · " + fmtBytes(item.bufferedBytes || 0) +
        " · " + (item.underrunCount || 0) + " " + esc(t("underruns"));
      const cause = item.lastEventMessage || item.lastError || "-";
      const when = active ? fmtDuration(item.durationSeconds || 0) : fmtTime(item.startedAt) + " → " + fmtTime(item.endedAt);
      return "<tr>" +
        "<td>" + esc(item.station || "-") + "</td>" +
        '<td><span class="badge ' + stateClass + '">' + esc(item.state || "-") + "</span></td>" +
        "<td>" + esc(when) + "</td>" +
        "<td>" + esc(fmtBytes(item.bytesIn || 0)) + " " + esc(t("bytesIn")) + "<br>" + esc(fmtBytes(item.bytesOut || 0)) + " " + esc(t("bytesOut")) + "</td>" +
        "<td>" + esc(buffer) + "</td>" +
        '<td class="wrap">' + esc(cause) + "</td></tr>";
    }).join("");
    return header + rows + "</tbody></table>";
  }

  function renderAll() {
    applyStaticI18n();
    populateStaticSelects();
    renderPresets();
    renderStations();
    renderCatalog();
    renderSetup();
    renderSpeakers();
    renderProxy();
  }

  // ------------------------------------------------------------------ actions
  async function setPreset(slot) {
    const stationSelect = $("#station-select-" + slot);
    const modeSelect = $("#mode-select-" + slot);
    const stationName = stationSelect ? stationSelect.value : "";
    if (!stationName) {
      showToast(t("selectStationFirst"), "error");
      return;
    }
    const mode = modeSelect ? modeSelect.value : "auto";
    try {
      const result = await api("/api/stations/" + encodeURIComponent(stationName) + "/preset/" + slot, { method: "POST", body: { mode } });
      const station = state.stations.find((item) => item.name === stationName);
      const label = (station && (station.displayName || station.name)) || stationName;
      showToast(t("presetAssigned", { slot, station: label }) + " · " + modeLabel((result && result.mode) || mode), "success");
      await loadStations();
      await loadSetup(false);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function removePreset(slot) {
    if (!window.confirm(t("confirmRemovePreset", { slot }))) return;
    try {
      await api("/api/presets/" + slot + "/remove", { method: "POST" });
      showToast(t("presetRemoved", { slot }), "success");
      await loadStations();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function probeStation(name) {
    try {
      const data = await api("/api/stations/" + encodeURIComponent(name) + "/probe", { method: "POST" });
      const probe = data.probe || {};
      if (probe.ok) {
        showToast(t("probeResult", { status: probe.status || "-", contentType: probe.contentType || "-", recommendation: modeLabel(probe.recommendation) }), "success");
      } else {
        showToast(t("probeFailed", { message: probe.error || "?" }), "error");
      }
      await loadStations();
      await loadSetup(false);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function playStation(name, mode, slot) {
    try {
      const body = { mode: mode || "auto" };
      if (slot) body.slot = Number(slot);
      const data = await api("/api/stations/" + encodeURIComponent(name) + "/play", { method: "POST", body });
      if (data.status === "ok") {
        showToast(t("playStarted", { station: name, mode: modeLabel(data.mode) }), "success");
      } else {
        showToast(t("playFailed", { message: data.message || t("playTimeout") }), "error");
      }
      await loadSpeakers(true);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function deleteStation(name, displayName) {
    if (!window.confirm(t("confirmDelete", { station: displayName || name }) + "\n\n" + t("confirmDeleteHint"))) return;
    try {
      await api("/api/stations/" + encodeURIComponent(name) + "?removePresets=1", { method: "DELETE" });
      showToast(t("stationDeleted", { station: displayName || name }), "success");
      await loadStations();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function searchCatalog(event) {
    if (event) event.preventDefault();
    const params = new URLSearchParams();
    const query = $("#catalogQuery") ? $("#catalogQuery").value.trim() : "";
    if (query) params.set("name", query);
    const country = $("#catalogCountry") ? $("#catalogCountry").value : "";
    if (country) params.set("countrycode", country);
    const tag = $("#catalogTag") ? $("#catalogTag").value : "";
    if (tag) params.set("tag", tag);
    const order = $("#catalogOrder") ? $("#catalogOrder").value : "";
    if (order) params.set("order", order);
    params.set("limit", "40");
    const container = $("#catalogResults");
    if (container) container.innerHTML = '<p class="hint">' + esc(t("catalogSearching")) + "</p>";
    try {
      const data = await api("/api/catalog/search?" + params.toString());
      if (data.status !== "ok") {
        state.catalog = [];
        showToast(t("catalogError", { message: data.message || "?" }), "error");
      } else {
        state.catalog = data.stations || [];
      }
      renderCatalog();
    } catch (error) {
      state.catalog = [];
      renderCatalog();
      showToast(error.message, "error");
    }
  }

  function catalogPayload(index) {
    const station = state.catalog[index];
    if (!station) return null;
    return {
      displayName: station.name,
      dispatcherUrl: station.urlResolved || station.url,
      radioBrowserUuid: station.stationuuid,
      artwork: station.favicon
    };
  }

  async function addCatalog(index) {
    const payload = catalogPayload(index);
    if (!payload) return;
    try {
      const data = await api("/api/catalog/add", { method: "POST", body: payload });
      if (data.status === "ok") {
        showToast(t("stationAdded", { station: payload.displayName }), "success");
        await loadStations();
      } else {
        showToast(data.message, "error");
      }
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function setCatalogPreset(index) {
    const payload = catalogPayload(index);
    if (!payload) return;
    const presetSelect = document.querySelector('[data-role="catalog-preset"][data-index="' + index + '"]');
    const modeSelect = document.querySelector('[data-role="catalog-mode"][data-index="' + index + '"]');
    const slot = parseInt(presetSelect && presetSelect.value, 10);
    if (!slot) {
      showToast(t("selectPresetFirst"), "error");
      return;
    }
    payload.preset = slot;
    payload.mode = modeSelect ? modeSelect.value : "auto";
    try {
      const data = await api("/api/catalog/add", { method: "POST", body: payload });
      if (data.status === "ok") {
        showToast(t("stationSet", { station: payload.displayName, slot }), "success");
        await loadStations();
      } else {
        showToast(data.message, "error");
      }
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  // ----------------------------------------------------------------- settings
  function openSettings() {
    const panel = $("#settingsPanel");
    const backdrop = $("#settingsBackdrop");
    if (panel) panel.hidden = false;
    if (backdrop) backdrop.hidden = false;
    if ($("#bufferSeconds")) $("#bufferSeconds").value = state.settings.bufferSeconds || 3;
    if ($("#prebufferSeconds")) $("#prebufferSeconds").value = state.settings.prebufferSeconds || 0;
    if ($("#bitrateKbps")) $("#bitrateKbps").value = state.settings.bitrateKbps || 128;
    if ($("#reconnectBackoffMs")) $("#reconnectBackoffMs").value = (state.settings.reconnectBackoffMs || [250, 500, 1000, 2000, 3000]).join(",");
    if ($("#apiToken")) $("#apiToken").value = state.apiToken;
    renderModeSelectOptions();
  }

  function closeSettings() {
    const panel = $("#settingsPanel");
    const backdrop = $("#settingsBackdrop");
    if (panel) panel.hidden = true;
    if (backdrop) backdrop.hidden = true;
  }

  async function saveSettings(event) {
    if (event) event.preventDefault();
    const bufferSeconds = parseInt($("#bufferSeconds").value, 10);
    const prebufferSeconds = parseInt($("#prebufferSeconds").value, 10);
    const bitrateKbps = parseInt($("#bitrateKbps").value, 10);
    const backoff = $("#reconnectBackoffMs").value.split(",").map((value) => parseInt(value.trim(), 10)).filter((value) => Number.isFinite(value));
    if (!Number.isFinite(bufferSeconds) || !Number.isFinite(prebufferSeconds) || !Number.isFinite(bitrateKbps) || !backoff.length) {
      showToast(t("invalidSettings"), "error");
      return;
    }
    const tokenField = $("#apiToken");
    setToken(tokenField ? tokenField.value.trim() : state.apiToken);
    try {
      const data = await api("/api/settings", {
        method: "POST",
        body: { bufferSeconds, prebufferSeconds, bitrateKbps, reconnectBackoffMs: backoff, defaultPresetMode: $("#defaultPresetMode").value, audioProxyEnabled: true, apiToken: undefined }
      });
      state.settings = data.settings || state.settings;
      showToast(t("settingsSaved"), "success");
      closeSettings();
      renderPresets();
      renderModeSelectOptions();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  // --------------------------------------------------------------------- tabs
  function showTab(name, updateHash) {
    state.tab = name;
    if (updateHash !== false) {
      const hash = "#" + name;
      if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
    }
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.getAttribute("data-tab") === name));
    $$(".view").forEach((view) => view.classList.toggle("active", view.id === "view-" + name));
    if (name === "status") {
      loadProxy();
      loadSpeakers(false);
      loadSetup(false);
      if (!state.proxyTimer) state.proxyTimer = window.setInterval(loadProxy, 3000);
    } else if (state.proxyTimer) {
      window.clearInterval(state.proxyTimer);
      state.proxyTimer = null;
    }
  }

  // -------------------------------------------------------------------- init
  function bindEvents() {
    document.addEventListener("click", async (event) => {
      const target = event.target.closest("[data-action]");
      if (target) {
        const action = target.getAttribute("data-action");
        const station = target.getAttribute("data-station");
        const slot = target.getAttribute("data-slot");
        const index = parseInt(target.getAttribute("data-index"), 10);
        if (action === "preset-set") await setPreset(parseInt(slot, 10));
        else if (action === "preset-remove") await removePreset(parseInt(slot, 10));
        else if (action === "preset-probe" || action === "station-probe") await probeStation(station);
        else if (action === "preset-play") await playStation(station, ($("#mode-select-" + slot) || {}).value, slot);
        else if (action === "station-play") await playStation(station, "auto");
        else if (action === "station-delete") await deleteStation(station, target.getAttribute("data-name"));
        else if (action === "catalog-add") await addCatalog(index);
        else if (action === "catalog-set") await setCatalogPreset(index);
        else if (action === "setup-refresh") { await loadSetup(true); await loadStations(); }
        else if (action === "tab") showTab(target.getAttribute("data-tab"));
        return;
      }
      const tab = event.target.closest("[data-tab]");
      if (tab) showTab(tab.getAttribute("data-tab"));
    });

    const presetTable = $("#presetTable");
    if (presetTable) {
      presetTable.addEventListener("change", (event) => {
        const select = event.target;
        if (!select || select.getAttribute("data-role") !== "station") return;
        const slot = select.getAttribute("data-slot");
        const station = state.stations.find((item) => item.name === select.value);
        const modeSelect = $("#mode-select-" + slot);
        const previousMode = modeSelect ? modeSelect.value : "auto";
        if (modeSelect) modeSelect.innerHTML = modeOptions(previousMode, station || null);
        const urlCell = $("#url-" + slot);
        if (urlCell) {
          urlCell.innerHTML = station
            ? '<a href="' + esc(station.dispatcherUrl) + '" target="_blank" rel="noreferrer">' + esc(station.dispatcherUrl) + "</a>"
            : "&mdash;";
        }
      });
    }

    const addForm = $("#addStationForm");
    if (addForm) {
      addForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const displayName = $("#newStationDisplay").value.trim();
        const url = $("#newStationUrl").value.trim();
        if (!displayName || !url) {
          showToast(t("invalidSettings"), "error");
          return;
        }
        try {
          const data = await api("/api/stations", { method: "POST", body: { displayName, dispatcherUrl: url } });
          if (data.status === "ok") {
            showToast(t("stationAdded", { station: displayName }), "success");
            $("#newStationDisplay").value = "";
            $("#newStationUrl").value = "";
            await loadStations();
          }
        } catch (error) {
          showToast(error.message, "error");
        }
      });
    }

    const catalogForm = $("#catalogForm");
    if (catalogForm) catalogForm.addEventListener("submit", searchCatalog);

    const settingsForm = $("#settingsForm");
    if (settingsForm) settingsForm.addEventListener("submit", saveSettings);

    const languageButton = $("#languageBtn");
    if (languageButton) languageButton.addEventListener("click", () => {
      state.lang = state.lang === "de" ? "en" : "de";
      localStorage.setItem("losesound.lang", state.lang);
      applyStaticI18n();
      renderAll();
    });

    const settingsButton = $("#settingsBtn");
    if (settingsButton) settingsButton.addEventListener("click", openSettings);
    const settingsClose = $("#settingsClose");
    if (settingsClose) settingsClose.addEventListener("click", closeSettings);
    const backdrop = $("#settingsBackdrop");
    if (backdrop) backdrop.addEventListener("click", closeSettings);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSettings();
    });
  }

  async function init() {
    bindEvents();
    populateStaticSelects();
    applyStaticI18n();
    renderPresets();
    renderStations();
    renderCatalog();
    renderProxy();
    const initialTab = (window.location.hash || "").replace("#", "");
    if (["presets", "catalog", "status"].indexOf(initialTab) !== -1) showTab(initialTab, false);
    await loadAll();
    if (state.tab === "status") await loadProxy();
    state.setupTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") loadSetup(false);
    }, 30000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
