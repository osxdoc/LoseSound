/* LoseSound web UI. No framework, no build step. */
(function () {
  "use strict";

  const I18N = window.LOSESOUND_I18N || { en: {}, de: {} };
  const queryLang = (new URLSearchParams(window.location.search).get("lang") || "").toLowerCase();
  const storedLang = localStorage.getItem("losesound.lang");
  const detectedLang = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
  const reduceMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  const state = {
    lang: (queryLang === "de" || queryLang === "en") ? queryLang : (storedLang || detectedLang),
    apiToken: localStorage.getItem("losesound.apiToken") || "",
    stations: [],
    stationsReady: false,
    features: { orion: false, radioBrowser: false },
    settings: {},
    setup: null,
    speakers: [],
    speakersLoaded: false,
    proxy: null,
    catalog: [],
    catalogBusy: false,
    catalogSearched: false,
    health: null,
    tab: "presets",
    editSlot: null,
    deckBooted: false,
    proxyTimer: null,
    setupTimer: null,
    speakersTimer: null
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

  function capitalize(value) {
    return String(value || "").charAt(0).toUpperCase() + String(value || "").slice(1);
  }

  function modeLabel(mode) {
    const key = "mode" + capitalize(mode);
    const label = t(key);
    return label === key ? t("modeUnknown") : label;
  }

  function modeShort(mode) {
    const key = "modeShort" + capitalize(mode);
    const label = t(key);
    return label === key ? modeLabel(mode) : label;
  }

  function modeHelp(mode) {
    const key = "mode" + capitalize(mode) + "Help";
    const text = t(key);
    return text === key ? "" : text;
  }

  function playStateLabel(status) {
    const value = String(status || "").toUpperCase();
    if (value.indexOf("PLAY") !== -1) return t("playing");
    if (value.indexOf("PAUSE") !== -1) return t("paused");
    if (value.indexOf("STOP") !== -1) return t("stopped");
    return "";
  }

  const SOURCE_KEYS = {
    ORION: "sourceOrion",
    RADIO_BROWSER: "sourceRadioBrowser",
    INTERNET_RADIO: "sourceInternetRadio",
    TUNEIN: "sourceTunein"
  };

  function sourceLabel(source) {
    const value = String(source || "");
    if (!value) return "";
    const key = SOURCE_KEYS[value.toUpperCase()];
    if (!key) return value;
    const label = t(key);
    return label === key ? value : label;
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

  function initials(name) {
    const text = String(name || "?").trim();
    const words = text.split(/\s+/).filter(Boolean);
    if (words.length > 1) return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
    return text.slice(0, 2).toUpperCase();
  }

  function artBlock(src, name) {
    if (src) {
      return '<img class="art" src="' + esc(src) + '" alt="" loading="lazy" referrerpolicy="no-referrer">';
    }
    return '<span class="art-placeholder" aria-hidden="true">' + esc(initials(name)) + "</span>";
  }

  function hostOf(url) {
    try { return new URL(url).hostname || url; } catch (error) { return url; }
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
    if (dot) dot.className = "led " + (kind || "");
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

  // -------------------------------------------------------------- model
  function stationForSlot(slot) {
    return state.stations.find((station) => Array.isArray(station.presets) && station.presets.map(Number).indexOf(Number(slot)) !== -1) || null;
  }

  function slotMode(station, slot) {
    if (!station) return state.settings.defaultPresetMode || "auto";
    const modes = station.presetModes || {};
    return modes[String(slot)] || state.settings.defaultPresetMode || "auto";
  }

  function playingSlots() {
    const slots = new Set();
    const device = state.speakers[0];
    const playing = device && device.nowPlaying;
    if (!playing || playing.error) return slots;
    const name = playing.stationName || playing.itemName || playing.track || "";
    const normalize = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
    const target = normalize(name);
    if (!target) return slots;
    const exact = [];
    const partial = [];
    state.stations.forEach((station) => {
      if (!Array.isArray(station.presets) || !station.presets.length) return;
      const candidates = [station.name, station.displayName].filter(Boolean).map(normalize).filter(Boolean);
      if (candidates.indexOf(target) !== -1) {
        exact.push(station);
      } else if (candidates.some((candidate) => candidate.length >= 4 && (target.indexOf(candidate) !== -1 || candidate.indexOf(target) !== -1))) {
        partial.push(station);
      }
    });
    (exact.length ? exact : partial).forEach((station) => station.presets.forEach((slot) => slots.add(Number(slot))));
    return slots;
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

  function catalogModeOptions() {
    const modes = ["auto", "relay", "speaker"];
    if (state.features.radioBrowser) modes.push("radioBrowser");
    if (state.features.orion) modes.push("orion");
    return modes.map((mode) => '<option value="' + esc(mode) + '">' + esc(modeLabel(mode)) + "</option>").join("");
  }

  // -------------------------------------------------------------- i18n / shell
  function applyStaticI18n() {
    document.documentElement.lang = state.lang;
    $$("[data-i18n]").forEach((node) => { node.textContent = t(node.getAttribute("data-i18n")); });
    $$("[data-i18n-placeholder]").forEach((node) => { node.setAttribute("placeholder", t(node.getAttribute("data-i18n-placeholder"))); });
    $$("[data-i18n-title]").forEach((node) => { node.setAttribute("title", t(node.getAttribute("data-i18n-title"))); });
    $$("[data-i18n-aria]").forEach((node) => { node.setAttribute("aria-label", t(node.getAttribute("data-i18n-aria"))); });
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
      state.stationsReady = true;
      renderPresets();
      renderStations();
    } catch (error) {
      state.stationsReady = true;
      renderStations();
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
      state.speakersLoaded = true;
      renderSpeakers();
    } catch (error) {
      state.speakers = [];
      state.speakersLoaded = true;
      renderSpeakers(error.message);
    }
    renderDisplay();
    syncPlayingState();
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

  // ------------------------------------------------------------------ display
  function renderDisplay() {
    const display = $(".display");
    const title = $("#displayTitle");
    const sub = $("#displaySub");
    const speakerName = $("#displaySpeaker");
    const speakerIp = $("#displayIp");
    if (!display || !title || !sub) return;
    const device = state.speakers[0];
    if (!device) {
      display.classList.add("is-idle");
      title.textContent = state.speakersLoaded ? t("speakerUnavailable") : t("loading");
      sub.textContent = "";
      if (speakerName) speakerName.textContent = "";
      if (speakerIp) speakerIp.textContent = "";
      return;
    }
    if (speakerName) speakerName.textContent = device.name || t("device");
    if (speakerIp) speakerIp.textContent = device.speakerIp || "";
    if (device.speakerReachable === false) {
      display.classList.add("is-idle");
      title.textContent = t("speakerUnavailable");
      sub.textContent = "";
      return;
    }
    const playing = device.nowPlaying;
    if (!playing || playing.error || (!playing.stationName && !playing.track && !playing.itemName)) {
      display.classList.add("is-idle");
      title.textContent = t("nothingPlaying");
      sub.textContent = (playing && playing.error) ? String(playing.error) : "";
      return;
    }
    display.classList.remove("is-idle");
    title.textContent = playing.stationName || playing.track || playing.itemName || t("nowPlaying");
    const parts = [];
    if (playing.artist) parts.push(playing.artist);
    if (playing.album) parts.push(playing.album);
    const playState = playStateLabel(playing.playStatus);
    if (playState) parts.push(playState);
    if (playing.source) parts.push(sourceLabel(playing.source));
    sub.textContent = parts.join(" · ");
  }

  // -------------------------------------------------------------------- deck
  function renderDeck() {
    const deck = $("#presetDeck");
    if (!deck) return;
    const boot = state.stationsReady && !state.deckBooted;
    if (boot) state.deckBooted = true;
    const playing = playingSlots();
    deck.className = "deck" + (boot ? " boot" : "");
    deck.setAttribute("role", "group");
    deck.setAttribute("aria-label", t("presetsTitle"));
    deck.innerHTML = [1, 2, 3, 4, 5, 6].map((slot) => {
      const station = stationForSlot(slot);
      const editing = Number(state.editSlot) === Number(slot);
      if (!station) {
        return '<div class="preset-key is-empty' + (editing ? " editing" : "") + '" data-slot="' + slot + '">' +
          '<button type="button" class="key-face" data-action="preset-edit" data-slot="' + slot + '" aria-label="' + esc(t("presetKeyEdit", { slot })) + '" title="' + esc(t("presetKeyEdit", { slot })) + '" aria-expanded="' + (editing ? "true" : "false") + '" aria-controls="presetEditor">' +
            '<span class="key-head"><span class="key-num">' + slot + '</span><span class="key-led" aria-hidden="true"></span></span>' +
            '<span class="key-empty"><span class="plus" aria-hidden="true">+</span><span>' + esc(t("emptyKeyHint")) + "</span></span>" +
          "</button></div>";
      }
      const name = station.displayName || station.name;
      const mode = slotMode(station, slot);
      const isPlaying = playing.has(Number(slot));
      return '<div class="preset-key assigned' + (isPlaying ? " is-playing" : "") + (editing ? " editing" : "") + '" data-slot="' + slot + '">' +
        '<button type="button" class="key-face" data-action="preset-play" data-slot="' + slot + '" data-station="' + esc(station.name) + '"' +
          (isPlaying ? ' aria-current="true"' : "") +
          ' aria-label="' + esc(t("presetKeyPlay", { slot, station: name })) + '" title="' + esc(t("presetKeyPlay", { slot, station: name })) + '">' +
          '<span class="key-head"><span class="key-num">' + slot + '</span><span class="key-led" aria-hidden="true"></span></span>' +
          '<span class="key-mid">' + artBlock(station.artwork, name) +
            '<span class="key-text"><span class="key-name">' + esc(name) + "</span></span>" +
          "</span>" +
          '<span class="key-foot">' +
            (isPlaying ? '<span class="key-state">' + esc(t("playing")) + "</span>" : "") +
            '<span class="key-mode">' + esc(modeShort(mode)) + "</span>" +
          "</span>" +
        "</button>" +
        '<button type="button" class="key-edit" data-action="preset-edit" data-slot="' + slot + '" aria-label="' + esc(t("presetKeyEdit", { slot })) + '" title="' + esc(t("edit")) + '" aria-expanded="' + (editing ? "true" : "false") + '" aria-controls="presetEditor">' +
          '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">' +
            '<path d="M3.2 12.8l.5-2.6 7-7a1.4 1.4 0 0 1 2 2l-7 7-2.5.6Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>' +
            '<path d="M9.9 4.1l2 2" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
          "</svg>" +
        "</button>" +
      "</div>";
    }).join("");
  }

  function renderEditor() {
    const box = $("#presetEditor");
    if (!box) return;
    const slot = state.editSlot;
    if (!slot) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const station = stationForSlot(slot);
    const mode = slotMode(station, slot);
    const stationOptions = '<option value="">-- ' + esc(t("empty")) + " --</option>" + state.stations.map((item) =>
      '<option value="' + esc(item.name) + '"' + (station && station.name === item.name ? " selected" : "") + ">" + esc(item.displayName || item.name) + "</option>"
    ).join("");
    const name = station ? (station.displayName || station.name) : t("emptyPreset");
    const url = station
      ? '<a href="' + esc(station.dispatcherUrl) + '" target="_blank" rel="noreferrer">' + esc(station.dispatcherUrl) + "</a>"
      : "&mdash;";
    const head =
      '<div class="editor-head">' +
        '<div><span class="editor-kicker">' + esc(t("presetEditorTitle", { slot })) + '</span>' +
          '<span class="editor-station">' + esc(name) + "</span></div>" +
        '<button class="quiet-btn" type="button" data-action="preset-edit-close" aria-label="' + esc(t("close")) + '">&#10005;</button>' +
      "</div>";
    box.hidden = false;
    box.setAttribute("aria-label", t("presetEditorTitle", { slot }));
    if (!state.stations.length) {
      box.innerHTML = head +
        '<div class="editor-empty"><p>' + esc(t("noStations")) + "</p>" +
          '<button type="button" class="btn secondary small" data-tab="catalog">' + esc(t("tabCatalog")) + "</button></div>";
      return;
    }
    const actions = station
      ? '<div class="actions">' +
          '<button type="button" class="btn ghost small" data-action="preset-probe" data-station="' + esc(station.name) + '">' + esc(t("probe")) + "</button>" +
          '<button type="button" class="btn ghost small" data-action="preset-play" data-slot="' + slot + '" data-station="' + esc(station.name) + '">' + esc(t("play")) + "</button>" +
          '<button type="button" class="btn ghost small danger" data-action="preset-remove" data-slot="' + slot + '">' + esc(t("remove")) + "</button>" +
        "</div>"
      : "";
    box.innerHTML =
      head +
      '<div class="editor-row">' +
        '<label class="field"><span>' + esc(t("station")) + '</span><select id="station-select-' + slot + '" data-role="station" data-slot="' + slot + '">' + stationOptions + "</select></label>" +
        '<label class="field"><span>' + esc(t("mode")) + '</span><select id="mode-select-' + slot + '" data-role="mode" data-slot="' + slot + '">' + modeOptions(mode, station) + "</select></label>" +
        '<button type="button" class="btn primary" data-action="preset-set" data-slot="' + slot + '">' + esc(t("set")) + "</button>" +
      "</div>" +
      '<p class="hint" id="mode-help-' + slot + '">' + esc(modeHelp(mode)) + "</p>" +
      '<div class="editor-foot">' +
        '<span class="editor-url" id="url-' + slot + '">' + url + "</span>" +
        actions +
      "</div>";
  }

  function renderPresets() {
    renderDeck();
    renderEditor();
  }

  function syncPlayingState() {
    const deck = $("#presetDeck");
    if (!deck) return;
    const playing = playingSlots();
    $$(".preset-key", deck).forEach((key) => {
      const slot = Number(key.getAttribute("data-slot"));
      const isPlaying = playing.has(slot);
      key.classList.toggle("is-playing", isPlaying);
      const face = $(".key-face", key);
      if (face) {
        if (isPlaying) face.setAttribute("aria-current", "true");
        else face.removeAttribute("aria-current");
      }
      const foot = $(".key-foot", key);
      if (!foot) return;
      let stateEl = $(".key-state", foot);
      if (isPlaying) {
        if (!stateEl) {
          stateEl = document.createElement("span");
          stateEl.className = "key-state";
          foot.insertBefore(stateEl, foot.firstChild);
        }
        stateEl.textContent = t("playing");
      } else if (stateEl) {
        stateEl.remove();
      }
    });
  }

  function openEditor(slot) {
    state.editSlot = Number(slot);
    renderEditor();
    renderDeck();
    const select = $("#station-select-" + slot);
    const target = select || $("#presetEditor .editor-empty .btn") || $("#presetEditor .quiet-btn");
    if (target) {
      window.requestAnimationFrame(() => {
        target.focus({ preventScroll: true });
        const box = $("#presetEditor");
        if (box && box.scrollIntoView) box.scrollIntoView({ block: "nearest", behavior: reduceMotion.matches ? "auto" : "smooth" });
      });
    }
  }

  function closeEditor() {
    const slot = state.editSlot;
    state.editSlot = null;
    renderEditor();
    renderDeck();
    if (!slot) return;
    const trigger = document.querySelector('.preset-key[data-slot="' + slot + '"] .key-edit') ||
      document.querySelector('.preset-key[data-slot="' + slot + '"] .key-face');
    if (trigger) trigger.focus({ preventScroll: true });
  }

  // ----------------------------------------------------------------- library
  function renderStations() {
    const container = $("#stationList");
    if (!container) return;
    if (!state.stationsReady) {
      container.innerHTML = '<p class="hint">' + esc(t("loading")) + "</p>";
      return;
    }
    if (!state.stations.length) {
      container.innerHTML =
        '<div class="empty-state"><p>' + esc(t("noStations")) + "</p>" +
        '<button type="button" class="btn secondary small" data-tab="catalog">' + esc(t("tabCatalog")) + "</button></div>";
      return;
    }
    const header = '<div class="table-wrap"><table><thead><tr>' +
      "<th>" + esc(t("station")) + "</th>" +
      "<th>" + esc(t("presetsColumn")) + "</th>" +
      "<th>" + esc(t("recommendation")) + "</th>" +
      "<th>" + esc(t("actions")) + "</th>" +
      "</tr></thead><tbody>";
    const rows = state.stations.map((station) => {
      const name = station.displayName || station.name;
      const presets = (station.presets || []).map((slot) => {
        const mode = (station.presetModes || {})[String(slot)] || "";
        return '<span class="chip" title="' + esc(mode ? modeLabel(mode) : t("modeLabel")) + '">' + esc(slot) + "</span>";
      }).join("");
      const probe = station.lastProbe;
      let recommendation = '<span class="empty">' + esc(t("never")) + "</span>";
      if (probe) {
        const kind = probe.ok ? (probe.recommendation === "relay" ? "warn" : "good") : "bad";
        recommendation = '<span class="status ' + kind + '">' + esc(modeLabel(probe.recommendation)) + "</span>" +
          '<div class="status-meta">' + esc(probe.contentType || probe.error || "-") + " · " + esc(fmtAge(probe.at)) + "</div>";
      }
      return "<tr>" +
        '<td><div class="station-cell">' + artBlock(station.artwork, name) +
          '<div class="station-text"><div class="station-name">' + esc(name) + "</div>" +
            (station.radioBrowserUuid ? '<span class="station-url">' + esc(station.radioBrowserUuid) + "</span>" : "") +
            '<span class="station-url"><a href="' + esc(station.dispatcherUrl) + '" target="_blank" rel="noreferrer">' + esc(station.dispatcherUrl) + "</a></span>" +
          "</div></div></td>" +
        '<td data-label="' + esc(t("presetsColumn")) + '">' + (presets ? '<div class="chips">' + presets + "</div>" : '<span class="empty">' + esc(t("empty")) + "</span>") + "</td>" +
        '<td data-label="' + esc(t("recommendation")) + '">' + recommendation + "</td>" +
        '<td class="cell-actions" data-label="' + esc(t("actions")) + '"><div class="actions">' +
          '<button type="button" class="btn ghost small" data-action="station-probe" data-station="' + esc(station.name) + '">' + esc(t("probe")) + "</button>" +
          '<button type="button" class="btn ghost small" data-action="station-play" data-station="' + esc(station.name) + '">' + esc(t("play")) + "</button>" +
          '<button type="button" class="btn ghost small danger" data-action="station-delete" data-station="' + esc(station.name) + '" data-name="' + esc(name) + '">' + esc(t("delete")) + "</button>" +
        "</div></td></tr>";
    }).join("");
    container.innerHTML = header + rows + "</tbody></table></div>";
  }

  function renderCatalog() {
    const container = $("#catalogResults");
    if (!container) return;
    if (state.catalogBusy) {
      container.innerHTML = '<p class="hint">' + esc(t("catalogSearching")) + "</p>";
      return;
    }
    if (!state.catalog.length) {
      container.innerHTML = '<div class="empty-state"><p>' + esc(state.catalogSearched ? t("catalogNoResults") : t("catalogEmpty")) + "</p></div>";
      return;
    }
    const presetOptions = '<option value="">' + esc(t("selectPlaceholder")) + "</option>" + [1, 2, 3, 4, 5, 6].map((slot) => '<option value="' + slot + '">' + esc(t("preset")) + " " + slot + "</option>").join("");
    const header = '<div class="table-wrap"><table><thead><tr>' +
      "<th>" + esc(t("station")) + "</th>" +
      "<th>" + esc(t("country")) + "</th>" +
      "<th>" + esc(t("codec")) + "</th>" +
      "<th>" + esc(t("tags")) + "</th>" +
      "<th>" + esc(t("actions")) + "</th>" +
      "</tr></thead><tbody>";
    const rows = state.catalog.map((station, index) => {
      const checked = Number(station.lastcheckok) === 1;
      const country = station.countrycode || station.country || "-";
      const format = [station.codec || "-", station.bitrate ? station.bitrate + " kbps" : ""].filter(Boolean).join(" · ");
      return "<tr>" +
        '<td><div class="station-cell">' + artBlock(station.favicon, station.name) +
          '<div class="station-text"><div class="station-name">' + esc(station.name || "-") + "</div>" +
            (station.homepage ? '<span class="station-url"><a href="' + esc(station.homepage) + '" target="_blank" rel="noreferrer">' + esc(hostOf(station.homepage)) + "</a></span>" : "") +
          "</div></div></td>" +
        '<td data-label="' + esc(t("country")) + '"><span class="status ' + (checked ? "good" : "bad") + '" title="' + esc(checked ? t("lastCheckOk") : t("lastCheckBad")) + '">' + esc(country) + "</span></td>" +
        '<td data-label="' + esc(t("codec")) + '">' + esc(format) + "</td>" +
        '<td class="cell-wrap" data-label="' + esc(t("tags")) + '">' + esc(station.tags || "-") + "</td>" +
        '<td class="cell-actions" data-label="' + esc(t("actions")) + '"><div class="actions catalog-actions">' +
          '<div class="action-links">' +
            '<a class="btn ghost small" href="' + esc(station.urlResolved || station.url) + '" target="_blank" rel="noreferrer">' + esc(t("test")) + "</a>" +
            '<button type="button" class="btn ghost small" data-action="catalog-add" data-index="' + index + '">' + esc(t("add")) + "</button>" +
          "</div>" +
          '<div class="preset-assign">' +
            '<select class="inline-select" data-role="catalog-preset" data-index="' + index + '">' + presetOptions + "</select>" +
            '<select class="inline-select" data-role="catalog-mode" data-index="' + index + '">' + catalogModeOptions() + "</select>" +
            '<button type="button" class="btn ghost small accent" data-action="catalog-set" data-index="' + index + '">' + esc(t("set")) + "</button>" +
          "</div>" +
        "</div></td></tr>";
    }).join("");
    container.innerHTML = header + rows + "</tbody></table></div>";
  }

  // ------------------------------------------------------------------- setup
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
    setHealth(kind, errors || warnings ? t("degraded") : t("ready"));

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
    const summary = errors
      ? (errors === 1 ? t("setupErrorOne") : t("setupErrors", { count: errors }))
      : warnings
        ? (warnings === 1 ? t("setupWarningOne") : t("setupWarnings", { count: warnings }))
        : t("setupOk");

    const findingHtml = findings.map((finding) => {
      const title = t(finding.code);
      const hint = t("hint_" + finding.code);
      const message = String(finding.message || "");
      const showDetail = title === finding.code || /https?:\/\/|HTTP \d|\.xml|\.json/.test(message);
      return '<li class="finding">' +
        '<div class="finding-title ' + esc(finding.severity) + '">' + esc(title === finding.code ? message : title) + "</div>" +
        (title !== finding.code && showDetail ? '<div class="finding-detail">' + esc(message) + "</div>" : "") +
        '<div class="finding-hint"><span class="muted">' + esc(t("hint")) + ":</span> " + esc(hint === "hint_" + finding.code ? (finding.hint || "") : hint) + "</div>" +
        "</li>";
    }).join("");

    banner.className = "notice " + kind;
    banner.hidden = false;
    banner.innerHTML =
      '<div class="notice-head">' +
        '<div><div class="notice-title">' + esc(summary) + '</div><div class="notice-meta">' + esc(setup.aftertouchUrl || "") + "</div></div>" +
        '<div class="actions">' +
          '<button type="button" class="btn secondary small" data-action="setup-refresh">' + esc(t("refreshSetup")) + "</button>" +
          '<a class="btn secondary small" href="' + esc(setup.aftertouchUrl || "#") + '" target="_blank" rel="noreferrer">' + esc(t("openAfterTouch")) + "</a>" +
        "</div>" +
      "</div>" +
      (findings.length ? '<ul class="findings">' + findingHtml + "</ul>" : '<div class="hint">' + esc(t("setupOk")) + "</div>") +
      '<div class="facts">' +
        "<div>" + esc(t("aftertouchVersion")) + ": <b>" + esc(version) + "</b></div>" +
        "<div>" + esc(t("registry")) + ': <b class="' + (aftertouch.registryReachable ? "" : "error-text") + '">' + esc(registry) + "</b></div>" +
        "<div>" + esc(t("orion")) + ': <b class="' + (aftertouch.orion && aftertouch.orion.available ? "" : "error-text") + '">' + esc(orion) + "</b></div>" +
        "<div>" + esc(t("radioBrowser")) + ': <b class="' + (aftertouch.radioBrowser && aftertouch.radioBrowser.available ? "" : "error-text") + '">' + esc(radioBrowser) + "</b></div>" +
        "<div>" + esc(t("device")) + ": <b>" + esc((setup.speakerIps || []).join(", ") || "-") + "</b></div>" +
      "</div>";
  }

  // ---------------------------------------------------------------- speakers
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
      const reachable = '<span class="status ' + (device.speakerReachable ? "good" : "bad") + '">' +
        esc(device.speakerReachable ? t("reachable") : t("unreachable")) + "</span>";
      const sources = '<span class="status ' + (device.sourcesReady ? "good" : "bad") + '">' +
        esc(device.sourcesReady ? t("sourcesReady") : t("sourcesMissing", { sources: (device.missingSources || []).join(", ") })) + "</span>";
      const marge = device.margeReachable === null ? "" : '<span class="status ' + (device.margeReachable ? "good" : "bad") + '">HTTP ' + esc(device.margeStatus || "?") + "</span>";
      const playing = device.nowPlaying || {};
      let now = '<div class="now-line">' + esc(t("nothingPlaying")) + "</div>";
      if (playing.stationName || playing.track) {
        const stateLabel = playStateLabel(playing.playStatus);
        now = '<div class="now-line"><span>' + esc(t("nowPlaying")) + ":</span><b>" + esc(playing.stationName || playing.track) + "</b>" +
          (stateLabel ? "<span>" + esc(stateLabel) + "</span>" : "") +
          (playing.source ? "<span>" + esc(sourceLabel(playing.source)) + "</span>" : "") +
          "</div>";
      }
      return '<article class="device">' +
        '<div class="device-head">' +
          '<div><div class="device-name">' + esc(device.name || t("device")) + '</div><span class="device-ip">' + esc(device.speakerIp || "") + "</span></div>" +
          '<div class="device-status">' + reachable + sources + "</div>" +
        "</div>" +
        '<dl class="facts">' +
          "<div><dt>" + esc(t("firmware")) + '</dt><dd class="mono">' + esc(device.firmware || "-") + "</dd></div>" +
          "<div><dt>" + esc(t("marge")) + '</dt><dd class="mono">' + esc(device.currentMargeUrl || "-") + " " + marge + "</dd></div>" +
        "</dl>" + now +
        "</article>";
    }).join("");
  }

  // ------------------------------------------------------------------- relay
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

    const summary = '<div class="readouts">' +
      readout(t("activeStreams"), active.length) +
      readout(t("reconnects"), reconnects) +
      readout(t("errors"), errors) +
      readout(t("sent"), fmtBytes(sent)) +
      "</div>";

    const activeTable = active.length ? proxyTable(active, true) : '<p class="hint">' + esc(t("noActive")) + "</p>";
    const recentTable = recent.length ? proxyTable(recent, false) : '<p class="hint">' + esc(t("noRecent")) + "</p>";
    container.innerHTML = summary + '<h3 class="subhead">' + esc(t("current")) + "</h3>" + activeTable +
      '<h3 class="subhead">' + esc(t("recent")) + "</h3>" + recentTable;
  }

  function readout(label, value) {
    return '<div class="readout"><div class="readout-label">' + esc(label) + '</div><div class="readout-value">' + esc(value) + "</div></div>";
  }

  function proxyTable(items, active) {
    const header = '<div class="table-wrap"><table><thead><tr>' +
      "<th>" + esc(t("station")) + "</th>" +
      "<th>" + esc(t("state")) + "</th>" +
      "<th>" + (active ? esc(t("duration")) : esc(t("started"))) + "</th>" +
      "<th>" + esc(t("bytes")) + "</th>" +
      "<th>" + esc(t("buffer")) + "</th>" +
      "<th>" + esc(t("cause")) + "</th>" +
      "</tr></thead><tbody>";
    const rows = items.map((item) => {
      const kind = item.state === "streaming" ? "good" : (item.state === "buffer-underrun" || item.state === "error" || item.state === "client-disconnect") ? "bad" : "warn";
      const buffer = (item.bufferFillPercent !== undefined ? Number(item.bufferFillPercent).toFixed(1) + "%" : "-") +
        " · " + fmtBytes(item.bufferedBytes || 0) +
        " · " + (item.underrunCount || 0) + " " + esc(t("underruns"));
      const cause = item.lastEventMessage || item.lastError || "-";
      const when = active ? fmtDuration(item.durationSeconds || 0) : fmtTime(item.startedAt) + " → " + fmtTime(item.endedAt);
      return "<tr>" +
        "<td>" + esc(item.station || "-") + "</td>" +
        '<td data-label="' + esc(t("state")) + '"><span class="status ' + kind + '">' + esc(item.state || "-") + "</span></td>" +
        '<td data-label="' + esc(active ? t("duration") : t("started")) + '">' + esc(when) + "</td>" +
        '<td data-label="' + esc(t("bytes")) + '">' + esc(fmtBytes(item.bytesIn || 0)) + " " + esc(t("bytesIn")) + "<br>" + esc(fmtBytes(item.bytesOut || 0)) + " " + esc(t("bytesOut")) + "</td>" +
        '<td data-label="' + esc(t("buffer")) + '">' + esc(buffer) + "</td>" +
        '<td class="cell-wrap" data-label="' + esc(t("cause")) + '">' + esc(cause) + "</td></tr>";
    }).join("");
    return header + rows + "</tbody></table></div>";
  }

  function renderAll() {
    applyStaticI18n();
    populateStaticSelects();
    renderDisplay();
    renderPresets();
    renderStations();
    renderCatalog();
    renderSetup();
    renderSpeakers();
    renderProxy();
  }

  // ------------------------------------------------------------------ actions
  function modeForPlay(slot, stationName) {
    const modeSelect = $("#mode-select-" + slot);
    if (modeSelect) return modeSelect.value;
    const station = state.stations.find((item) => item.name === stationName);
    return station ? slotMode(station, slot) : (state.settings.defaultPresetMode || "auto");
  }

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
    state.catalogBusy = true;
    state.catalog = [];
    renderCatalog();
    try {
      const data = await api("/api/catalog/search?" + params.toString());
      if (data.status !== "ok") {
        showToast(t("catalogError", { message: data.message || "?" }), "error");
        state.catalog = [];
        state.catalogSearched = false;
      } else {
        state.catalog = data.stations || [];
        state.catalogSearched = true;
      }
    } catch (error) {
      state.catalog = [];
      state.catalogSearched = false;
      showToast(error.message, "error");
    }
    state.catalogBusy = false;
    renderCatalog();
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
  let settingsReturnFocus = null;

  function openSettings() {
    const panel = $("#settingsPanel");
    const backdrop = $("#settingsBackdrop");
    if (!panel || !backdrop) return;
    settingsReturnFocus = document.activeElement;
    panel.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("no-scroll");
    const shell = $(".shell");
    if (shell) shell.setAttribute("aria-hidden", "true");
    const settingsButton = $("#settingsBtn");
    if (settingsButton) settingsButton.setAttribute("aria-expanded", "true");
    if ($("#bufferSeconds")) $("#bufferSeconds").value = state.settings.bufferSeconds || 3;
    if ($("#prebufferSeconds")) $("#prebufferSeconds").value = state.settings.prebufferSeconds || 0;
    if ($("#bitrateKbps")) $("#bitrateKbps").value = state.settings.bitrateKbps || 128;
    if ($("#reconnectBackoffMs")) $("#reconnectBackoffMs").value = (state.settings.reconnectBackoffMs || [250, 500, 1000, 2000, 3000]).join(",");
    if ($("#apiToken")) $("#apiToken").value = state.apiToken;
    renderModeSelectOptions();
    const first = $("#bufferSeconds");
    if (first) first.focus();
  }

  function closeSettings() {
    const panel = $("#settingsPanel");
    const backdrop = $("#settingsBackdrop");
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    if (backdrop) backdrop.hidden = true;
    document.body.classList.remove("no-scroll");
    const shell = $(".shell");
    if (shell) shell.removeAttribute("aria-hidden");
    const settingsButton = $("#settingsBtn");
    if (settingsButton) settingsButton.setAttribute("aria-expanded", "false");
    const target = settingsReturnFocus && settingsReturnFocus !== document.body ? settingsReturnFocus : settingsButton;
    if (target && target.focus) target.focus();
    settingsReturnFocus = null;
  }

  function trapSettingsFocus(event) {
    const panel = $("#settingsPanel");
    if (!panel || panel.hidden || event.key !== "Tab") return;
    const focusable = $$('button, input, select, textarea, a[href]', panel).filter((node) => !node.disabled && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
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
  function applyTabUI(name) {
    $$(".tab").forEach((tab) => {
      const active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
    $$(".view").forEach((view) => {
      const active = view.id === "view-" + name;
      view.classList.toggle("active", active);
      view.hidden = !active;
    });
  }

  function startProxyTimer() {
    if (state.proxyTimer) return;
    state.proxyTimer = window.setInterval(loadProxy, 3000);
  }

  function stopProxyTimer() {
    if (!state.proxyTimer) return;
    window.clearInterval(state.proxyTimer);
    state.proxyTimer = null;
  }

  function showTab(name, updateHash) {
    state.tab = name;
    if (updateHash !== false) {
      const hash = "#" + name;
      if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
    }
    applyTabUI(name);
    if (name === "status") {
      loadProxy();
      loadSpeakers(false);
      loadSetup(false);
      startProxyTimer();
    } else {
      stopProxyTimer();
    }
    if (name === "presets") loadSpeakers(false);
  }

  // -------------------------------------------------------------------- init
  const FOCUS_ACTIONS = ["preset-set", "preset-remove", "preset-probe", "station-probe", "station-play", "preset-play", "station-delete", "setup-refresh", "catalog-add", "catalog-set"];

  function captureFocus() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) return null;
    if (el.id) return { id: el.id };
    const action = el.getAttribute("data-action");
    if (!action) return null;
    let scope = "";
    if (el.closest("#presetEditor")) scope = "#presetEditor ";
    else if (el.closest("#presetDeck")) scope = "#presetDeck ";
    else if (el.closest(".rack")) scope = ".rack ";
    else if (el.closest("#setupBanner")) scope = "#setupBanner ";
    return {
      scope,
      action,
      station: el.getAttribute("data-station"),
      slot: el.getAttribute("data-slot"),
      index: el.getAttribute("data-index")
    };
  }

  function attrSelector(name, value) {
    return "[" + name + '="' + String(value).replace(/[\\"]/g, "\\$&") + '"]';
  }

  function restoreFocus(info) {
    const active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement && document.contains(active)) return;
    let el = null;
    if (info.id) {
      el = document.getElementById(info.id);
    } else {
      let selector = info.scope + attrSelector("data-action", info.action);
      if (info.station !== null) selector += attrSelector("data-station", info.station);
      if (info.slot !== null) selector += attrSelector("data-slot", info.slot);
      if (info.index !== null) selector += attrSelector("data-index", info.index);
      el = document.querySelector(selector);
    }
    if (!el) el = document.querySelector("#presetEditor .btn.primary") || document.querySelector("#presetEditor .quiet-btn");
    if (el && el.focus) el.focus({ preventScroll: true });
  }

  function bindEvents() {
    document.addEventListener("click", async (event) => {
      const target = event.target.closest("[data-action]");
      if (target) {
        const action = target.getAttribute("data-action");
        const station = target.getAttribute("data-station");
        const slot = target.getAttribute("data-slot");
        const index = parseInt(target.getAttribute("data-index"), 10);
        const focusInfo = FOCUS_ACTIONS.indexOf(action) !== -1 ? captureFocus() : null;
        try {
          if (action === "preset-set") await setPreset(parseInt(slot, 10));
          else if (action === "preset-remove") await removePreset(parseInt(slot, 10));
          else if (action === "preset-probe" || action === "station-probe") await probeStation(station);
          else if (action === "preset-play") await playStation(station, modeForPlay(slot, station), slot);
          else if (action === "preset-edit") openEditor(parseInt(slot, 10));
          else if (action === "preset-edit-close") closeEditor();
          else if (action === "station-play") await playStation(station, "auto");
          else if (action === "station-delete") await deleteStation(station, target.getAttribute("data-name"));
          else if (action === "catalog-add") await addCatalog(index);
          else if (action === "catalog-set") await setCatalogPreset(index);
          else if (action === "setup-refresh") { await loadSetup(true); await loadStations(); }
        } finally {
          if (focusInfo) restoreFocus(focusInfo);
        }
        return;
      }
      const tab = event.target.closest("[data-tab]");
      if (tab) {
        showTab(tab.getAttribute("data-tab"));
        if (!tab.classList.contains("tab")) {
          const activeTab = $("#tab-" + tab.getAttribute("data-tab"));
          if (activeTab) activeTab.focus();
        }
      }
    });

    const editor = $("#presetEditor");
    if (editor) {
      editor.addEventListener("change", (event) => {
        const select = event.target;
        if (!select || !select.getAttribute) return;
        const role = select.getAttribute("data-role");
        const slot = select.getAttribute("data-slot");
        if (role === "station") {
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
          const help = $("#mode-help-" + slot);
          if (help && modeSelect) help.textContent = modeHelp(modeSelect.value);
        } else if (role === "mode") {
          const help = $("#mode-help-" + slot);
          if (help) help.textContent = modeHelp(select.value);
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

    const tabs = $("#tabs");
    if (tabs) {
      tabs.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        const buttons = $$(".tab");
        const index = buttons.indexOf(document.activeElement);
        if (index === -1) return;
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : buttons.length - 1;
        const next = buttons[(index + step) % buttons.length];
        next.focus();
        showTab(next.getAttribute("data-tab"));
      });
    }

    document.addEventListener("keydown", (event) => {
      trapSettingsFocus(event);
      if (event.key !== "Escape") return;
      const panel = $("#settingsPanel");
      if (panel && !panel.hidden) {
        closeSettings();
      } else if (state.editSlot) {
        closeEditor();
      }
    });
  }

  async function init() {
    bindEvents();
    populateStaticSelects();
    applyStaticI18n();
    const initialTab = (window.location.hash || "").replace("#", "");
    state.tab = ["presets", "catalog", "status"].indexOf(initialTab) !== -1 ? initialTab : "presets";
    applyTabUI(state.tab);
    renderPresets();
    renderStations();
    renderCatalog();
    renderProxy();
    renderDisplay();
    await loadAll();
    if (state.tab === "status") startProxyTimer();
    state.setupTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") loadSetup(false);
    }, 30000);
    state.speakersTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") loadSpeakers(false);
    }, 20000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") loadSpeakers(false);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
