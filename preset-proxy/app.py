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
    "reconnectBackoffMs": parse_backoff_ms(os.getenv("RECONNECT_BACKOFF_MS", "250,500,1000,2000,3000"))
}
DEFAULT_CONFIG = {
    "settings": DEFAULT_SETTINGS.copy(),
    "stations": {
        "hr3": {
            "name": "HR3",
            "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/hr3/live/mp3/high",
            "presets": []
        },
        "youfm": {
            "name": "YOU FM",
            "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/youfm/live/mp3/high",
            "presets": []
        }
    }
}

config_lock = threading.RLock()
config = None
stream_stats_lock = threading.Lock()
stream_stats = {}

SPEAKER_IP = os.getenv("SPEAKER_IP", "")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8092")
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

def update_stream_stats(station_name, **updates):
    with stream_stats_lock:
        stats = stream_stats.setdefault(station_name, {})
        stats.update(updates)
        stats["updatedAt"] = int(time.time())

def get_stream_stats(station_name):
    with stream_stats_lock:
        return copy.deepcopy(stream_stats.get(station_name, {}))

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

    return {
        "bufferSeconds": bounded_int("bufferSeconds", DEFAULT_SETTINGS["bufferSeconds"], 1, 30),
        "bitrateKbps": bounded_int("bitrateKbps", DEFAULT_SETTINGS["bitrateKbps"], 32, 320),
        "reconnectBackoffMs": parse_backoff_ms(data.get("reconnectBackoffMs", DEFAULT_SETTINGS["reconnectBackoffMs"]))
    }

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

    for station in data["stations"].values():
        if "presets" not in station or not isinstance(station["presets"], list):
            station["presets"] = []
            changed = True

    return data, changed

def stream_settings():
    with config_lock:
        settings = normalize_settings(config.get("settings", {}))
    return (
        settings["bufferSeconds"],
        settings["bitrateKbps"],
        settings["reconnectBackoffMs"]
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
    stream_url = f"{base}/{station_name}"
    return {
        "audio": {
            "hasPlaylist": False,
            "isRealtime": True,
            "streamUrl": stream_url
        },
        "imageUrl": "",
        "isRealtime": True,
        "name": config["stations"][station_name]["name"],
        "streamUrl": stream_url,
        "streamType": "liveRadio"
    }

def set_preset_on_speaker(station_name, slot):
    if not SPEAKER_IP:
        return {
            "ok": False,
            "message": "SPEAKER_IP is not configured. Run scripts/setup.py or set SPEAKER_IP in .env."
        }

    base = BASE_URL.rstrip("/")
    station = config["stations"][station_name]
    location = f"{base}/{station_name}.json"
    now = int(time.time())
    preset_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<preset id="{slot}" createdOn="{now}" updatedOn="{now}">
  <ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location={quoteattr(location)} sourceAccount="" isPresetable="true">
    <itemName>{escape(station['name'])}</itemName>
  </ContentItem>
</preset>"""

    url = f"http://{SPEAKER_IP}:8090/storePreset"
    log("preset.store.start", station=station_name, slot=slot, speaker=SPEAKER_IP, location=location)
    try:
        req = urllib.request.Request(url, data=preset_xml.encode("utf-8"))
        req.add_header("Content-Type", "application/xml")
        req.add_header("Accept", "application/xml")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            log("preset.store.ok", station=station_name, slot=slot, status=resp.status, bytes=len(body))
            return {
                "ok": True,
                "status": resp.status,
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

class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log("http.request", client=self.client_address[0], message=format % args)

    def send_audio_stream(self, station_name):
        station = config["stations"].get(station_name)
        if not station:
            self.send_error(404, "Station not found")
            return

        buffer_seconds, bitrate_kbps, backoff_ms = stream_settings()
        stream_id = f"{station_name}-{int(time.time() * 1000)}-{threading.get_ident()}"
        buffer_bytes_target = (buffer_seconds * bitrate_kbps * 1000) // 8
        max_chunks = max(8, buffer_bytes_target // 4096)
        ring_buffer = collections.deque(maxlen=max_chunks)
        buffer_lock = threading.Lock()
        producer_done = threading.Event()
        producer_ready = threading.Event()
        bytes_in = 0
        bytes_out = 0

        log(
            "stream.start",
            stream_id=stream_id,
            station=station_name,
            client=self.client_address[0],
            buffer_target_bytes=buffer_bytes_target,
            buffer_seconds=buffer_seconds,
            bitrate_kbps=bitrate_kbps,
            max_chunks=max_chunks
        )
        update_stream_stats(
            station_name,
            state="starting",
            client=self.client_address[0],
            bytesIn=0,
            bytesOut=0,
            lastError=""
        )

        def producer():
            nonlocal bytes_in
            backoff_idx = 0
            while not producer_done.is_set():
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
                            lastError=""
                        )
                        while not producer_done.is_set():
                            try:
                                chunk = resp.read(4096)
                                if not chunk:
                                    log("stream.upstream.eof", stream_id=stream_id, station=station_name)
                                    break
                                with buffer_lock:
                                    ring_buffer.append(chunk)
                                    bytes_in += len(chunk)
                                    buffered = len(ring_buffer)
                                update_stream_stats(station_name, state="buffering", bytesIn=bytes_in, bufferedChunks=buffered)
                                producer_ready.set()
                                if LOG_STREAM_CHUNKS and bytes_in % (256 * 1024) < len(chunk):
                                    log("stream.producer.bytes", stream_id=stream_id, bytes_in=bytes_in, buffered_chunks=buffered)
                            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                                log("stream.upstream.read_error", stream_id=stream_id, station=station_name, message=str(e))
                                update_stream_stats(station_name, state="upstream-read-error", lastError=str(e))
                                break
                except Exception as e:
                    log("stream.upstream.error", stream_id=stream_id, station=station_name, message=str(e))
                    update_stream_stats(station_name, state="upstream-error", lastError=str(e))

                if producer_done.is_set():
                    break

                backoff = backoff_ms[min(backoff_idx, len(backoff_ms) - 1)] / 1000.0
                backoff_idx += 1
                log("stream.upstream.retry", stream_id=stream_id, station=station_name, backoff_seconds=backoff)

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
            log("stream.error", stream_id=stream_id, station=station_name, message="upstream did not produce data before timeout")
            update_stream_stats(station_name, state="error", lastError="upstream did not produce data before timeout")
            self.send_error(502, "Upstream stream did not produce data")
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        log("stream.response.sent", stream_id=stream_id, station=station_name)
        update_stream_stats(station_name, state="streaming")

        try:
            while True:
                time.sleep(0.1)
                with buffer_lock:
                    if ring_buffer:
                        chunk = ring_buffer.popleft()
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            bytes_out += len(chunk)
                            update_stream_stats(station_name, state="streaming", bytesOut=bytes_out)
                            if LOG_STREAM_CHUNKS and bytes_out % (256 * 1024) < len(chunk):
                                log("stream.consumer.bytes", stream_id=stream_id, bytes_out=bytes_out, buffered_chunks=len(ring_buffer))
                        except (BrokenPipeError, ConnectionResetError):
                            log("stream.client.disconnect", stream_id=stream_id, station=station_name, bytes_out=bytes_out)
                            update_stream_stats(station_name, state="client-disconnect", bytesIn=bytes_in, bytesOut=bytes_out)
                            break
                    elif producer_done.is_set():
                        break
        except (BrokenPipeError, ConnectionResetError):
            log("stream.client.disconnect", stream_id=stream_id, station=station_name, bytes_out=bytes_out)
            update_stream_stats(station_name, state="client-disconnect", bytesIn=bytes_in, bytesOut=bytes_out)
        finally:
            producer_done.set()
            producer_thread.join(timeout=1)
            log("stream.end", stream_id=stream_id, station=station_name, bytes_in=bytes_in, bytes_out=bytes_out)
            update_stream_stats(station_name, state="ended", bytesIn=bytes_in, bytesOut=bytes_out)

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
                            "presets": data["presets"]
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
                            "presets": data.get("presets", [])
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
            reconnect_backoff_ms=",".join(str(value) for value in settings["reconnectBackoffMs"])
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
                "presets": []
            }
            save_config()
        log("catalog.station.add", station=name, display_name=display_name)

        slot = data.get("preset")
        if isinstance(slot, int) and 1 <= slot <= 6:
            result = set_preset_on_speaker(name, slot)
            if result["ok"]:
                with config_lock:
                    for station in config["stations"].values():
                        if slot in station["presets"]:
                            station["presets"].remove(slot)
                    config["stations"][name]["presets"].append(slot)
                    save_config()
                self.send_json({"status": "ok", "name": name, "preset": slot})
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
                result = set_preset_on_speaker(station_name, slot)
                if result["ok"]:
                    with config_lock:
                        for station in config["stations"].values():
                            if slot in station["presets"]:
                                station["presets"].remove(slot)
                        station = config["stations"][station_name]
                        station["presets"].append(slot)
                        save_config()

                    self.send_json({
                        "status": "ok",
                        "preset": slot,
                        "station": station_name,
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
    buffer_seconds, bitrate_kbps, backoff_ms = stream_settings()
    log(
        "proxy.start",
        port=port,
        base_url=BASE_URL,
        speaker_ip=SPEAKER_IP,
        buffer_seconds=buffer_seconds,
        bitrate_kbps=bitrate_kbps,
        reconnect_backoff_ms=",".join(str(value) for value in backoff_ms)
    )
    server.serve_forever()

if __name__ == "__main__":
    main()
