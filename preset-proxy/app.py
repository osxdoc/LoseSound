import os
import json
import threading
import time
import collections
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

CONFIG_PATH = "/data/config.json"
DEFAULT_CONFIG = {
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

SPEAKER_IP = os.getenv("SPEAKER_IP", "")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8092")
BUFFER_SECONDS = int(os.getenv("BUFFER_SECONDS", "3"))
BITRATE_KBPS = int(os.getenv("BITRATE_KBPS", "128"))
BACKOFF_MS = [int(x) for x in os.getenv("RECONNECT_BACKOFF_MS", "250,500,1000,2000,3000").split(",")]

def load_config():
    global config
    with config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    config = json.load(f)
            except (json.JSONDecodeError, IOError):
                config = DEFAULT_CONFIG.copy()
                save_config()
        else:
            config = DEFAULT_CONFIG.copy()
            save_config()

def save_config():
    with config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)

def get_station_json(station_name):
    base = BASE_URL.rstrip("/")
    return {
        "audio": {
            "hasPlaylist": False,
            "isRealtime": True,
            "streamUrl": f"{base}/{station_name}"
        },
        "name": config["stations"][station_name]["name"],
        "streamType": "liveRadio"
    }

def set_preset_on_speaker(station_name, slot):
    if not SPEAKER_IP:
        return "SPEAKER_IP is not configured. Run scripts/setup.py or set SPEAKER_IP in .env."

    base = BASE_URL.rstrip("/")
    station = config["stations"][station_name]
    preset_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<preset>
  <ContentItem type="LOCAL_INTERNET_RADIO">
    <itemName>{station['name']}</itemName>
    <sourceAccount></sourceAccount>
    <source>INTERNET_RADIO</source>
    <location>{base}/{station_name}.json</location>
    <playSpec>
      <ondemand>0</ondemand>
    </playSpec>
  </ContentItem>
</preset>"""

    url = f"http://{SPEAKER_IP}:8090/storePreset?preset={slot}"
    try:
        req = urllib.request.Request(url, data=preset_xml.encode("utf-8"))
        req.add_header("Content-Type", "application/xml")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return str(e)

def resolve_dispatcher(dispatcher_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SoundTouch/1.0)",
        "Accept": "*/*"
    }
    req = urllib.request.Request(dispatcher_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url, resp.headers.get("Content-Type", "audio/mpeg")
    except Exception as e:
        raise Exception(f"Dispatcher resolve failed: {e}")

class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_audio_stream(self, station_name):
        station = config["stations"].get(station_name)
        if not station:
            self.send_error(404, "Station not found")
            return

        buffer_size = (BUFFER_SECONDS * BITRATE_KBPS * 1000) // 8
        ring_buffer = collections.deque(maxlen=buffer_size)
        buffer_lock = threading.Lock()
        producer_done = threading.Event()
        consumer_waiting = threading.Event()

        def producer():
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
                        while not producer_done.is_set():
                            try:
                                chunk = resp.read(4096)
                                if not chunk:
                                    break
                                with buffer_lock:
                                    ring_buffer.append(chunk)
                            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                                break
                except Exception:
                    pass

                if producer_done.is_set():
                    break

                backoff = BACKOFF_MS[min(backoff_idx, len(BACKOFF_MS) - 1)] / 1000.0
                backoff_idx += 1

                start_time = time.time()
                while time.time() - start_time < backoff:
                    if producer_done.is_set():
                        break
                    time.sleep(0.05)

            producer_done.set()

        producer_thread = threading.Thread(target=producer, daemon=True)
        producer_thread.start()

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                time.sleep(0.1)
                with buffer_lock:
                    if ring_buffer:
                        chunk = ring_buffer.popleft()
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    elif producer_done.is_set():
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            producer_done.set()
            producer_thread.join(timeout=1)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/" or path == "":
            self.serve_index()
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

        if path.startswith("/api/stations/") and "/preset/" in path:
            self.handle_preset_api(path)
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
                    self.send_json({"status": "ok", "name": name})
                except json.JSONDecodeError:
                    self.send_error(400, "Invalid JSON")
            else:
                self.send_error(405, "Method not allowed")
        else:
            self.send_error(404, "Not found")

    def handle_preset_api(self, path):
        parts = path.replace("/api/stations/", "").split("/")
        if len(parts) >= 3 and parts[2] == "preset":
            station_name = parts[0]
            slot_str = parts[3] if len(parts) > 3 else None
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
                with config_lock:
                    station = config["stations"][station_name]
                    if slot not in station["presets"]:
                        station["presets"].append(slot)
                    save_config()

                result = set_preset_on_speaker(station_name, slot)
                if "error" in result.lower() or "Exception" in result:
                    self.send_json({"status": "error", "message": result})
                else:
                    self.send_json({"status": "ok", "preset": slot, "station": station_name, "speaker_response": result[:200] if result else "ok"})
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
        self.send_json({"status": "ok", "deleted": name})

    def serve_station_json(self, station_name):
        with config_lock:
            if station_name not in config["stations"]:
                self.send_error(404, "Station not found")
                return
            station_json = get_station_json(station_name)
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
    server = HTTPServer(("0.0.0.0", port), StreamHandler)
    print(f"Preset-Proxy läuft auf Port {port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
