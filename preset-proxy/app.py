import os
import json
import copy
import threading
import time
import collections
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

CONFIG_PATH = "/data/config.json"

def parse_backoff_ms(value):
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")

    backoff = []
    for item in items:
        try:
            wait_ms = int(str(item).strip())
        except ValueError:
            continue
        if 50 <= wait_ms <= 30000:
            backoff.append(wait_ms)
    return backoff or [250, 500, 1000, 2000, 3000]

DEFAULT_SETTINGS = {
    "bufferSeconds": int(os.getenv("BUFFER_SECONDS", "3")),
    "bitrateKbps": int(os.getenv("BITRATE_KBPS", "128")),
    "reconnectBackoffMs": parse_backoff_ms(os.getenv("RECONNECT_BACKOFF_MS", "250,500,1000,2000,3000")),
    "audioProxyEnabled": os.getenv("AUDIO_PROXY_ENABLED", "1").lower() in ("1", "true", "yes", "on")
}
DEFAULT_CONFIG = {
    "settings": DEFAULT_SETTINGS.copy(),
    "stations": {
        "hr3": {
            "name": "HR3",
            "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/hr3/live/mp3/high",
            "presets": [],
            "presetModes": {}
        },
        "youfm": {
            "name": "YOU FM",
            "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/youfm/live/mp3/high",
            "presets": [],
            "presetModes": {}
        }
    }
}

config_lock = threading.RLock()
config = None
stream_stats_lock = threading.Lock()
stream_stats = {}
active_streams = {}

SPEAKER_IP = os.getenv("SPEAKER_IP", "")
SPEAKER_IPS = [ip.strip() for ip in os.getenv("SPEAKER_IPS", SPEAKER_IP).split(",") if ip.strip()]
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8092")
AFTERTOUCH_URL = os.getenv("AFTERTOUCH_URL", "http://127.0.0.1:8091")
LOG_STREAM_CHUNKS = os.getenv("LOG_STREAM_CHUNKS", "0").lower() in ("1", "true", "yes", "on")
RADIO_BROWSER_BASE_URL = os.getenv("RADIO_BROWSER_BASE_URL", "https://de1.api.radio-browser.info")

def format_log_value(value):
    text = str(value).replace("\n", "\\n").replace("\r", "\\r")
    if not text or any(char.isspace() for char in text):
        return json.dumps(text)
    return text

def log(event, **fields):
    parts = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), event]
    for key, value in fields.items():
        parts.append(f"{key}={format_log_value(value)}")
    print(" ".join(parts), flush=True)

def http_get(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LoseSound-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()

def update_stream_stats(station_name, **updates):
    with stream_stats_lock:
        stats = stream_stats.setdefault(station_name, {})
        now = int(time.time())
        if "startedAt" not in stats and "startedAt" not in updates:
            stats["startedAt"] = now
        stats.update(updates)
        stats["updatedAt"] = now

def append_stream_event(station_name, side, event, message="", stream_id=None, **details):
    record = {
        "at": int(time.time()),
        "side": side,
        "event": event
    }
    if message:
        record["message"] = str(message)
    if stream_id:
        record["streamId"] = stream_id
    for key, value in details.items():
        if value is not None:
            record[key] = value

    with stream_stats_lock:
        stats = stream_stats.setdefault(station_name, {})
        history = list(stats.get("streamEvents", []))
        history.append(record)
        if len(history) > 30:
            history = history[-30:]
        stats["streamEvents"] = history
        stats["lastEvent"] = record
        stats["lastEventSide"] = side
        stats["lastEventType"] = event
        stats["lastEventMessage"] = record.get("message", "")
        stats["updatedAt"] = record["at"]

def get_stream_stats(station_name):
    with stream_stats_lock:
        return copy.deepcopy(stream_stats.get(station_name, {}))

def register_active_stream(stream_id, station_name, client):
    with stream_stats_lock:
        active_streams[stream_id] = {
            "streamId": stream_id,
            "station": station_name,
            "client": client,
            "startedAt": int(time.time()),
            "bytesIn": 0,
            "bytesOut": 0,
            "reconnects": 0,
            "state": "starting",
            "lastError": ""
        }

def update_active_stream(stream_id, **updates):
    with stream_stats_lock:
        if stream_id in active_streams:
            active_streams[stream_id].update(updates)
            active_streams[stream_id]["updatedAt"] = int(time.time())

def unregister_active_stream(stream_id):
    with stream_stats_lock:
        active_streams.pop(stream_id, None)

def proxy_status():
    now = int(time.time())
    with stream_stats_lock:
        active = copy.deepcopy(list(active_streams.values()))
        stats = copy.deepcopy(stream_stats)

    for item in active:
        item["durationSeconds"] = max(0, now - item.get("startedAt", now))

    station_stats = []
    with config_lock:
        station_names = sorted(config.get("stations", {}).keys())
    for name in station_names:
        item = stats.get(name, {})
        if item and item.get("endedAt"):
            item["station"] = name
            if item.get("updatedAt"):
                item["ageSeconds"] = max(0, now - item["updatedAt"])
            if item.get("startedAt"):
                item["durationSeconds"] = max(0, item["endedAt"] - item["startedAt"])
            station_stats.append(item)

    return {
        "status": "ok",
        "now": now,
        "activeStreams": active,
        "stationStats": station_stats
    }

def default_config():
    return copy.deepcopy(DEFAULT_CONFIG)

def normalize_settings(data):
    data = data if isinstance(data, dict) else {}

    def bounded_int(name, default, minimum, maximum):
        try:
            value = int(data.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def bool_setting(name, default):
        value = data.get(name, default)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    return {
        "bufferSeconds": bounded_int("bufferSeconds", DEFAULT_SETTINGS["bufferSeconds"], 1, 30),
        "bitrateKbps": bounded_int("bitrateKbps", DEFAULT_SETTINGS["bitrateKbps"], 32, 320),
        "reconnectBackoffMs": parse_backoff_ms(data.get("reconnectBackoffMs", DEFAULT_SETTINGS["reconnectBackoffMs"])),
        "audioProxyEnabled": bool_setting("audioProxyEnabled", DEFAULT_SETTINGS["audioProxyEnabled"])
    }

def normalize_playback_mode(value, default=None):
    if value in ("proxy", "direct"):
        return value
    if default in ("proxy", "direct"):
        return default
    settings = normalize_settings(config.get("settings") if config else DEFAULT_SETTINGS)
    return "proxy" if settings["audioProxyEnabled"] else "direct"

def normalize_config(data):
    changed = False
    if not isinstance(data, dict):
        return default_config(), True

    if not isinstance(data.get("stations"), dict):
        data["stations"] = copy.deepcopy(DEFAULT_CONFIG["stations"])
        changed = True

    normalized_settings = normalize_settings(data.get("settings"))
    if data.get("settings") != normalized_settings:
        data["settings"] = normalized_settings
        changed = True

    default_mode = "proxy" if normalized_settings["audioProxyEnabled"] else "direct"
    for station in data["stations"].values():
        if "presets" not in station or not isinstance(station["presets"], list):
            station["presets"] = []
            changed = True
        if "presetModes" not in station or not isinstance(station["presetModes"], dict):
            station["presetModes"] = {}
            changed = True
        valid_modes = {}
        for slot in station["presets"]:
            slot_key = str(slot)
            valid_modes[slot_key] = normalize_playback_mode(station["presetModes"].get(slot_key), default_mode)
        if station["presetModes"] != valid_modes:
            station["presetModes"] = valid_modes
            changed = True

    return data, changed

def stream_settings():
    with config_lock:
        settings = normalize_settings(config.get("settings", {}))
    return (
        settings["bufferSeconds"],
        settings["bitrateKbps"],
        settings["reconnectBackoffMs"],
        settings["audioProxyEnabled"]
    )

def load_config():
    global config
    with config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    loaded = json.load(f)
                config, changed = normalize_config(loaded)
                if changed:
                    save_config()
            except (json.JSONDecodeError, IOError):
                config = default_config()
                save_config()
        else:
            config = default_config()
            save_config()

def save_config():
    with config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)

def get_station_json(station_name):
    base = BASE_URL.rstrip("/")
    station = config["stations"][station_name]
    stream_url = f"{base}/{station_name}"
    return {
        "audio": {
            "hasPlaylist": False,
            "isRealtime": True,
            "streamUrl": stream_url
        },
        "imageUrl": "",
        "isRealtime": True,
        "name": station["name"],
        "playbackMode": "proxy",
        "streamUrl": stream_url,
        "streamType": "liveRadio"
    }

def preset_location_for_station(station_name, playback_mode=None):
    station = config["stations"][station_name]
    playback_mode = normalize_playback_mode(playback_mode)
    if playback_mode == "proxy":
        return f"{BASE_URL.rstrip('/')}/{station_name}.json", "proxy"
    return station["dispatcherUrl"], "direct"

def set_preset_on_speaker(station_name, slot, playback_mode=None):
    if not SPEAKER_IP:
        return {
            "ok": False,
            "message": "SPEAKER_IP is not configured. Run scripts/setup.py or set SPEAKER_IP in .env."
        }

    station = config["stations"][station_name]
    location, playback_mode = preset_location_for_station(station_name, playback_mode)
    now = int(time.time())
    preset_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<preset id="{slot}" createdOn="{now}" updatedOn="{now}">
  <ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location={quoteattr(location)} sourceAccount="" isPresetable="true">
    <itemName>{escape(station['name'])}</itemName>
  </ContentItem>
</preset>"""

    url = f"http://{SPEAKER_IP}:8090/storePreset"
    log("preset.store.start", station=station_name, slot=slot, speaker=SPEAKER_IP, location=location, playback_mode=playback_mode)
    try:
        req = urllib.request.Request(url, data=preset_xml.encode("utf-8"))
        req.add_header("Content-Type", "application/xml")
        req.add_header("Accept", "application/xml")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log("preset.store.ok", station=station_name, slot=slot, status=resp.status, bytes=len(body), playback_mode=playback_mode)
            return {
                "ok": True,
                "status": resp.status,
                "location": location,
                "playbackMode": playback_mode,
                "body": body
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        message = body.strip() or f"HTTP {e.code} {e.reason}"
        log("preset.store.http_error", station=station_name, slot=slot, status=e.code, message=message[:160])
        return {"ok": False, "status": e.code, "message": message}
    except Exception as e:
        log("preset.store.error", station=station_name, slot=slot, message=str(e))
        return {"ok": False, "message": str(e)}

def resolve_dispatcher(dispatcher_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SoundTouch/1.0)",
        "Accept": "*/*"
    }
    req = urllib.request.Request(dispatcher_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "audio/mpeg")
            log("dispatcher.resolve.ok", dispatcher=dispatcher_url, resolved=resp.url, content_type=content_type)
            return resp.url, content_type
    except Exception as e:
        log("dispatcher.resolve.error", dispatcher=dispatcher_url, message=str(e))
        raise Exception(f"Dispatcher resolve failed: {e}")

def bitrate_from_headers(headers):
    for key in ("icy-br", "icy-bitrate", "x-audiocast-bitrate"):
        value = headers.get(key)
        if not value:
            continue
        try:
            return int(str(value).strip().split()[0])
        except ValueError:
            continue
    return None

def inspect_station_stream(station_name):
    with config_lock:
        station = copy.deepcopy(config["stations"].get(station_name))
        settings = normalize_settings(config.get("settings"))
    if not station:
        return {"ok": False, "message": "Station not found"}

    started = time.time()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SoundTouch/1.0)",
        "Accept": "*/*"
    }
    req = urllib.request.Request(station["dispatcherUrl"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            response_headers = {key.lower(): value for key, value in resp.headers.items()}
            detected_kbps = bitrate_from_headers(response_headers)
            sample_bytes = 0
            sample_duration = 0

            if detected_kbps is None:
                sample_started = time.time()
                deadline = sample_started + 2
                while time.time() < deadline and sample_bytes < 128 * 1024:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    sample_bytes += len(chunk)
                sample_duration = max(0.001, time.time() - sample_started)
                if sample_bytes:
                    detected_kbps = round((sample_bytes * 8) / sample_duration / 1000)

            interesting_headers = {}
            for key in (
                "content-type",
                "icy-br",
                "icy-name",
                "icy-description",
                "icy-genre",
                "icy-url",
                "icy-metaint",
                "server",
                "cache-control"
            ):
                if key in response_headers:
                    interesting_headers[key] = response_headers[key]

            stats = get_stream_stats(station_name)
            result = {
                "ok": True,
                "station": station_name,
                "displayName": station.get("name", station_name),
                "dispatcherUrl": station["dispatcherUrl"],
                "playbackMode": "proxy" if settings["audioProxyEnabled"] else "direct",
                "effectiveStreamUrl": f"{BASE_URL.rstrip('/')}/{station_name}" if settings["audioProxyEnabled"] else station["dispatcherUrl"],
                "resolvedUrl": resp.url,
                "status": resp.status,
                "contentType": resp.headers.get("Content-Type", ""),
                "detectedBitrateKbps": detected_kbps,
                "bitrateSource": "headers" if bitrate_from_headers(response_headers) is not None else ("sample" if sample_bytes else "unknown"),
                "sampleBytes": sample_bytes,
                "sampleDurationSeconds": round(sample_duration, 2) if sample_duration else 0,
                "elapsedMs": round((time.time() - started) * 1000),
                "headers": interesting_headers,
                "lastProxyStats": stats
            }
            log("station.diagnostics.ok", station=station_name, status=resp.status, bitrate_kbps=detected_kbps, resolved=resp.url)
            return result
    except Exception as e:
        stats = get_stream_stats(station_name)
        message = str(e)
        log("station.diagnostics.error", station=station_name, message=message)
        return {
            "ok": False,
            "station": station_name,
            "displayName": station.get("name", station_name),
            "dispatcherUrl": station["dispatcherUrl"],
            "playbackMode": "proxy" if settings["audioProxyEnabled"] else "direct",
            "effectiveStreamUrl": f"{BASE_URL.rstrip('/')}/{station_name}" if settings["audioProxyEnabled"] else station["dispatcherUrl"],
            "message": message,
            "lastProxyStats": stats
        }

def station_slug(name):
    slug = []
    for char in name.lower():
        if char.isalnum():
            slug.append(char)
        elif slug and slug[-1] != "_":
            slug.append("_")
    value = "".join(slug).strip("_")
    return value[:48] or "station"

def unique_station_name(name):
    base = station_slug(name)
    candidate = base
    counter = 2
    with config_lock:
        while candidate in config["stations"]:
            candidate = f"{base}_{counter}"
            counter += 1
    return candidate

def fetch_radio_browser_stations(params):
    query = {
        "hidebroken": "true",
        "order": params.get("order", ["clickcount"])[0] or "clickcount",
        "reverse": "true",
        "limit": params.get("limit", ["30"])[0] or "30"
    }

    allowed = {
        "name",
        "countrycode",
        "language",
        "tag",
        "codec",
        "bitrateMin",
        "bitrateMax"
    }
    for key in allowed:
        value = params.get(key, [""])[0].strip()
        if value:
            query[key] = value

    url = f"{RADIO_BROWSER_BASE_URL.rstrip('/')}/json/stations/search?{urlencode(query)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "LoseSound/1.0",
        "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        stations = json.loads(resp.read().decode("utf-8"))

    out = []
    for station in stations:
        stream_url = station.get("url_resolved") or station.get("url")
        if not stream_url:
            continue
        out.append({
            "stationuuid": station.get("stationuuid", ""),
            "name": station.get("name", ""),
            "url": station.get("url", ""),
            "urlResolved": stream_url,
            "homepage": station.get("homepage", ""),
            "favicon": station.get("favicon", ""),
            "tags": station.get("tags", ""),
            "country": station.get("country", ""),
            "countrycode": station.get("countrycode", ""),
            "language": station.get("language", ""),
            "codec": station.get("codec", ""),
            "bitrate": station.get("bitrate", 0),
            "votes": station.get("votes", 0),
            "clickcount": station.get("clickcount", 0),
            "lastcheckok": station.get("lastcheckok", 0)
        })
    log("catalog.search.ok", count=len(out), query=url)
    return out

def xml_text(root, name):
    for node in root.iter():
        tag = node.tag.split("}", 1)[-1]
        if tag == name and node.text:
            return node.text.strip()
    return ""

def speaker_setup_status():
    aftertouch_url = AFTERTOUCH_URL.rstrip("/")
    expected_marge_url = f"{aftertouch_url}/marge"
    device_statuses = [speaker_device_status(ip, expected_marge_url, aftertouch_url) for ip in SPEAKER_IPS]
    target = next((item for item in device_statuses if item["speakerIp"] == SPEAKER_IP), None)
    if not target and device_statuses:
        target = device_statuses[0]

    all_reachable = all(item["speakerReachable"] for item in device_statuses) if device_statuses else False
    all_migrated = all(item["migrated"] for item in device_statuses) if device_statuses else False

    return {
        "status": "ok",
        "speakerIp": SPEAKER_IP,
        "speakerIps": SPEAKER_IPS,
        "devices": device_statuses,
        "speakerReachable": target["speakerReachable"] if target else False,
        "deviceId": target["deviceId"] if target else "",
        "name": target["name"] if target else "",
        "type": target["type"] if target else "",
        "currentMargeUrl": target["currentMargeUrl"] if target else "",
        "expectedMargeUrl": expected_marge_url,
        "aftertouchUrl": aftertouch_url,
        "proxyUrl": BASE_URL.rstrip("/"),
        "migrationUrl": target["migrationUrl"] if target else "",
        "migrated": target["migrated"] if target else False,
        "ready": bool(target and target["speakerReachable"] and target["migrated"] and all_reachable and all_migrated),
        "message": "All configured SoundTouch speakers are migrated to AfterTouch." if all_reachable and all_migrated else "One or more configured SoundTouch speakers need migration or are unreachable."
    }

def speaker_device_status(speaker_ip, expected_marge_url, aftertouch_url):
    status = {
        "speakerIp": speaker_ip,
        "speakerReachable": False,
        "deviceId": "",
        "name": "",
        "type": "",
        "currentMargeUrl": "",
        "expectedMargeUrl": expected_marge_url,
        "migrationUrl": "",
        "migrated": False,
        "message": ""
    }

    if not speaker_ip:
        status["message"] = "SPEAKER_IP is not configured."
        return status

    try:
        body = http_get(f"http://{speaker_ip}:8090/info", timeout=4)
        root = ET.fromstring(body)
    except Exception as e:
        status["message"] = str(e)
        return status

    attrs = {key.lower(): value for key, value in root.attrib.items()}
    device_id = attrs.get("deviceid") or attrs.get("device_id") or xml_text(root, "deviceID") or xml_text(root, "deviceId")
    current_marge_url = xml_text(root, "margeURL")
    status.update({
        "speakerReachable": True,
        "deviceId": device_id or "",
        "name": xml_text(root, "name"),
        "type": xml_text(root, "type"),
        "currentMargeUrl": current_marge_url,
    })

    if device_id:
        query = urlencode({"target_url": aftertouch_url})
        status["migrationUrl"] = f"{aftertouch_url}/setup/migrate/{device_id}?{query}"

    status["migrated"] = current_marge_url.rstrip("/") == expected_marge_url
    status["ready"] = status["speakerReachable"] and status["migrated"]
    if status["ready"]:
        status["message"] = "SoundTouch speaker is migrated to AfterTouch."
    elif current_marge_url:
        status["message"] = "SoundTouch speaker points to a different Marge URL."
    else:
        status["message"] = "SoundTouch speaker is reachable but does not report an AfterTouch Marge URL."
    log("setup.status", speaker=speaker_ip, reachable=status["speakerReachable"], migrated=status["migrated"], current_marge=current_marge_url)
    return status

class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log("http.request", client=self.client_address[0], message=format % args)

    def send_audio_stream(self, station_name):
        station = config["stations"].get(station_name)
        if not station:
            self.send_error(404, "Station not found")
            return

        buffer_seconds, bitrate_kbps, backoff_ms, default_audio_proxy_enabled = stream_settings()
        stream_id = f"{station_name}-{int(time.time() * 1000)}-{threading.get_ident()}"
        buffer_bytes_target = (buffer_seconds * bitrate_kbps * 1000) // 8
        max_chunks = max(8, buffer_bytes_target // 4096)
        ring_buffer = collections.deque(maxlen=max_chunks)
        buffer_lock = threading.Lock()
        producer_done = threading.Event()
        producer_ready = threading.Event()
        bytes_in = 0
        bytes_out = 0
        buffered_bytes = 0
        stream_started_at = time.time()
        last_bytes_in_at = None
        last_bytes_out_at = None
        last_health_at = stream_started_at
        last_health_bytes_in = 0
        last_health_bytes_out = 0
        underrun_count = 0
        underrun_started_at = None
        max_underrun_ms = 0
        overflow_count = 0
        last_overflow_log_at = 0

        def stream_metrics(state=None, now=None):
            now = now or time.time()
            elapsed = max(0.001, now - stream_started_at)
            with buffer_lock:
                buffered_chunks = len(ring_buffer)
                current_buffered_bytes = buffered_bytes
                current_bytes_in = bytes_in
                current_bytes_out = bytes_out
                current_last_bytes_in_at = last_bytes_in_at
                current_last_bytes_out_at = last_bytes_out_at
                current_underrun_started_at = underrun_started_at
                current_max_underrun_ms = max_underrun_ms
                current_underrun_count = underrun_count
                current_overflow_count = overflow_count

            current_underrun_ms = 0
            if current_underrun_started_at is not None:
                current_underrun_ms = round((now - current_underrun_started_at) * 1000)

            metrics = {
                "bytesIn": current_bytes_in,
                "bytesOut": current_bytes_out,
                "bufferedBytes": current_buffered_bytes,
                "bufferedChunks": buffered_chunks,
                "bufferFillPercent": round((current_buffered_bytes / buffer_bytes_target) * 100, 1) if buffer_bytes_target else 0,
                "bufferTargetBytes": buffer_bytes_target,
                "underrunCount": current_underrun_count,
                "currentUnderrunMs": current_underrun_ms,
                "maxUnderrunMs": max(current_max_underrun_ms, current_underrun_ms),
                "overflowCount": current_overflow_count,
                "upstreamReadGapMs": round((now - current_last_bytes_in_at) * 1000) if current_last_bytes_in_at else None,
                "producerBytesPerSecond": round(current_bytes_in / elapsed),
                "consumerBytesPerSecond": round(current_bytes_out / elapsed),
                "lastBytesInAt": int(current_last_bytes_in_at) if current_last_bytes_in_at else None,
                "lastBytesOutAt": int(current_last_bytes_out_at) if current_last_bytes_out_at else None
            }
            if state:
                metrics["state"] = state
            return metrics

        def update_stream_metrics(state=None, **updates):
            metrics = stream_metrics(state)
            metrics.update(updates)
            update_stream_stats(station_name, **metrics)
            update_active_stream(stream_id, **metrics)

        def log_stream_health(now=None):
            nonlocal last_health_at, last_health_bytes_in, last_health_bytes_out
            now = now or time.time()
            if now - last_health_at < 5:
                return
            elapsed = max(0.001, now - last_health_at)
            metrics = stream_metrics(now=now)
            producer_rate = round((metrics["bytesIn"] - last_health_bytes_in) / elapsed)
            consumer_rate = round((metrics["bytesOut"] - last_health_bytes_out) / elapsed)
            log(
                "stream.health",
                stream_id=stream_id,
                station=station_name,
                bytes_in=metrics["bytesIn"],
                bytes_out=metrics["bytesOut"],
                buffered_bytes=metrics["bufferedBytes"],
                buffered_chunks=metrics["bufferedChunks"],
                buffer_fill_percent=metrics["bufferFillPercent"],
                upstream_read_gap_ms=metrics["upstreamReadGapMs"],
                underrun_count=metrics["underrunCount"],
                current_underrun_ms=metrics["currentUnderrunMs"],
                producer_bps=producer_rate,
                consumer_bps=consumer_rate
            )
            last_health_at = now
            last_health_bytes_in = metrics["bytesIn"]
            last_health_bytes_out = metrics["bytesOut"]
            update_stream_metrics()

        log(
            "stream.start",
            stream_id=stream_id,
            station=station_name,
            client=self.client_address[0],
            buffer_target_bytes=buffer_bytes_target,
            buffer_seconds=buffer_seconds,
            bitrate_kbps=bitrate_kbps,
            default_audio_proxy_enabled=default_audio_proxy_enabled,
            max_chunks=max_chunks
        )
        update_stream_stats(
            station_name,
            state="starting",
            client=self.client_address[0],
            startedAt=int(time.time()),
            bytesIn=0,
            bytesOut=0,
            reconnects=0,
            lastError="",
            endedAt=None,
            reconnectHistory=[],
            streamEvents=[],
            lastEvent={},
            lastEventSide="",
            lastEventType="",
            lastEventMessage="",
            bufferedBytes=0,
            bufferedChunks=0,
            bufferFillPercent=0,
            bufferTargetBytes=buffer_bytes_target,
            underrunCount=0,
            currentUnderrunMs=0,
            maxUnderrunMs=0,
            overflowCount=0,
            upstreamReadGapMs=None,
            producerBytesPerSecond=0,
            consumerBytesPerSecond=0,
            lastBytesInAt=None,
            lastBytesOutAt=None
        )
        register_active_stream(stream_id, station_name, self.client_address[0])
        update_active_stream(
            stream_id,
            bufferedBytes=0,
            bufferedChunks=0,
            bufferFillPercent=0,
            bufferTargetBytes=buffer_bytes_target,
            underrunCount=0,
            currentUnderrunMs=0,
            maxUnderrunMs=0,
            overflowCount=0,
            upstreamReadGapMs=None,
            producerBytesPerSecond=0,
            consumerBytesPerSecond=0,
            lastBytesInAt=None,
            lastBytesOutAt=None
        )

        def producer():
            nonlocal bytes_in, buffered_bytes, last_bytes_in_at, overflow_count, last_overflow_log_at
            backoff_idx = 0
            while not producer_done.is_set():
                retry_reason = ""
                try:
                    resolved_url, content_type = resolve_dispatcher(station["dispatcherUrl"])
                    req = urllib.request.Request(resolved_url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; SoundTouch/1.0)",
                        "Accept": "*/*"
                    })
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        backoff_idx = 0
                        log(
                            "stream.upstream.open",
                            stream_id=stream_id,
                            station=station_name,
                            status=resp.status,
                            content_type=resp.headers.get("Content-Type", content_type),
                            resolved=resolved_url
                        )
                        update_stream_stats(
                            station_name,
                            state="upstream-open",
                            upstreamStatus=resp.status,
                            contentType=resp.headers.get("Content-Type", content_type),
                            resolvedUrl=resolved_url,
                            detectedBitrateKbps=bitrate_from_headers({key.lower(): value for key, value in resp.headers.items()}),
                            lastError="",
                            **stream_metrics()
                        )
                        update_active_stream(
                            stream_id,
                            state="upstream-open",
                            upstreamStatus=resp.status,
                            contentType=resp.headers.get("Content-Type", content_type),
                            resolvedUrl=resolved_url,
                            **stream_metrics()
                        )
                        while not producer_done.is_set():
                            try:
                                chunk = resp.read(4096)
                                if not chunk:
                                    retry_reason = "upstream returned EOF"
                                    log("stream.upstream.eof", stream_id=stream_id, station=station_name)
                                    update_stream_stats(station_name, state="upstream-eof", lastError=retry_reason, **stream_metrics())
                                    update_active_stream(
                                        stream_id,
                                        state="upstream-eof",
                                        **stream_metrics(),
                                        lastError=retry_reason,
                                        lastEventSide="upstream",
                                        lastEventType="upstream-eof",
                                        lastEventMessage=retry_reason
                                    )
                                    append_stream_event(station_name, "upstream", "upstream-eof", retry_reason, stream_id)
                                    break
                                dropped_bytes = 0
                                dropped_chunks = 0
                                overflow_event = None
                                now = time.time()
                                with buffer_lock:
                                    if len(ring_buffer) == max_chunks and ring_buffer:
                                        dropped_bytes = len(ring_buffer[0])
                                        buffered_bytes = max(0, buffered_bytes - dropped_bytes)
                                        overflow_count += 1
                                        dropped_chunks = 1
                                        if now - last_overflow_log_at >= 5:
                                            last_overflow_log_at = now
                                            overflow_event = overflow_count
                                    ring_buffer.append(chunk)
                                    bytes_in += len(chunk)
                                    buffered_bytes += len(chunk)
                                    last_bytes_in_at = now
                                    buffered = len(ring_buffer)
                                if dropped_chunks:
                                    overflow_message = "ring buffer full; oldest audio chunk dropped"
                                    if overflow_event:
                                        log(
                                            "stream.buffer.overflow",
                                            stream_id=stream_id,
                                            station=station_name,
                                            dropped_bytes=dropped_bytes,
                                            overflow_count=overflow_event,
                                            buffered_chunks=buffered
                                        )
                                        append_stream_event(
                                            station_name,
                                            "proxy",
                                            "buffer-overflow",
                                            overflow_message,
                                            stream_id,
                                            droppedBytes=dropped_bytes,
                                            overflowCount=overflow_event,
                                            bufferedChunks=buffered
                                        )
                                update_stream_metrics()
                                producer_ready.set()
                                if LOG_STREAM_CHUNKS and bytes_in % (256 * 1024) < len(chunk):
                                    log("stream.producer.bytes", stream_id=stream_id, bytes_in=bytes_in, buffered_chunks=buffered)
                            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                                retry_reason = str(e)
                                log("stream.upstream.read_error", stream_id=stream_id, station=station_name, message=retry_reason)
                                update_stream_stats(station_name, state="upstream-read-error", lastError=retry_reason, **stream_metrics())
                                update_active_stream(
                                    stream_id,
                                    state="upstream-read-error",
                                    **stream_metrics(),
                                    lastError=retry_reason,
                                    lastEventSide="upstream",
                                    lastEventType="upstream-read-error",
                                    lastEventMessage=retry_reason
                                )
                                append_stream_event(station_name, "upstream", "upstream-read-error", retry_reason, stream_id)
                                break
                except Exception as e:
                    retry_reason = str(e)
                    log("stream.upstream.error", stream_id=stream_id, station=station_name, message=retry_reason)
                    update_stream_stats(station_name, state="upstream-error", lastError=retry_reason, **stream_metrics())
                    update_active_stream(
                        stream_id,
                        state="upstream-error",
                        **stream_metrics(),
                        lastError=retry_reason,
                        lastEventSide="upstream",
                        lastEventType="upstream-error",
                        lastEventMessage=retry_reason
                    )
                    append_stream_event(station_name, "upstream", "upstream-error", retry_reason, stream_id)

                if producer_done.is_set():
                    break

                backoff = backoff_ms[min(backoff_idx, len(backoff_ms) - 1)] / 1000.0
                backoff_idx += 1
                log("stream.upstream.retry", stream_id=stream_id, station=station_name, backoff_seconds=backoff)
                with stream_stats_lock:
                    if stream_id in active_streams:
                        reconnects = active_streams[stream_id].get("reconnects", 0) + 1
                    else:
                        reconnects = 0
                    reconnect_event = {
                        "at": int(time.time()),
                        "side": "upstream",
                        "reason": retry_reason or stream_stats.get(station_name, {}).get("lastError", "") or "upstream reconnect",
                        "backoffSeconds": backoff
                    }
                    stats = stream_stats.setdefault(station_name, {})
                    history = list(stats.get("reconnectHistory", []))
                    history.append(reconnect_event)
                    if len(history) > 20:
                        history = history[-20:]
                    stats["reconnectHistory"] = history
                update_active_stream(
                    stream_id,
                    state="reconnecting",
                    **stream_metrics(),
                    reconnects=reconnects,
                    backoffSeconds=backoff,
                    lastEventSide="upstream",
                    lastEventType="upstream-retry",
                    lastEventMessage=reconnect_event["reason"]
                )
                update_stream_stats(station_name, state="reconnecting", reconnects=reconnects, backoffSeconds=backoff, **stream_metrics())

                start_time = time.time()
                while time.time() - start_time < backoff:
                    if producer_done.is_set():
                        break
                    time.sleep(0.05)

            producer_done.set()

        producer_thread = threading.Thread(target=producer, daemon=True)
        producer_thread.start()

        if not producer_ready.wait(timeout=10):
            producer_done.set()
            producer_thread.join(timeout=1)
            error_message = "upstream did not produce data before timeout"
            log("stream.error", stream_id=stream_id, station=station_name, message=error_message)
            update_stream_stats(station_name, state="error", lastError=error_message, **stream_metrics())
            update_active_stream(
                stream_id,
                state="error",
                **stream_metrics(),
                lastError=error_message,
                lastEventSide="proxy",
                lastEventType="upstream-timeout",
                lastEventMessage=error_message
            )
            append_stream_event(station_name, "proxy", "upstream-timeout", error_message, stream_id)
            self.send_error(502, "Upstream stream did not produce data")
            unregister_active_stream(stream_id)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        log("stream.response.sent", stream_id=stream_id, station=station_name)
        update_stream_metrics("streaming")

        try:
            while True:
                time.sleep(0.1)
                now = time.time()
                chunk = None
                underrun_start_event = False
                underrun_end_ms = None
                buffered_after_pop = 0
                with buffer_lock:
                    if ring_buffer:
                        chunk = ring_buffer.popleft()
                        buffered_bytes = max(0, buffered_bytes - len(chunk))
                        buffered_after_pop = len(ring_buffer)
                        if underrun_started_at is not None:
                            underrun_end_ms = round((now - underrun_started_at) * 1000)
                            max_underrun_ms = max(max_underrun_ms, underrun_end_ms)
                            underrun_started_at = None
                    elif producer_done.is_set():
                        break
                    else:
                        if underrun_started_at is None:
                            underrun_started_at = now
                            underrun_count += 1
                            underrun_start_event = True
                        buffered_after_pop = 0

                if chunk:
                    if underrun_end_ms is not None:
                        log(
                            "stream.buffer.underrun.end",
                            stream_id=stream_id,
                            station=station_name,
                            duration_ms=underrun_end_ms,
                            buffered_chunks=buffered_after_pop
                        )
                        append_stream_event(
                            station_name,
                            "proxy",
                            "buffer-underrun-end",
                            f"buffer refilled after {underrun_end_ms}ms",
                            stream_id,
                            durationMs=underrun_end_ms,
                            bufferedChunks=buffered_after_pop
                        )
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        with buffer_lock:
                            bytes_out += len(chunk)
                            last_bytes_out_at = time.time()
                            current_bytes_out = bytes_out
                            current_buffered = len(ring_buffer)
                        update_stream_metrics("streaming")
                        if LOG_STREAM_CHUNKS and current_bytes_out % (256 * 1024) < len(chunk):
                            log("stream.consumer.bytes", stream_id=stream_id, bytes_out=current_bytes_out, buffered_chunks=current_buffered)
                    except (BrokenPipeError, ConnectionResetError) as e:
                        disconnect_message = f"{type(e).__name__}: downstream client closed the connection"
                        log("stream.client.disconnect", stream_id=stream_id, station=station_name, bytes_out=bytes_out, message=disconnect_message)
                        update_stream_stats(station_name, state="client-disconnect", lastClientDisconnect=disconnect_message, **stream_metrics())
                        update_active_stream(
                            stream_id,
                            state="client-disconnect",
                            **stream_metrics(),
                            lastClientDisconnect=disconnect_message,
                            lastEventSide="client",
                            lastEventType="client-disconnect",
                            lastEventMessage=disconnect_message
                        )
                        append_stream_event(station_name, "client", "client-disconnect", disconnect_message, stream_id, bytesOut=bytes_out)
                        break
                elif underrun_start_event:
                    underrun_message = "proxy buffer is empty while upstream producer is still active"
                    log(
                        "stream.buffer.underrun.start",
                        stream_id=stream_id,
                        station=station_name,
                        underrun_count=underrun_count,
                        upstream_read_gap_ms=stream_metrics().get("upstreamReadGapMs")
                    )
                    update_stream_metrics(
                        "buffer-underrun",
                        lastEventSide="proxy",
                        lastEventType="buffer-underrun-start",
                        lastEventMessage=underrun_message
                    )
                    append_stream_event(
                        station_name,
                        "proxy",
                        "buffer-underrun-start",
                        underrun_message,
                        stream_id,
                        underrunCount=underrun_count,
                        upstreamReadGapMs=stream_metrics().get("upstreamReadGapMs")
                    )
                log_stream_health(now)
        except (BrokenPipeError, ConnectionResetError) as e:
            disconnect_message = f"{type(e).__name__}: downstream client closed the connection"
            log("stream.client.disconnect", stream_id=stream_id, station=station_name, bytes_out=bytes_out, message=disconnect_message)
            update_stream_stats(station_name, state="client-disconnect", lastClientDisconnect=disconnect_message, **stream_metrics())
            update_active_stream(
                stream_id,
                state="client-disconnect",
                **stream_metrics(),
                lastClientDisconnect=disconnect_message,
                lastEventSide="client",
                lastEventType="client-disconnect",
                lastEventMessage=disconnect_message
            )
            append_stream_event(station_name, "client", "client-disconnect", disconnect_message, stream_id, bytesOut=bytes_out)
        finally:
            producer_done.set()
            producer_thread.join(timeout=1)
            log("stream.end", stream_id=stream_id, station=station_name, bytes_in=bytes_in, bytes_out=bytes_out)
            update_stream_stats(station_name, state="ended", endedAt=int(time.time()), **stream_metrics())
            unregister_active_stream(stream_id)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        log("request.get", path=path or "/", client=self.client_address[0], user_agent=self.headers.get("User-Agent", "-"))

        if path == "/" or path == "":
            self.serve_index()
        elif path == "/api/settings":
            self.handle_api_settings(path)
        elif path == "/api/catalog/search":
            self.handle_catalog_search(parsed.query)
        elif path == "/api/proxy/status":
            self.send_json(proxy_status())
        elif path == "/api/setup/status":
            self.send_json(speaker_setup_status())
        elif path.startswith("/api/stations/") and path.endswith("/diagnostics"):
            self.handle_station_diagnostics(path)
        elif path.startswith("/api/stations"):
            self.handle_api_stations(path)
        elif path.endswith(".json") and not path.startswith("/api/"):
            station_name = path.lstrip("/").replace(".json", "")
            self.serve_station_json(station_name)
        elif path.startswith("/api/stations/") and "/preset/" in path:
            self.handle_preset_api(path)
        else:
            station_name = path.lstrip("/")
            if station_name in config["stations"]:
                self.send_audio_stream(station_name)
            else:
                self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        log("request.post", path=path or "/", client=self.client_address[0], user_agent=self.headers.get("User-Agent", "-"))

        if path.startswith("/api/stations/") and "/preset/" in path:
            self.handle_preset_api(path)
        elif path == "/api/stations" or path == "/api/stations/":
            self.handle_api_stations(path)
        elif path == "/api/catalog/add":
            self.handle_catalog_add()
        elif path == "/api/settings":
            self.handle_api_settings(path)
        else:
            self.send_error(404, "Not found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/stations/"):
            parts = path.replace("/api/stations/", "").split("/")
            if len(parts) == 1 and parts[0]:
                self.delete_station(parts[0])
            else:
                self.send_error(404, "Not found")
        else:
            self.send_error(404, "Not found")

    def serve_index(self):
        index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
        if os.path.exists(index_path):
            with open(index_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "index.html not found")

    def handle_api_stations(self, path):
        if path == "/api/stations" or path == "/api/stations/":
            if self.command == "GET":
                with config_lock:
                    stations_out = []
                    for name, data in config["stations"].items():
                        stations_out.append({
                            "name": name,
                            "displayName": data["name"],
                            "dispatcherUrl": data["dispatcherUrl"],
                            "presets": data["presets"],
                            "presetModes": data.get("presetModes", {})
                        })
                self.send_json(stations_out)
            elif self.command == "POST":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    name = data.get("name", "").lower().replace(" ", "_")
                    if not name:
                        self.send_error(400, "Name required")
                        return
                    with config_lock:
                        config["stations"][name] = {
                            "name": data.get("displayName", data.get("name", name)),
                            "dispatcherUrl": data.get("dispatcherUrl", ""),
                            "presets": data.get("presets", []),
                            "presetModes": data.get("presetModes", {})
                        }
                        save_config()
                    log("station.add", station=name, display_name=config["stations"][name]["name"])
                    self.send_json({"status": "ok", "name": name})
                except json.JSONDecodeError:
                    self.send_error(400, "Invalid JSON")
            else:
                self.send_error(405, "Method not allowed")
        else:
            self.send_error(404, "Not found")

    def handle_api_settings(self, path):
        if path != "/api/settings":
            self.send_error(404, "Not found")
            return

        if self.command == "GET":
            with config_lock:
                settings = normalize_settings(config.get("settings"))
            self.send_json(settings)
            return

        if self.command != "POST":
            self.send_error(405, "Method not allowed")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        settings = normalize_settings(data)
        with config_lock:
            config["settings"] = settings
            save_config()
        log(
            "settings.update",
            buffer_seconds=settings["bufferSeconds"],
            bitrate_kbps=settings["bitrateKbps"],
            reconnect_backoff_ms=",".join(str(value) for value in settings["reconnectBackoffMs"]),
            audio_proxy_enabled=settings["audioProxyEnabled"]
        )
        self.send_json({"status": "ok", "settings": settings})

    def handle_station_diagnostics(self, path):
        parts = path.replace("/api/stations/", "").split("/")
        if len(parts) != 2 or parts[1] != "diagnostics":
            self.send_error(404, "Not found")
            return
        if self.command != "GET":
            self.send_error(405, "Method not allowed")
            return

        station_name = parts[0]
        if station_name not in config["stations"]:
            self.send_error(404, "Station not found")
            return

        self.send_json(inspect_station_stream(station_name))

    def handle_catalog_search(self, query_string):
        try:
            stations = fetch_radio_browser_stations(parse_qs(query_string))
            self.send_json({"status": "ok", "stations": stations})
        except Exception as e:
            log("catalog.search.error", message=str(e))
            self.send_json({"status": "error", "message": str(e), "stations": []})

    def handle_catalog_add(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        display_name = data.get("displayName") or data.get("name") or "Station"
        dispatcher_url = data.get("dispatcherUrl") or data.get("urlResolved") or data.get("url")
        if not dispatcher_url:
            self.send_error(400, "Missing dispatcher URL")
            return

        with config_lock:
            name = unique_station_name(display_name)
            config["stations"][name] = {
                "name": display_name,
                "dispatcherUrl": dispatcher_url,
                "presets": [],
                "presetModes": {}
            }
            save_config()
        log("catalog.station.add", station=name, display_name=display_name)

        slot = data.get("preset")
        if isinstance(slot, int) and 1 <= slot <= 6:
            playback_mode = normalize_playback_mode(data.get("playbackMode"))
            result = set_preset_on_speaker(name, slot, playback_mode)
            if result["ok"]:
                with config_lock:
                    for station in config["stations"].values():
                        if slot in station["presets"]:
                            station["presets"].remove(slot)
                        station.setdefault("presetModes", {}).pop(str(slot), None)
                    config["stations"][name]["presets"].append(slot)
                    config["stations"][name].setdefault("presetModes", {})[str(slot)] = result.get("playbackMode", playback_mode)
                    save_config()
                self.send_json({
                    "status": "ok",
                    "name": name,
                    "preset": slot,
                    "playbackMode": result.get("playbackMode", playback_mode),
                    "location": result.get("location", "")
                })
            else:
                self.send_json({"status": "error", "name": name, "message": result.get("message", "Unknown speaker error")})
            return

        self.send_json({"status": "ok", "name": name})

    def handle_preset_api(self, path):
        parts = path.replace("/api/stations/", "").split("/")
        if len(parts) == 3 and parts[1] == "preset":
            station_name = parts[0]
            slot_str = parts[2]
            if not slot_str or not slot_str.isdigit():
                self.send_error(400, "Invalid preset slot")
                return
            slot = int(slot_str)
            if slot < 1 or slot > 6:
                self.send_error(400, "Preset must be 1-6")
                return

            if station_name not in config["stations"]:
                self.send_error(404, "Station not found")
                return

            if self.command == "POST":
                playback_mode = None
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    body = self.rfile.read(length)
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        self.send_error(400, "Invalid JSON")
                        return
                    playback_mode = data.get("playbackMode")
                playback_mode = normalize_playback_mode(playback_mode)

                result = set_preset_on_speaker(station_name, slot, playback_mode)
                if result["ok"]:
                    with config_lock:
                        for station in config["stations"].values():
                            if slot in station["presets"]:
                                station["presets"].remove(slot)
                            station.setdefault("presetModes", {}).pop(str(slot), None)
                        station = config["stations"][station_name]
                        station["presets"].append(slot)
                        station.setdefault("presetModes", {})[str(slot)] = result.get("playbackMode", playback_mode)
                        save_config()

                    self.send_json({
                        "status": "ok",
                        "preset": slot,
                        "station": station_name,
                        "playbackMode": result.get("playbackMode", playback_mode),
                        "location": result.get("location", ""),
                        "speaker_response": result.get("body", "")[:200] or "ok"
                    })
                else:
                    self.send_json({"status": "error", "message": result.get("message", "Unknown speaker error")})
            else:
                self.send_error(405, "Method not allowed")
        else:
            self.send_error(404, "Not found")

    def delete_station(self, name):
        if name not in config["stations"]:
            self.send_error(404, "Station not found")
            return
        with config_lock:
            del config["stations"][name]
            save_config()
        log("station.delete", station=name)
        self.send_json({"status": "ok", "deleted": name})

    def serve_station_json(self, station_name):
        with config_lock:
            if station_name not in config["stations"]:
                self.send_error(404, "Station not found")
                return
            station_json = get_station_json(station_name)
        log("station.json", station=station_name, stream_url=station_json["streamUrl"], client=self.client_address[0])
        content = json.dumps(station_json).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data):
        content = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

def main():
    os.makedirs("/data", exist_ok=True)
    load_config()
    port = int(os.getenv("LISTEN_PORT", "8092"))
    server = ThreadingHTTPServer(("0.0.0.0", port), StreamHandler)
    buffer_seconds, bitrate_kbps, backoff_ms, default_audio_proxy_enabled = stream_settings()
    log(
        "proxy.start",
        port=port,
        base_url=BASE_URL,
        speaker_ip=SPEAKER_IP,
        buffer_seconds=buffer_seconds,
        bitrate_kbps=bitrate_kbps,
        reconnect_backoff_ms=",".join(str(value) for value in backoff_ms),
        default_audio_proxy_enabled=default_audio_proxy_enabled
    )
    server.serve_forever()

if __name__ == "__main__":
    main()
