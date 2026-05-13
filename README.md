# LoseSound: SoundTouch + AfterTouch + Preset Proxy

## English

LoseSound runs a local preset and stream proxy for Bose SoundTouch speakers together with [AfterTouch / Bose SoundTouch Toolkit](https://github.com/gesellix/Bose-SoundTouch).

### Requirements

- Docker + Docker Compose
- SoundTouch speaker on the same LAN

### Quick Start

```bash
python3 scripts/setup.py
```

If speaker discovery does not find your device:

```bash
python3 scripts/setup.py --speaker-ip 10.10.10.26
```

The setup script writes `.env`, starts Docker, and prints the local URLs.

### URLs

- AfterTouch: `http://<SERVICE_HOST>:8091`
- Preset Proxy: `http://<SERVICE_HOST>:8092`

### Important

AfterTouch migration is required. Without it, the SoundTouch will not load local presets reliably. The Preset Proxy page checks the setup and only shows a warning when something is missing or incorrect.

If AfterTouch asks for SSH, insert a FAT/FAT32 USB stick with an empty `remote_services` file and reboot the speaker.

### Audio Proxy Mode

By default, new presets point to local station JSON and the audio stream runs through the proxy. In the preset table you can choose per preset slot whether it should use `via proxy` or `direct`. Direct preset assignments point straight to the station URL; proxy assignments point to local JSON and use the proxy stream for buffering/debugging. Existing presets need to be set again after changing the mode.

The default for new preset rows can also be set through `.env`:

```env
AUDIO_PROXY_ENABLED=0
```

### Multiple SoundTouch Devices

`SPEAKER_IP` is the speaker used for direct preset writes. `SPEAKER_IPS` can contain all speakers the web UI should check.

```env
SERVICE_HOST=10.10.10.61
SPEAKER_IP=10.10.10.26
SPEAKER_IPS=10.10.10.26,10.10.10.27
```

AfterTouch can discover and migrate all speakers in its own web UI.

### Update

```bash
docker compose up -d --build --force-recreate preset-proxy
```

## Deutsch

LoseSound betreibt einen lokalen Preset- und Stream-Proxy für Bose SoundTouch zusammen mit [AfterTouch / Bose SoundTouch Toolkit](https://github.com/gesellix/Bose-SoundTouch).

### Voraussetzungen

- Docker + Docker Compose
- SoundTouch im gleichen Netzwerk

### Schnellstart

```bash
python3 scripts/setup.py
```

Falls der Lautsprecher nicht automatisch gefunden wird:

```bash
python3 scripts/setup.py --speaker-ip 10.10.10.26
```

Das Setup-Skript schreibt `.env`, startet Docker und zeigt die lokalen URLs an.

### URLs

- AfterTouch: `http://<SERVICE_HOST>:8091`
- Preset Proxy: `http://<SERVICE_HOST>:8092`

### Wichtig

Die AfterTouch-Migration ist nötig. Ohne sie ruft der SoundTouch lokale Presets nicht zuverlässig ab. Die Preset-Proxy-Seite prüft die Einrichtung und zeigt nur dann eine Warnung, wenn etwas fehlt oder falsch eingestellt ist.

Wenn AfterTouch SSH verlangt: FAT/FAT32 USB-Stick mit leerer Datei `remote_services` einstecken und den Speaker neu starten.

### Audio-Proxy-Modus

Standardmäßig zeigen neue Presets auf lokale Sender-JSON und der Audio-Stream läuft durch den Proxy. In der Preset-Tabelle kannst du pro Preset-Slot wählen, ob er `über Proxy` oder `direkt` laufen soll. Direkte Belegungen zeigen direkt auf die Sender-URL; Proxy-Belegungen zeigen auf lokale JSON und nutzen den Proxy-Stream zum Puffern/Debuggen. Bestehende Presets müssen nach dem Umschalten einmal neu gesetzt werden.

Der Standard für neue Preset-Zeilen lässt sich auch über `.env` setzen:

```env
AUDIO_PROXY_ENABLED=0
```

### Mehrere SoundTouch-Geräte

`SPEAKER_IP` ist der Speaker, auf den Presets direkt geschrieben werden. `SPEAKER_IPS` kann alle Speaker enthalten, die die Weboberfläche prüfen soll.

```env
SERVICE_HOST=10.10.10.61
SPEAKER_IP=10.10.10.26
SPEAKER_IPS=10.10.10.26,10.10.10.27
```

AfterTouch kann alle Speaker in der eigenen Weboberfläche suchen und migrieren.

### Update

```bash
docker compose up -d --build --force-recreate preset-proxy
```
