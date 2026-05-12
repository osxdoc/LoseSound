# SoundTouch Self-Hosted with AfterTouch + Preset-Proxy

Self-host your Bose SoundTouch speaker: no more cloud dependency.

## Overview

This setup replaces the manufacturer's cloud connection with a local AfterTouch server and a stream proxy that reliably resolves dispatcher URLs and automatically reconnects on brief stream interruptions.

**Services:**

- **AfterTouch** (Port 8091): Local Marge/BMX server replacing the speaker's cloud connection
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

Ports and stream tuning values are optional and have defaults in `docker-compose.yml`.

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
- Verify Preset-Proxy is reachable: `http://<SERVICE_HOST>:8092`
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
