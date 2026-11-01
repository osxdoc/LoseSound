# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-01

First public release.

### Added

- Preset engines `relay`, `speaker`, `radioBrowser` and `orion`, selectable per preset slot,
  with `auto` using the recommendation from the last stream check.
- Mode-aware station descriptions (`BmxPlaybackResponse`) with multiple `streams` for failover and
  station artwork.
- Stream check (`POST /api/stations/<name>/probe`) that follows redirects, detects tokenised URLs
  and recommends a playback mode.
- Setup diagnostics for AfterTouch (version, BMX registry, Orion availability, radio-browser),
  speaker marge URL and required radio sources, shown as actionable findings in the web UI.
- Read-only verification tool `scripts/verify.py` with table and JSON output.
- Web UI: preset editor, station list, radio-browser catalogue, speaker cards, relay statistics,
  language switch (EN/DE), optional API token handling.
- Audio relay with token-bucket pacing, prebuffering, single upstream connection, reconnect
  backoff, underrun/overflow statistics and health endpoint.
- Optional bearer token for write actions (`API_TOKEN`).
- Docker image published to GHCR, multi-arch (amd64/arm64), non-root, healthcheck.
- pytest suite with fake speaker, AfterTouch and stream origin; GitHub Actions CI, CodeQL and
  release workflow; issue templates, security policy and contribution guide.

### Changed

- Configuration schema v2 with automatic migration of `proxy` → `relay` and `direct` → `speaker`.
- Atomic config writes with a `.bak` copy; corrupt files are preserved instead of silently reset.
- `docker-compose.yml` now passes every documented environment variable, pins AfterTouch via
  `SOUNDTOUCH_VERSION` and uses the published LoseSound image.
- `scripts/setup.py` migrates speakers via `POST`, preserves unknown `.env` keys and no longer
  overwrites `SERVICE_HOST` silently.
- Frontend split into `index.html`, `style.css`, `app.js` and `i18n.js`; CSP and security headers.

### Fixed

- **Direct mode never worked**: it stored a raw stream URL as preset location, but the speaker
  expects a station description. Direct now uses a JSON description whose `streamUrl` points at the
  station.
- `AUDIO_PROXY_ENABLED` was documented but never passed to the container.
- Preset writes are verified against `GET /presets` instead of trusting any 2xx response.
- Preset removal, speaker playback checks and station cleanup are supported.
- HTML/XSS issues in station names, broken `escapeHtml` on non-strings, dead API route branch and
  ignored `LISTEN_ADDR`.
- Setup no longer reports "ready" for a speaker whose marge URL answers 502.

### Removed

- Obsolete single-file `preset-proxy/app.py` layout and the old inline UI.
