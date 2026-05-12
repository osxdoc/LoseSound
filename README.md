# SoundTouch Self-Hosted with AfterTouch + Preset-Proxy

Self-host your Bose SoundTouch speaker: no more cloud dependency.

## Overview

This setup replaces the manufacturer's cloud connection with a local AfterTouch server and a stream proxy that reliably resolves dispatcher URLs and automatically reconnects on brief stream interruptions.

**Services:**

- **AfterTouch** (Port 8000): Local Marge/BMX server replacing the speaker's cloud connection
- **Preset-Proxy** (Port 8788): Stream proxy with short buffer + reconnect + Web UI for preset management

## Requirements

- Docker + Docker Compose
- SoundTouch speaker on the same LAN
- Static IP address for the Docker host

## Setup

### 1. Clone the repository

```bash
git clone <repository-url> LoseSound
cd LoseSound
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```bash
SERVICE_HOST=10.10.10.85      # Docker host IP
AFTERTOUCH_HTTP_PORT=8000     # AfterTouch Web UI port
AFTERTOUCH_HTTPS_PORT=8443    # AfterTouch HTTPS port
PROXY_PORT=8788               # Preset-Proxy port
SPEAKER_IP=10.10.10.26        # SoundTouch speaker IP
BUFFER_SECONDS=3              # Stream buffer size
BITRATE_KBPS=128              # Expected bitrate
RECONNECT_BACKOFF_MS=250,500,1000,2000,3000  # Reconnect wait times
```

### 3. Start Docker

```bash
docker compose up -d
```

### 4. Migrate SoundTouch speaker

After starting, redirect the SoundTouch speaker to the local AfterTouch server. Open in browser:

```
http://<SERVICE_HOST>:8000/setup/migrate/<DEVICE_ID>?target_url=http://<SERVICE_HOST>:8000
```

Find `<DEVICE_ID>` in the AfterTouch Web UI under "Devices" or via:

```bash
curl http://<SPEAKER_IP>:8090/info
```

Alternatively, migrate via the AfterTouch Web UI.

### 5. Set presets

Open the Preset UI in your browser:

```
http://<SERVICE_HOST>:8788
```

- Select a station from the dropdown for Preset 1-6
- Click "Save"
- The preset is stored on the SoundTouch speaker

## Managing Stations

In the Preset UI you can:

- **Add new station**: Enter name, display name, and dispatcher URL
- **Assign preset**: Select station and save preset number
- **Test station**: Open stream directly in browser
- **Delete station**: Remove from the list

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
- Verify Preset-Proxy is reachable: `http://<SERVICE_HOST>:8788`
- Check speaker logs on the SoundTouch device

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
| AfterTouch HTTP | 8000 | Web UI, Marge, BMX API |
| AfterTouch HTTPS | 8443 | HTTPS endpoint |
| Preset-Proxy | 8788 | Preset UI, Stream Proxy, API |

## Architecture

```
┌─────────────────────┐      ┌─────────────────────┐
│   SoundTouch        │      │   Docker Host        │
│   Speaker           │──────│                     │
│   10.10.10.26       │      │  ┌─────────────────┐ │
└─────────────────────┘      │  │ AfterTouch      │ │
                              │  │ Port 8000       │ │
                              │  │ (Marge/BMX)     │ │
                              │  └─────────────────┘ │
                              │                     │
                              │  ┌─────────────────┐ │
                              │  │ Preset-Proxy    │ │
                              │  │ Port 8788       │ │
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
