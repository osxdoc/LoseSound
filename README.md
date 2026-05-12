# SoundTouch Self-Hosted with AfterTouch + Preset-Proxy

Self-host your Bose SoundTouch speaker: no more cloud dependency.

## Overview

This setup replaces the manufacturer's cloud connection with a local AfterTouch server and a stream proxy that reliably resolves dispatcher URLs and automatically reconnects on brief stream interruptions.

**Services:**

- **AfterTouch** (Port 8091): Local Marge/BMX server replacing the speaker's cloud connection, using host networking for UPnP/SSDP discovery
- **Preset-Proxy** (Port 8092): Stream proxy with short buffer + reconnect + Web UI for preset management

## Requirements

- Docker + Docker Compose
- SoundTouch speaker on the same LAN
- Static or reserved IP address for the Docker host is recommended

## Setup

### 1. Clone the repository

```bash
git clone <repository-url> LoseSound
cd LoseSound
```

### 2. Run automatic setup

```bash
python3 scripts/setup.py
```

The setup script:

- detects the Docker host LAN IP
- finds the SoundTouch speaker via SSDP or a local subnet scan
- writes `.env` with only `SERVICE_HOST` and `SPEAKER_IP`
- starts Docker Compose
- leaves speaker discovery and migration in the AfterTouch Web UI by default

If automatic speaker discovery fails, provide the speaker IP once:

```bash
python3 scripts/setup.py --speaker-ip 10.10.10.26
```

To only generate `.env` without starting Docker:

```bash
python3 scripts/setup.py --no-start
```

If you have multiple speakers, the setup script lists them and asks which one the Preset-Proxy should control. AfterTouch can still discover and migrate all speakers independently in its own Web UI.

To also send the migration request from the setup script:

```bash
python3 scripts/setup.py --migrate
```

### Manual setup

Copy the example file only if you do not want to use the setup script:

```bash
cp .env.example .env
docker compose up -d
```

Only these values are normally required:

```bash
SERVICE_HOST=10.10.10.85      # Docker host LAN IP
SPEAKER_IP=10.10.10.26        # SoundTouch speaker IP
```

Ports and stream tuning values are optional and have defaults in `docker-compose.yml`. AfterTouch uses Docker host networking so speaker discovery can receive UPnP/SSDP responses from the LAN.

To migrate manually, open:

```
http://<SERVICE_HOST>:8091/setup/migrate/<DEVICE_ID>?target_url=http://<SERVICE_HOST>:8091
```

### 3. Set presets

Open the Preset UI in your browser:

```
http://<SERVICE_HOST>:8092
```

- Select a station from the dropdown for Preset 1-6
- Click "Save"
- The preset is stored on the SoundTouch speaker

## Managing Stations

In the Preset UI you can:

- **Add new station**: Enter name, display name, and dispatcher URL
- **Browse station catalog**: Search Radio Browser by country, category, and popularity
- **Assign preset**: Select station and save preset number
- **Test station**: Open stream directly in browser
- **Inspect station**: Show resolved stream URL, HTTP/ICY metadata, bitrate, and latest proxy errors
- **Delete station**: Remove from the list
- **Tune streaming**: Adjust buffer seconds, expected bitrate, and reconnect backoff without editing `.env`

Stream settings are saved in the `preset_proxy_data` Docker volume. New stream connections use the updated values immediately.

### Pre-configured Stations

HR3 and YOU FM are pre-configured. Find more streams at:

- [radiomonster](https://radiomonster.fm/stream-regions/europe/germany)
- [radio-browser.info](https://www.radio-browser.info)

## Updating AfterTouch

```bash
docker compose pull aftertouch && docker compose up -d aftertouch
```

All data (accounts, presets, sources, certificates) persists in the Docker volume.

### Backup before update

```bash
docker run --rm -v losesound_aftertouch_data:/data -v $(pwd):/backup alpine tar czf /backup/aftertouch_backup_$(date +%Y%m%d_%H%M).tgz -C /data .
```

## Troubleshooting

### Stream keeps reconnecting

Increase `BUFFER_SECONDS` in `.env` or check your network connection.

### SoundTouch won't play stream

- Verify `SERVICE_HOST` and `SPEAKER_IP` are correct
- Verify Preset-Proxy is reachable: `http://<SERVICE_HOST>:8092`
- Check speaker logs on the SoundTouch device

### Preset save returns HTTP 400

Rebuild the Preset-Proxy so the current `storePreset` XML format is used:

```bash
docker compose up -d --build --force-recreate preset-proxy
```

Then verify the speaker API is reachable from the Docker host:

```bash
curl http://<SPEAKER_IP>:8090/info
```

### Analyze proxy streaming

Follow the Preset-Proxy logs while pressing a preset on the SoundTouch:

```bash
docker compose logs -f preset-proxy
```

Useful events are `station.json`, `stream.start`, `dispatcher.resolve.ok`, `stream.upstream.open`, `stream.response.sent`, `stream.client.disconnect`, and `stream.end`.

For verbose byte counters, set this in `.env` and recreate the proxy:

```bash
LOG_STREAM_CHUNKS=1
docker compose up -d --build --force-recreate preset-proxy
```

### AfterTouch Web UI not working

```bash
docker compose logs aftertouch
```

### Reset data

To reset all data:

```bash
docker compose down -v
docker compose up -d
```

## Ports

| Service | Port | Description |
|---------|------|-------------|
| AfterTouch HTTP | 8091 | Web UI, Marge, BMX API |
| AfterTouch HTTPS | 8444 | HTTPS endpoint |
| Preset-Proxy | 8092 | Preset UI, Stream Proxy, API |

## Architecture

```
┌─────────────────────┐      ┌─────────────────────┐
│   SoundTouch        │      │   Docker Host        │
│   Speaker           │──────│                     │
│   10.10.10.26       │      │  ┌─────────────────┐ │
└─────────────────────┘      │  │ AfterTouch      │ │
                              │  │ Port 8091       │ │
                              │  │ (Marge/BMX)     │ │
                              │  └─────────────────┘ │
                              │                     │
                              │  ┌─────────────────┐ │
                              │  │ Preset-Proxy    │ │
                              │  │ Port 8092       │ │
                              │  │ - Web UI        │ │
                              │  │ - Stream Proxy  │ │
                              │  │ - API           │ │
                              │  └────────┬────────┘ │
                              └────────────│──────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │   HR3/YOU FM        │
                              │   Dispatcher URLs    │
                              └─────────────────────┘
```
