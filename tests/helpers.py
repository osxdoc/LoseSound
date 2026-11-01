"""Test doubles for the speaker, AfterTouch, radio-browser and a stream origin."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

from losesound.config import ConfigStore, default_config
from losesound.http_server import AppContext, create_server


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - matches BaseHTTPRequestHandler
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(length) if length else b""


class _BaseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ServerContext:
    def __init__(self) -> None:
        self.httpd: _BaseServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, handler_class, server_class=_BaseServer):
        self.httpd = server_class(("127.0.0.1", 0), handler_class)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


# ------------------------------------------------------------------ speaker
class _SpeakerServer(_BaseServer):
    speaker: FakeSpeaker


class _SpeakerHandler(_QuietHandler):
    @property
    def state(self) -> dict:
        return self.server.speaker.state  # type: ignore[attr-defined]

    def _send(self, status: int, body: str, content_type: str = "application/xml") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - HTTP verb
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/info":
            marge = self.state.get("marge_url") or ""
            body = (
                f"<info deviceID={quoteattr(self.state['device_id'])}>"
                f"<name>{escape(self.state['name'])}</name>"
                f"<type>{escape(self.state['type'])}</type>"
                f"<margeURL>{escape(marge)}</margeURL>"
                "<components><component><componentCategory>SCM</componentCategory>"
                f"<softwareVersion>{escape(self.state['firmware'])}</softwareVersion></component></components>"
                "</info>"
            )
            self._send(200, body)
        elif path == "/sources":
            items = []
            for source in self.state["sources"]:
                items.append(
                    f'<sourceItem source="{source}" status="{self.state.get("source_status", "READY")}" '
                    f'isLocal="false" multiroomallowed="true">{source}</sourceItem>'
                )
            self._send(200, '<sources deviceID="DEV1">' + "".join(items) + "</sources>")
        elif path == "/presets":
            presets = []
            for slot, item in sorted(self.state["presets"].items()):
                presets.append(
                    f'<preset id="{slot}"><ContentItem source={quoteattr(item["source"])} '
                    f"type={quoteattr(item.get('type', 'stationurl'))} location={quoteattr(item['location'])} "
                    f'sourceAccount="" isPresetable="true"><itemName>{escape(item.get("name", ""))}</itemName>'
                    "</ContentItem></preset>"
                )
            self._send(200, "<presets>" + "".join(presets) + "</presets>")
        elif path == "/now_playing":
            if self.state.get("inactive"):
                self._send(
                    200,
                    '<errors deviceID="DEV1"><error value="1043" name="EVENT_IN_WRONG_STATE">NowPlaying not supported in current state: Inactive</error></errors>',
                )
                return
            now = self.state.get("now")
            if not now:
                self._send(200, '<nowPlaying source="" playStatus="STOP_STATE"/>')
                return
            self._send(
                200,
                '<nowPlaying source={source} sourceAccount=""><ContentItem source={source} type="stationurl" '
                'location={location} sourceAccount="" isPresetable="true"><itemName>{name}</itemName></ContentItem>'
                "<stationName>{name}</stationName><track>{name}</track>"
                "<playStatus>PLAY_STATE</playStatus><streamType>RADIO_STREAMING</streamType></nowPlaying>".format(
                    source=quoteattr(now["source"]),
                    location=quoteattr(now["location"]),
                    name=escape(now.get("name", "")),
                ),
            )
        elif path.startswith("/marge"):
            status = int(self.state.get("marge_status", 200))
            self._send(status, "<marge/>")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):  # noqa: N802 - HTTP verb
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        body = self._body()
        self.state.setdefault("requests", []).append((path, body))
        try:
            root = ET.fromstring(body or b"<x/>")
        except ET.ParseError:
            self._send(400, "invalid xml", "text/plain")
            return

        if path == "/key":
            key = (root.text or "").strip()
            self.state.setdefault("keys", []).append((root.attrib.get("state", ""), key))
            if key == "POWER":
                self.state["inactive"] = False
            self._send(200, "<status>/key</status>")
        elif path == "/storePreset":
            if self.state.get("store_mode") == "reject":
                self._send(400, "rejected", "text/plain")
                return
            slot = int(root.attrib.get("id", "0"))
            content = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "contentitem"), None)
            if content is None:
                self._send(400, "missing content item", "text/plain")
                return
            location = content.attrib.get("location", "")
            if self.state.get("mismatch_location"):
                location = self.state["mismatch_location"]
            self.state["presets"][slot] = {
                "source": content.attrib.get("source", ""),
                "type": content.attrib.get("type", ""),
                "location": location,
                "name": next(
                    (node.text or "" for node in content.iter() if node.tag.rsplit("}", 1)[-1].lower() == "itemname"),
                    "",
                ),
            }
            self._send(200, "<presets/>")
        elif path == "/removePreset":
            slot = int(root.attrib.get("id", "0"))
            self.state["presets"].pop(slot, None)
            self._send(200, "<presets/>")
        elif path == "/select":
            content = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "contentitem"), None)
            if self.state.get("inactive") or self.state.get("select_mode") == "ignore" or content is None:
                self.state["now"] = None
            else:
                self.state["now"] = {
                    "source": content.attrib.get("source", ""),
                    "location": content.attrib.get("location", ""),
                    "name": next(
                        (
                            node.text or ""
                            for node in content.iter()
                            if node.tag.rsplit("}", 1)[-1].lower() == "itemname"
                        ),
                        "",
                    ),
                }
            self._send(200, "<nowPlaying/>")
        else:
            self._send(404, "not found", "text/plain")


class FakeSpeaker(_ServerContext):
    def __init__(self) -> None:
        super().__init__()
        self.state = {
            "device_id": "DEV1",
            "name": "Test Speaker",
            "type": "SoundTouch 10",
            "firmware": "27.0.6",
            "marge_url": "",
            "marge_status": 200,
            "sources": ["TUNEIN", "LOCAL_INTERNET_RADIO", "RADIO_BROWSER"],
            "source_status": "READY",
            "presets": {},
            "store_mode": "ok",
            "seek_mode": "normal",
            "mismatch_location": None,
            "now": None,
            "inactive": False,
            "keys": [],
            "requests": [],
        }

    def __enter__(self):
        self.start(_SpeakerHandler, _SpeakerServer)
        self.httpd.speaker = self  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc):
        self.stop()


# --------------------------------------------------------------- aftertouch
class _AfterTouchHandler(_QuietHandler):
    @property
    def state(self) -> dict:
        return self.server.aftertouch.state  # type: ignore[attr-defined]

    def _json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _xml(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - HTTP verb
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        base = self.state["base_url"]
        if path in ("/setup/version", "/api/setup/version"):
            self._json({"version": self.state["version"], "commit": "test", "date": "2026-01-01"})
        elif path == "/health":
            self._json({"status": "up", "version": self.state["version"]})
        elif path == "/bmx/registry/v1/services":
            services = [
                {"id": {"name": "TUNEIN"}, "baseUrl": base + "/bmx/tunein"},
                {"id": {"name": "LOCAL_INTERNET_RADIO"}, "baseUrl": base + "/core02/svc-bmx-adapter-orion/prod/orion"},
                {"id": {"name": "RADIO_BROWSER"}, "baseUrl": self.state["radio_browser_base"]},
            ]
            self._json({"bmx_services": services})
        elif path == "/core02/svc-bmx-adapter-orion/prod/orion/station":
            if not self.state.get("orion_ok", True):
                self._json({"error": "not found"}, 502)
                return
            self._json(
                {"name": "probe", "streamType": "liveRadio", "audio": {"streamUrl": "http://example.invalid/stream"}}
            )
        elif path.startswith("/soundtouch/stations/byuuid/"):
            if not self.state.get("radio_ok", True):
                self._json({"error": "not found"}, 404)
                return
            self._json(
                {"name": "Probe", "streamType": "liveRadio", "audio": {"streamUrl": "http://example.invalid/stream"}}
            )
        elif path.startswith("/streaming/account/"):
            self._xml("<account><accountStatus>OK</accountStatus></account>")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802 - HTTP verb
        self._body()
        self.state.setdefault("posts", []).append(self.path)
        self._json({"status": "ok"})


class _AfterTouchServer(_BaseServer):
    aftertouch: FakeAfterTouch


class FakeAfterTouch(_ServerContext):
    def __init__(self) -> None:
        super().__init__()
        self.state = {
            "version": "0.138.1",
            "orion_ok": True,
            "radio_ok": True,
            "base_url": "",
            "radio_browser_base": "",
            "posts": [],
        }

    def __enter__(self):
        self.start(_AfterTouchHandler, _AfterTouchServer)
        self.httpd.aftertouch = self  # type: ignore[attr-defined]
        self.state["base_url"] = self.url
        self.state["radio_browser_base"] = self.url + "/soundtouch"
        return self

    def __exit__(self, *exc):
        self.stop()


# ----------------------------------------------------------------- upstream
class _UpstreamHandler(_QuietHandler):
    def do_GET(self):  # noqa: N802 - HTTP verb
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        state = self.server.upstream.state  # type: ignore[attr-defined]
        host, port = self.server.server_address[0], self.server.server_address[1]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{port}/stream")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/token":
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{port}/stream?token=abc123&skey=42")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path != "/stream":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("icy-br", str(state["bitrate_kbps"]))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        chunk = b"\x00" * state["chunk_size"]
        deadline = time.monotonic() + state["duration"]
        try:
            while time.monotonic() < deadline:
                self.wfile.write(chunk)
                self.wfile.flush()
                state["bytes_sent"] += len(chunk)
                time.sleep(state["interval"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _UpstreamServer(_BaseServer):
    upstream: FakeUpstream


class FakeUpstream(_ServerContext):
    def __init__(
        self, *, bitrate_kbps: int = 128, duration: float = 5.0, chunk_size: int = 4096, interval: float = 0.25
    ) -> None:
        super().__init__()
        self.state = {
            "bitrate_kbps": bitrate_kbps,
            "duration": duration,
            "chunk_size": chunk_size,
            "interval": interval,
            "bytes_sent": 0,
        }

    def __enter__(self):
        self.start(_UpstreamHandler, _UpstreamServer)
        self.httpd.upstream = self  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc):
        self.stop()


# ------------------------------------------------------------------- client
def request(
    port: int, path: str, method: str = "GET", body: dict | None = None, token: str | None = None, timeout: float = 5.0
) -> tuple[int, dict | list | None, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = raw.decode("utf-8", errors="replace")
    return response.status, data, response.getheader("Content-Type", "")


class AppServer(_ServerContext):
    def __init__(
        self,
        tmp_path,
        fake_speaker: FakeSpeaker,
        fake_aftertouch: FakeAfterTouch,
        *,
        api_token: str = "",
        station_url: str | None = None,
    ) -> None:
        super().__init__()
        self.store = ConfigStore(tmp_path / "config.json")
        self.store.config = default_config()
        self.store.config["stations"] = {}
        if station_url:
            self.store.config["stations"]["test"] = {
                "name": "Test Station",
                "dispatcherUrl": station_url,
                "radioBrowserUuid": "uuid-1",
                "artwork": "",
                "bitrateKbps": 128,
                "headers": {},
                "presets": [],
                "presetModes": {},
                "lastProbe": None,
            }
        self.store.config["settings"]["bufferSeconds"] = 1
        self.store.config["settings"]["prebufferSeconds"] = 0
        self.store.config["settings"]["reconnectBackoffMs"] = [100]
        self.store.config["settings"]["defaultPresetMode"] = "relay"
        self.store.save()
        self.ctx = AppContext(
            self.store,
            base_url="http://127.0.0.1:0",
            aftertouch_url=fake_aftertouch.url,
            speaker_ip="127.0.0.1",
            speaker_ips=["127.0.0.1"],
            speaker_port=fake_speaker.port,
            api_token=api_token,
            radio_browser_url=fake_aftertouch.url,
        )

    def __enter__(self):
        self.httpd = create_server(self.ctx, "127.0.0.1", 0)
        self.ctx.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop()
