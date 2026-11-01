# Architecture

LoseSound is deliberately small: one Python service, one static web UI and an optional audio relay.

```
┌──────────────┐   1. preset via /storePreset    ┌──────────────────┐
│  Web browser │ ──────────────────────────────► │  LoseSound       │
│  (LoseSound) │ ◄────────────────────────────── │  HOST:8092       │
└──────────────┘   status / diagnostics          └────────┬─────────┘
                                                          │
                 2. ContentItem (JSON URL, RADIO_BROWSER  │ 3. GET /presets,
                    or /station?data=...)                 │    /select,
                                                          ▼    /now_playing
┌──────────────┐   4. GET station description    ┌──────────────────┐
│  SoundTouch  │ ◄────────────────────────────── │  LoseSound       │
│  speaker     │                                 │  station JSON    │
│  :8090       │   5. GET audio (direct modes)   └──────────────────┘
└──────┬───────┘ ──────────────────────────────────────────────┐
       │                                                       │
       │ 6. marge / BMX registry / Orion station JSON          │
       ▼                                                       ▼
┌──────────────┐                                   ┌────────────────────┐
│  AfterTouch  │ ◄──────────────────────────────── │  radio-browser.info│
│  :8091/:8444 │   (TUNEIN, LOCAL_INTERNET_RADIO,  │  native SoundTouch │
└──────────────┘    RADIO_BROWSER registry)        │  API               │
                                                   └────────────────────┘
```

## Components

| Component | Responsibility |
| --- | --- |
| `src/losesound/http_server.py` | HTTP API, static UI, security headers, optional token |
| `src/losesound/config.py` | Schema v2, migrations, validation, atomic persistence |
| `src/losesound/presets.py` | Build `ContentItem` XML per mode, store and verify presets |
| `src/losesound/stations.py` | Redirect probing, token detection, station JSON, catalogue |
| `src/losesound/relay.py` | Ring buffer, producer, paced consumer, stream statistics |
| `src/losesound/setup_checks.py` | AfterTouch/speaker diagnostics and findings |
| `scripts/setup.py` | LAN discovery, `.env`, Docker start, optional migration |
| `scripts/verify.py` | Read-only end-to-end health check |

## Preset write flow

1. The UI posts a station, slot and mode to `/api/stations/<name>/preset/<slot>`.
2. The service resolves `auto` to `relay` or `speaker` using `lastProbe`.
3. It builds a `ContentItem`:
   - `relay`: `LOCAL_INTERNET_RADIO` → `<BASE_URL>/<name>.json` (LoseSound serves the JSON, the JSON
     points back at the relay).
   - `speaker`: `LOCAL_INTERNET_RADIO` → `<BASE_URL>/<name>.json?mode=speaker` (the JSON points at
     the station URL, resolved on request).
   - `radioBrowser`: `RADIO_BROWSER` → `/stations/byuuid/<uuid>` (relative, resolved by the speaker's
     BMX registry).
   - `orion`: `LOCAL_INTERNET_RADIO` → `/station?data=<base64>` (relative, served by AfterTouch).
4. `POST /storePreset` writes it to the speaker.
5. `GET /presets` verifies source and location; a mismatch is reported as an error.
6. The configuration is updated so the UI knows which station owns the slot.

## Why the station JSON exists

The speaker does not play a `LOCAL_INTERNET_RADIO` location as audio. It fetches the location and
parses a `BmxPlaybackResponse`:

```json
{
  "name": "HR3",
  "imageUrl": "",
  "streamType": "liveRadio",
  "audio": {
    "hasPlaylist": false,
    "isRealtime": true,
    "streamUrl": "https://...",
    "streams": [{"hasPlaylist": false, "isRealtime": true, "streamUrl": "https://..."}]
  }
}
```

LoseSound reuses that document for the relay and direct modes. The `streams` array lets the speaker
fail over between candidates (resolved URL first, original URL second).

## Relay design

- One upstream connection per listener (no shared fan-out).
- Upstream redirects are followed by the producer; the final URL is logged (redacted).
- A deque of 8 KB chunks forms the ring buffer (`BUFFER_SECONDS` × bitrate).
- The consumer uses a token bucket: it sends immediately while the prebuffer allows it, then paces
  at the measured upstream rate so the buffer neither drains nor grows.
- Reconnects use the configured backoff; statistics (bytes, buffer fill, underruns, overflows,
  reconnects, last event) are exposed via `/api/proxy/status`.

## State and persistence

- `/data/config.json` (volume `preset_proxy_data`) holds settings, stations, preset ownership and
  the last probe result.
- Writes are atomic (`tmp` + `fsync` + `os.replace`) and keep a `.bak` of the previous file.
- Corrupt files are copied to `config.json.corrupt-<timestamp>` instead of being discarded.
- The UI settings override environment seeds after the first start. Environment values are defaults
  for a fresh volume.

## Network contracts

- Speaker: HTTP on `SPEAKER_PORT` (default 8090), XML API.
- AfterTouch: HTTP on 8091 (marge/BMX), HTTPS on 8444 for DNS migration.
- LoseSound: HTTP on `PROXY_PORT` (default 8092); the speaker must reach `BASE_URL`.
- Catalogue: HTTPS to `RADIO_BROWSER_BASE_URL`.
- Direct modes require the speaker itself to reach the station (and, for `radioBrowser`, to resolve
  the relative location through its BMX registry).
