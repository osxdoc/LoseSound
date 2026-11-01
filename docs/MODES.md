# Playback modes

A SoundTouch preset for internet radio is not a URL to an audio stream. It is a `ContentItem` whose
`location` the speaker **fetches**, expecting a station description (`BmxPlaybackResponse`) in
return. This is the single most important detail of the SoundTouch preset API.

If the location returns raw audio, the speaker accepts the preset but plays nothing - a silent
failure. This is documented by the AfterTouch project as well:

- [PRESETS.md - Save a direct stream URL](https://github.com/gesellix/Bose-SoundTouch/blob/main/docs/content/docs/guides/PRESETS.md)
- [Release v0.99.0](https://github.com/gesellix/Bose-SoundTouch/releases/tag/v0.99.0): "the
  speaker's BMX module calls GET on the stored location expecting a `BmxPlaybackResponse` JSON, not
  raw audio bytes"

LoseSound offers four ways to satisfy the speaker. The mode is chosen per preset slot.

## `relay`

```xml
<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl"
            location="http://losesound:8092/hr3.json" />
```

- `GET /hr3.json` returns a station description whose `streamUrl` is
  `http://losesound:8092/hr3`.
- The speaker then streams from LoseSound; the relay buffers and paces the upstream.
- The relay follows redirects and fetches a fresh token for every connection.
- Use it for ARD/Regiocast dispatchers (`dispatcher.rndfnk.com`, `streamabc.net`) and for debugging.

## `speaker` (Direct)

```xml
<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl"
            location="http://losesound:8092/hr3.json?mode=speaker" />
```

- The station description is built on request: LoseSound follows the dispatcher redirect once,
  caches the result for a few seconds and puts **the final URL first** and the original dispatcher
  URL second into `audio.streams`.
- The speaker fetches the audio itself. LoseSound only transfers a small JSON document.
- Works best for stable URLs. Tokenised URLs (`?token=`, `?skey=`, `?sABC=`) may expire while the
  speaker keeps the resolved URL; the relay is the safer choice there.

## `radioBrowser`

```xml
<ContentItem source="RADIO_BROWSER" type="stationurl"
            location="/stations/byuuid/9610c454-0601-11e8-ae97-52543be04c81" />
```

- No local service is involved in playback. The speaker resolves the relative location through the
  `RADIO_BROWSER` entry in its BMX registry (provided by AfterTouch) and asks
  `https://all.api.radio-browser.info/soundtouch/stations/byuuid/<uuid>` for the station JSON.
- Requires a station that was added from the catalogue (it carries a `stationuuid`) and a working
  `RADIO_BROWSER` source on the speaker.
- The stream URL comes from radio-browser's `url_resolved`; quality and redirect behaviour vary by
  station. Check the station with **Check stream** first.

## `orion` (AfterTouch 0.99+)

```xml
<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl"
            location="/station?data=eyJuYW1lIjoiSFIzIiwiaW1hZ2VVcmwiOiIiLCJzdHJlYW1VcmwiOiJodHRwczovL%2B...%7D" />
```

- Relative Orion station location. The speaker resolves it against the `LOCAL_INTERNET_RADIO`
  base URL from the BMX registry (AfterTouch) and fetches the JSON from AfterTouch.
- The location contains name, artwork and stream URL (base64 JSON), so no LoseSound process is in
  the playback path.
- Host-independent: if AfterTouch moves, presets keep working (the speaker resolves against the
  registry).
- Requires AfterTouch with a working Orion endpoint. The setup banner probes it and offers this
  mode only when available.

## `auto`

Uses the recommendation of the last **Check stream** result:

- redirect to a tokenised URL or unreachable stream → `relay`
- stable URL returning audio → `speaker`

If no probe has run yet, `auto` falls back to the default mode from Settings.

## Choosing a mode

| Situation | Recommended |
| --- | --- |
| New station from the catalogue | `radioBrowser` if the source is available, otherwise `speaker` |
| ARD/Regiocast dispatcher with token | `relay` |
| Stable Icecast/Shoutcast URL | `speaker` |
| AfterTouch ≥ 0.99 and you want presets independent of the LoseSound host | `orion` |
| Stream stutters on the speaker but works in a browser | `relay` |

Changing a mode does not rewrite existing presets. Set the slot again after changing the mode.

## Verifying a mode

- **Check stream** follows redirects, reports content type/bitrate and token detection.
- **Play now** sends `/select` with the exact `ContentItem` and polls `/now_playing`; it reports
  what the speaker actually did.
- `scripts/verify.py` classifies every preset location:
  - JSON description → PASS
  - raw audio URL → FAIL with the explanation above
  - `/station?data=...` → PASS (Orion)
  - `/stations/byuuid/...` → PASS (radio-browser)
