# Troubleshooting

Start with the read-only check:

```bash
python3 scripts/verify.py
```

The web UI shows the same findings as a banner. Every finding has a code that maps to one of the
sections below.

## The setup banner reports `marge_unreachable`

**Symptom:** the speaker's `/info` shows a marge URL, but requesting it returns 502. AfterTouch logs
show `[UNHANDLED] GET /marge/...` followed by a proxy request to its own HTTPS port and
`tls: first record does not look like a TLS handshake`.

**Cause:** AfterTouch versions before 0.99 did not serve a `/marge` path. The canonical marge URL is
the plain service base URL (`http://host:8091`), not `http://host:8091/marge`.

**Fix:** upgrade AfterTouch (see [UPGRADING.md](UPGRADING.md)) and migrate the speaker again:

```bash
curl -X POST "http://HOST:8091/setup/migrate/SPEAKER_IP?method=xml&target_url=http://HOST:8091&marge_url=http://HOST:8091"
```

Check `http://SPEAKER_IP:8090/info` afterwards: `margeURL` must not end with `/marge`.

## `sources_missing` / radio sources disappear after a reboot

The speaker loads `Sources.xml` only at boot. If AfterTouch is unreachable during a cold boot, or a
source was never written, `TUNEIN`, `LOCAL_INTERNET_RADIO` or `RADIO_BROWSER` disappear from
`/sources`.

Fix:

1. Make sure AfterTouch is reachable and its registry lists the source.
2. Reboot the speaker (power cycle) so it fetches `Sources.xml` again.
3. Trigger a refresh without rebooting:

```bash
curl -X POST http://SPEAKER_IP:8090/notification \
  -H "Content-Type: application/xml" \
  -d '<updates deviceID="DEVICE_ID"><sourcesUpdated/></updates>'
```

On some speakers radio sources never activate after an in-place migration; the confirmed remedy is
a factory reset followed by a fresh AfterTouch migration (see the AfterTouch troubleshooting docs).

## A preset plays nothing

Check the preset location in the UI or with:

```bash
curl -s http://SPEAKER_IP:8090/presets
```

- A raw `http(s)://.../stream` location for `LOCAL_INTERNET_RADIO` can never work. Save the slot
  again with **Direct** or **Relay**.
- `location` pointing at an old host or old port: the configuration changed. Open the preset table
  and set the slot again; `BASE_URL`/`SERVICE_HOST` must match the reachable address.
- **Play now** reports what the speaker did; if it times out, the speaker could not reach the
  location (test it from another machine on the same LAN).

## `radio_browser_unavailable`

The server cannot reach `https://all.api.radio-browser.info` (or the configured
`RADIO_BROWSER_BASE_URL`). Catalogue search and the `radioBrowser` mode need internet access from
the AfterTouch host/speaker. Other modes are unaffected. The registry entry comes from AfterTouch;
if the probe fails but the speaker has internet, `radioBrowser` may still work - the check runs from
the server, not the speaker.

## `orion_unavailable`

AfterTouch advertises the Orion endpoint in its BMX registry but the endpoint returns 502/404. That
was the case for the April/May 2026 builds. Update AfterTouch to a release that implements it
(0.99+; current is 0.138.x). Until then use **Direct** or **Relay**.

## Relay underruns / stutters

- Increase **Buffer** in Settings (e.g. 5-8 seconds) and keep **Prebuffer** at 1-2 seconds.
- Use **Check stream** to see the real bitrate and content type. Set a per-station bitrate in
  `config.json` (`"bitrateKbps": 192`) if pacing is off.
- The relay log explains the cause:

```bash
docker compose logs -f preset-proxy | grep -E "stream\.(health|buffer|upstream)"
```

- `upstream_read_gap_ms` high and `producer_bps` low → the station is slow, not the speaker.
- `buffer-underrun` with a full upstream → the speaker requested faster than the upstream delivers;
  a larger buffer helps.
- `buffer-overflow` → the buffer is too small for a bursty upstream; increase the buffer.

## API returns 401

An `API_TOKEN` is configured. Enter the token in Settings (the UI stores it in `localStorage`) or
send `Authorization: Bearer <token>` with `POST`/`DELETE` requests.

## `permission denied` after upgrading from 0.x

The container now runs as UID 10001. Older volumes are owned by root. Fix the ownership once:

```bash
docker run --rm -v losesound_preset_proxy_data:/data busybox chown -R 10001:10001 /data
```

## Images cannot be pulled

`docker compose pull` needs access to `ghcr.io`. For public packages no login is required. If the
package is still private, make it public in the GitHub package settings or run
`docker login ghcr.io` with a token that has `read:packages`.

## Speaker cannot reach `BASE_URL`

The speaker must be able to open the preset location. Typical causes:

- `SERVICE_HOST` is a Docker-internal or `localhost` address. Use the LAN IP of the Docker host.
- Firewall blocks the published `PROXY_PORT`.
- Docker runs on a VLAN the speaker cannot reach.

Test from another LAN machine: `curl http://HOST:8092/api/health`.

## Collecting diagnostics

```bash
python3 scripts/verify.py --json > verify.json
curl -s http://SPEAKER_IP:8090/info > speaker-info.xml
curl -s http://SPEAKER_IP:8090/presets > speaker-presets.xml
docker compose logs --tail 200 preset-proxy > lose-sound.log
```

Please redact stream tokens before attaching logs to an issue.
