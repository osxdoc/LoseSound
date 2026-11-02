"""HTTP API and web UI for LoseSound."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import ConfigStore, normalize_settings
from .log import log, redact_url
from .presets import PresetError, play_station, resolve_mode, store_and_verify
from .relay import RelaySession, StreamRegistry
from .setup_checks import ProbeCache, build_setup_status, probe_aftertouch, probe_speaker
from .speakers import SpeakerClient, SpeakerError
from .stations import (
    DEFAULT_RADIO_BROWSER_BASE_URL,
    build_station_response,
    fetch_radio_browser,
    probe_station,
    resolve_stream,
    slugify,
    unique_station_name,
    validate_stream_url,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "i18n.js": "application/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
    "archivo-var.woff2": "font/woff2",
    "archivo-OFL.txt": "text/plain; charset=utf-8",
}
JSON_LIMIT = 1_000_000
SLOT_RE = re.compile(
    r"^/api/(?:stations/(?P<station>[^/]+)/preset/(?P<slot>[1-6])|presets/(?P<remove_slot>[1-6])/remove)$"
)
STATION_ACTION_RE = re.compile(r"^/api/stations/(?P<station>[^/]+)/(?P<action>probe|play)$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class AppContext:
    """Everything a request needs. Kept explicit so tests can build their own."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        base_url: str,
        aftertouch_url: str,
        speaker_ip: str,
        speaker_ips: list[str] | None = None,
        speaker_port: int = 8090,
        api_token: str = "",
        radio_browser_url: str = DEFAULT_RADIO_BROWSER_BASE_URL,
        log_chunks: bool = False,
        version: str = __version__,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/") or "http://127.0.0.1:8092"
        self.aftertouch_url = aftertouch_url.rstrip("/") or "http://127.0.0.1:8091"
        self.speaker_ip = speaker_ip
        self.speaker_ips = [ip for ip in (speaker_ips or [speaker_ip]) if ip]
        self.speaker_port = speaker_port
        self.api_token = api_token
        self.radio_browser_url = radio_browser_url
        self.log_chunks = log_chunks
        self.version = version
        self.stats = StreamRegistry()
        self.aftertouch_cache = ProbeCache(ttl=60.0)
        self.setup_cache = ProbeCache(ttl=10.0)
        self.speakers_cache = ProbeCache(ttl=10.0)
        self.resolve_cache: dict[str, tuple[float, str, str]] = {}
        self.resolve_lock = threading.Lock()
        self.started = time.monotonic()

    # ------------------------------------------------------------- helpers
    def aftertouch(self, refresh: bool = False) -> dict:
        return self.aftertouch_cache.get(lambda: probe_aftertouch(self.aftertouch_url), refresh=refresh)

    def feature_support(self) -> dict:
        aftertouch = self.aftertouch()
        registry = aftertouch.get("registry") or []
        radio_registered = any(str(service.get("name", "")).upper() == "RADIO_BROWSER" for service in registry)
        return {
            "orion": bool(aftertouch.get("orion", {}).get("available")),
            "radioBrowser": bool(radio_registered or aftertouch.get("radioBrowser", {}).get("available")),
            "aftertouch": aftertouch,
        }

    def speaker(self, ip: str | None = None) -> SpeakerClient:
        return SpeakerClient(ip or self.speaker_ip, self.speaker_port)

    def resolve_station_stream(self, station_name: str, station: dict) -> tuple[str, str]:
        dispatcher = station.get("dispatcherUrl", "")
        now = time.monotonic()
        with self.resolve_lock:
            cached = self.resolve_cache.get(station_name)
        if cached and now - cached[0] < 5.0:
            return cached[1], cached[2]
        result = resolve_stream(dispatcher, timeout=4.0, extra_headers=station.get("headers") or {})
        if result["ok"]:
            resolved, content_type = result["resolvedUrl"], result["contentType"]
        else:
            resolved, content_type = dispatcher, ""
        with self.resolve_lock:
            self.resolve_cache[station_name] = (now, resolved, content_type)
        return resolved, content_type

    def station_snapshot(self, name: str) -> tuple[dict, dict]:
        config = self.store.snapshot()
        station = config.get("stations", {}).get(name)
        if not station:
            raise ApiError("Station not found", 404, "station_not_found")
        return config, station


class LoseSoundHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"LoseSound/{__version__}"
    sys_version = ""

    # ------------------------------------------------------------- plumbing
    @property
    def ctx(self) -> AppContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        log("http.request", client=self.client_address[0], message=fmt % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._security_headers()
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            self.wfile.write(body)

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def send_api_error(self, message: str, status: int = 400, code: str = "") -> None:
        payload = {"status": "error", "message": message}
        if code:
            payload["code"] = code
        self.send_json(payload, status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ApiError("Invalid Content-Length", 400, "invalid_request") from exc
        if length <= 0:
            return {}
        if length > JSON_LIMIT:
            raise ApiError("Request body too large", 413, "body_too_large")
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(f"Invalid JSON: {exc}", 400, "invalid_json") from exc
        if not isinstance(data, dict):
            raise ApiError("JSON body must be an object", 400, "invalid_json")
        return data

    def _client_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return self.headers.get("X-Api-Token", "").strip()

    def _authorized(self) -> bool:
        token = self.ctx.api_token
        if not token:
            return True
        return hmac.compare_digest(self._client_token(), token)

    def _parse(self) -> tuple[str, dict]:
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path)
        if len(path) > 1:
            path = path.rstrip("/")
        return path, urllib.parse.parse_qs(parts.query)

    # --------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        if not self._authorized():
            self.send_api_error("Missing or invalid API token", 401, "unauthorized")
            return
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        if not self._authorized():
            self.send_api_error("Missing or invalid API token", 401, "unauthorized")
            return
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path, query = self._parse()
        try:
            if method == "GET":
                self._route_get(path, query)
            elif method == "POST":
                self._route_post(path, query)
            elif method == "DELETE":
                self._route_delete(path, query)
        except ApiError as exc:
            self.send_api_error(str(exc), exc.status, exc.code)
        except SpeakerError as exc:
            log("api.speaker_error", path=path, message=str(exc))
            self.send_api_error(str(exc), 502, "speaker_error")
        except PresetError as exc:
            log("api.preset_error", path=path, message=str(exc))
            self.send_api_error(str(exc), 400, "preset_error")
        except Exception as exc:  # noqa: BLE001 - last resort for the HTTP API
            log("api.error", path=path, method=method, message=str(exc))
            with contextlib.suppress(OSError):
                self.send_api_error("Internal error", 500, "internal_error")

    def _route_get(self, path: str, query: dict) -> None:
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            self._serve_static(path.rsplit("/", 1)[-1])
        elif path in ("/favicon.ico", "/favicon.svg"):
            self._serve_static("favicon.svg")
        elif path == "/api/health":
            self._api_health()
        elif path == "/api/settings":
            self._api_settings_get()
        elif path == "/api/stations":
            self._api_stations_get()
        elif path == "/api/catalog/search":
            self._api_catalog_search(query)
        elif path == "/api/proxy/status":
            self._api_proxy_status()
        elif path == "/api/setup/status":
            self._api_setup_status(query)
        elif path == "/api/speakers":
            self._api_speakers(query)
        elif path.endswith(".json") and not path.startswith("/api/"):
            self._serve_station_json(path[1:-5], query)
        elif path.startswith("/api/"):
            raise ApiError("Not found", 404, "not_found")
        else:
            self._serve_audio(path[1:])

    def _route_post(self, path: str, query: dict) -> None:
        if path == "/api/settings":
            self._api_settings_post()
        elif path == "/api/stations":
            self._api_stations_post()
        elif path == "/api/catalog/add":
            self._api_catalog_add()
        elif path == "/api/speakers/refresh":
            self.ctx.speakers_cache.invalidate()
            self.ctx.setup_cache.invalidate()
            self.ctx.aftertouch_cache.invalidate()
            self._api_speakers({"refresh": ["1"]})
        else:
            action_match = STATION_ACTION_RE.match(path)
            slot_match = SLOT_RE.match(path)
            if action_match:
                station = urllib.parse.unquote(action_match.group("station"))
                action = action_match.group("action")
                if action == "probe":
                    self._api_station_probe(station)
                else:
                    self._api_station_play(station)
            elif slot_match and slot_match.group("station"):
                self._api_preset_set(urllib.parse.unquote(slot_match.group("station")), int(slot_match.group("slot")))
            elif slot_match and slot_match.group("remove_slot"):
                self._api_preset_remove(int(slot_match.group("remove_slot")))
            else:
                raise ApiError("Not found", 404, "not_found")

    def _route_delete(self, path: str, query: dict) -> None:
        if path.startswith("/api/stations/"):
            name = urllib.parse.unquote(path[len("/api/stations/") :])
            if "/" in name or not name:
                raise ApiError("Not found", 404, "not_found")
            self._api_station_delete(name, query)
        else:
            raise ApiError("Not found", 404, "not_found")

    # ----------------------------------------------------------------- web
    def _serve_static(self, name: str) -> None:
        filename = Path(name).name
        if filename not in STATIC_FILES:
            raise ApiError("Not found", 404, "not_found")
        path = STATIC_DIR / filename
        if not path.is_file():
            raise ApiError("Not found", 404, "not_found")
        body = path.read_bytes()
        self._send_bytes(200, body, STATIC_FILES[filename], cache="no-cache")

    def _serve_station_json(self, name: str, query: dict) -> None:
        config, station = self.ctx.station_snapshot(name)
        mode = (query.get("mode") or ["relay"])[0]
        if mode not in ("relay", "speaker"):
            raise ApiError("Unsupported station JSON mode", 400, "invalid_mode")
        slug = slugify(name)
        if mode == "relay":
            urls = [f"{self.ctx.base_url}/{slug}"]
        else:
            resolved, _content_type = self.ctx.resolve_station_stream(name, station)
            dispatcher = station.get("dispatcherUrl", "")
            urls = [resolved]
            if dispatcher and urllib.parse.unquote(resolved) != urllib.parse.unquote(dispatcher):
                urls.append(dispatcher)
        payload = build_station_response(station.get("name") or name, station.get("artwork", ""), urls, mode)
        log("station.json", station=name, mode=mode, stream_url=redact_url(urls[0]), client=self.client_address[0])
        self._send_bytes(200, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _serve_audio(self, name: str) -> None:
        config, station = self.ctx.station_snapshot(name)
        settings = config["settings"]
        session = RelaySession(self, name, station, settings, self.ctx.stats, self.ctx.log_chunks)
        session.run()

    # ----------------------------------------------------------------- api
    def _api_health(self) -> None:
        config = self.ctx.store.snapshot()
        active = self.ctx.stats.status([])["activeStreams"]
        self.send_json(
            {
                "status": "ok",
                "version": self.ctx.version,
                "uptimeSeconds": int(time.monotonic() - self.ctx.started),
                "activeStreams": len(active),
                "settings": normalize_settings(config.get("settings")),
                "baseUrl": self.ctx.base_url,
                "aftertouchUrl": self.ctx.aftertouch_url,
                "speakerIp": self.ctx.speaker_ip,
            }
        )

    def _api_settings_get(self) -> None:
        config = self.ctx.store.snapshot()
        self.send_json(normalize_settings(config.get("settings")))

    def _api_settings_post(self) -> None:
        data = self._read_json()
        settings = normalize_settings(data)

        def updater(config: dict) -> None:
            config["settings"] = settings

        self.ctx.store.update(updater)
        log(
            "settings.update",
            buffer_seconds=settings["bufferSeconds"],
            prebuffer_seconds=settings["prebufferSeconds"],
            bitrate_kbps=settings["bitrateKbps"],
            default_preset_mode=settings["defaultPresetMode"],
        )
        self.send_json({"status": "ok", "settings": settings})

    def _api_stations_get(self) -> None:
        config = self.ctx.store.snapshot()
        cached_aftertouch = self.ctx.aftertouch_cache.peek() or {}
        registry = cached_aftertouch.get("registry") or []
        features = {
            "orion": bool((cached_aftertouch.get("orion") or {}).get("available")),
            "radioBrowser": bool(
                any(str(service.get("name", "")).upper() == "RADIO_BROWSER" for service in registry)
                or (cached_aftertouch.get("radioBrowser") or {}).get("available")
            ),
        }
        stations = []
        for name, station in config.get("stations", {}).items():
            stations.append(
                {
                    "name": name,
                    "displayName": station.get("name", name),
                    "dispatcherUrl": station.get("dispatcherUrl", ""),
                    "radioBrowserUuid": station.get("radioBrowserUuid", ""),
                    "artwork": station.get("artwork", ""),
                    "bitrateKbps": station.get("bitrateKbps"),
                    "presets": station.get("presets", []),
                    "presetModes": station.get("presetModes", {}),
                    "lastProbe": station.get("lastProbe"),
                }
            )
        self.send_json(
            {
                "status": "ok",
                "stations": stations,
                "features": {"orion": features["orion"], "radioBrowser": features["radioBrowser"]},
            }
        )

    def _api_stations_post(self) -> None:
        data = self._read_json()
        display_name = str(data.get("displayName") or data.get("name") or "").strip()
        url = str(data.get("dispatcherUrl") or "").strip()
        if not display_name:
            raise ApiError("displayName is required", 400, "invalid_station")
        valid, message = validate_stream_url(url)
        if not valid:
            raise ApiError(message, 400, "invalid_station")

        config = self.ctx.store.snapshot()
        raw_name = str(data.get("name") or "").strip()
        name = slugify(raw_name) if raw_name else unique_station_name(display_name, set(config["stations"].keys()))
        if name in config["stations"]:
            raise ApiError("A station with this name already exists", 409, "station_exists")
        if any(
            str(station.get("name", "")).strip().casefold() == display_name.casefold()
            for station in config["stations"].values()
        ):
            raise ApiError("A station with this display name already exists", 409, "station_exists")

        bitrate = data.get("bitrateKbps")
        try:
            bitrate = int(bitrate) if bitrate not in (None, "") else None
        except (TypeError, ValueError):
            bitrate = None

        station = {
            "name": display_name,
            "dispatcherUrl": url,
            "radioBrowserUuid": str(data.get("radioBrowserUuid") or "").strip(),
            "artwork": str(data.get("artwork") or "").strip(),
            "bitrateKbps": bitrate,
            "headers": {},
            "presets": [],
            "presetModes": {},
            "lastProbe": None,
        }

        def updater(current: dict) -> None:
            current["stations"][name] = station

        self.ctx.store.update(updater)
        log("station.add", station=name, display_name=display_name)
        self.send_json({"status": "ok", "name": name, "station": station})

    def _api_station_probe(self, name: str) -> None:
        _config, station = self.ctx.station_snapshot(name)
        probe = probe_station(station)

        def updater(current: dict) -> None:
            target = current["stations"].get(name)
            if target is not None:
                target["lastProbe"] = probe

        self.ctx.store.update(updater)
        self.send_json({"status": "ok", "station": name, "probe": probe})

    def _resolve_mode_with_features(self, station: dict, slot: int, requested: str | None) -> str:
        config = self.ctx.store.snapshot()
        mode = resolve_mode(requested, station, slot, config["settings"])
        if mode == "radioBrowser":
            if not station.get("radioBrowserUuid"):
                raise ApiError("This station has no radio-browser UUID", 400, "radio_browser_uuid_missing")
            if not self.ctx.feature_support()["radioBrowser"]:
                raise ApiError(
                    "radio-browser.info is not available to this AfterTouch setup", 409, "radio_browser_unavailable"
                )
        if mode == "orion" and not self.ctx.feature_support()["orion"]:
            raise ApiError("AfterTouch does not provide the Orion station endpoint yet", 409, "orion_unavailable")
        return mode

    def _api_preset_set(self, name: str, slot: int) -> None:
        _config, station = self.ctx.station_snapshot(name)
        data = self._read_json()
        mode = self._resolve_mode_with_features(station, slot, data.get("mode"))
        client = self.ctx.speaker()
        result = store_and_verify(client, name, station, slot, mode, self.ctx.base_url)

        def updater(config: dict) -> None:
            for other in config["stations"].values():
                if slot in other.get("presets", []):
                    other["presets"].remove(slot)
                other.get("presetModes", {}).pop(str(slot), None)
            target = config["stations"][name]
            if slot not in target["presets"]:
                target["presets"].append(slot)
            target["presets"] = sorted(target["presets"])
            target.setdefault("presetModes", {})[str(slot)] = mode

        self.ctx.store.update(updater)
        self.send_json(result)

    def _api_preset_remove(self, slot: int) -> None:
        try:
            self.ctx.speaker().remove_preset(slot)
        except SpeakerError as exc:
            log("preset.remove.error", slot=slot, message=str(exc))

        removed = []

        def updater(config: dict) -> None:
            for name, station in config["stations"].items():
                if slot in station.get("presets", []):
                    station["presets"].remove(slot)
                    station.get("presetModes", {}).pop(str(slot), None)
                    removed.append(name)

        self.ctx.store.update(updater)
        log("preset.remove", slot=slot, stations=",".join(removed))
        self.send_json({"status": "ok", "slot": slot, "stations": removed})

    def _api_station_play(self, name: str) -> None:
        _config, station = self.ctx.station_snapshot(name)
        data = self._read_json()
        slot = 0
        try:
            slot = int(data.get("slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        mode = self._resolve_mode_with_features(station, slot, data.get("mode"))
        result = play_station(self.ctx.speaker(), name, station, mode, self.ctx.base_url)
        self.send_json(result, 200 if result.get("status") == "ok" else 504)

    def _api_station_delete(self, name: str, query: dict) -> None:
        config, station = self.ctx.station_snapshot(name)
        remove_presets = (query.get("removePresets") or ["0"])[0].lower() in ("1", "true", "yes")
        removed = []
        if remove_presets:
            client = self.ctx.speaker()
            for slot in station.get("presets", []):
                try:
                    client.remove_preset(slot)
                    removed.append(slot)
                except SpeakerError as exc:
                    log("preset.remove.error", station=name, slot=slot, message=str(exc))

        def updater(current: dict) -> None:
            current["stations"].pop(name, None)

        self.ctx.store.update(updater)
        log("station.delete", station=name, removed_presets=",".join(str(slot) for slot in removed))
        self.send_json({"status": "ok", "deleted": name, "removedPresets": removed})

    def _api_speakers(self, query: dict) -> None:
        refresh = (query.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")

        def collect() -> list[dict]:
            devices = []
            for ip in self.ctx.speaker_ips:
                device = probe_speaker(ip, self.ctx.speaker_port)
                try:
                    device["nowPlaying"] = self.ctx.speaker(ip).now_playing()
                except SpeakerError:
                    device["nowPlaying"] = None
                devices.append(device)
            return devices

        devices = self.ctx.speakers_cache.get(collect, refresh=refresh)
        self.send_json({"status": "ok", "devices": devices, "speakers": devices})

    def _api_setup_status(self, query: dict) -> None:
        refresh = (query.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
        aftertouch = self.ctx.aftertouch(refresh=refresh)
        stations = self.ctx.store.snapshot().get("stations", {})
        status = self.ctx.setup_cache.get(
            lambda: build_setup_status(
                base_url=self.ctx.base_url,
                aftertouch_url=self.ctx.aftertouch_url,
                speaker_ip=self.ctx.speaker_ip,
                speaker_ips=self.ctx.speaker_ips,
                speaker_port=self.ctx.speaker_port,
                aftertouch=aftertouch,
                stations=stations,
                refresh=refresh,
            ),
            refresh=refresh,
        )
        self.send_json(status)

    def _api_proxy_status(self) -> None:
        config = self.ctx.store.snapshot()
        self.send_json(self.ctx.stats.status(list(config.get("stations", {}).keys())))

    def _api_catalog_search(self, query: dict) -> None:
        try:
            stations = fetch_radio_browser(query, self.ctx.radio_browser_url)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log("catalog.search.error", message=str(exc))
            self.send_json({"status": "error", "message": str(exc), "stations": []})
            return
        self.send_json({"status": "ok", "stations": stations})

    def _api_catalog_add(self) -> None:
        data = self._read_json()
        display_name = str(data.get("displayName") or data.get("name") or "Station").strip()
        url = str(data.get("dispatcherUrl") or data.get("urlResolved") or data.get("url") or "").strip()
        valid, message = validate_stream_url(url)
        if not valid:
            raise ApiError(message, 400, "invalid_station")

        config = self.ctx.store.snapshot()
        name = unique_station_name(display_name, set(config["stations"].keys()))
        station = {
            "name": display_name,
            "dispatcherUrl": url,
            "radioBrowserUuid": str(data.get("radioBrowserUuid") or "").strip(),
            "artwork": str(data.get("artwork") or "").strip(),
            "bitrateKbps": None,
            "headers": {},
            "presets": [],
            "presetModes": {},
            "lastProbe": None,
        }

        def updater(current: dict) -> None:
            current["stations"][name] = station

        self.ctx.store.update(updater)
        log("catalog.station.add", station=name, display_name=display_name)

        slot = data.get("preset")
        if isinstance(slot, str) and slot.isdigit():
            slot = int(slot)
        response: dict = {"status": "ok", "name": name, "station": station}
        if isinstance(slot, int) and 1 <= slot <= 6:
            try:
                mode = self._resolve_mode_with_features(station, slot, data.get("mode"))
                result = store_and_verify(self.ctx.speaker(), name, station, slot, mode, self.ctx.base_url)

                def assign(current: dict) -> None:
                    for other in current["stations"].values():
                        if slot in other.get("presets", []):
                            other["presets"].remove(slot)
                        other.get("presetModes", {}).pop(str(slot), None)
                    target = current["stations"][name]
                    target["presets"] = sorted(set(target["presets"]) | {slot})
                    target.setdefault("presetModes", {})[str(slot)] = mode

                self.ctx.store.update(assign)
                response.update({"preset": slot, "mode": mode, "location": result.get("location", "")})
            except (PresetError, SpeakerError, ApiError) as exc:
                response = {"status": "error", "name": name, "message": str(exc)}
        self.send_json(response, 200 if response.get("status") == "ok" else 400)


class LoseSoundServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, context: AppContext) -> None:
        super().__init__(server_address, handler_class)
        self.ctx = context


def create_server(context: AppContext, host: str = "0.0.0.0", port: int = 8092) -> LoseSoundServer:
    return LoseSoundServer((host, port), LoseSoundHandler, context)


def build_context(store: ConfigStore) -> AppContext:
    """Build the runtime context from environment variables."""
    speaker_ip = os.getenv("SPEAKER_IP", "").strip()
    speaker_ips = [item.strip() for item in os.getenv("SPEAKER_IPS", speaker_ip).split(",") if item.strip()]
    try:
        speaker_port = int(os.getenv("SPEAKER_PORT", "8090"))
    except ValueError:
        speaker_port = 8090
    return AppContext(
        store,
        base_url=os.getenv("BASE_URL", "http://127.0.0.1:8092"),
        aftertouch_url=os.getenv("AFTERTOUCH_URL", "http://127.0.0.1:8091"),
        speaker_ip=speaker_ip,
        speaker_ips=speaker_ips,
        speaker_port=speaker_port,
        api_token=os.getenv("API_TOKEN", "").strip(),
        radio_browser_url=os.getenv("RADIO_BROWSER_BASE_URL", DEFAULT_RADIO_BROWSER_BASE_URL),
        log_chunks=os.getenv("LOG_STREAM_CHUNKS", "0").strip().lower() in ("1", "true", "yes", "on"),
        version=os.getenv("APP_VERSION", __version__),
    )
