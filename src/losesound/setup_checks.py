"""Diagnostics for the AfterTouch service, the speaker and the overall setup."""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .log import log, redact_url
from .speakers import SpeakerClient, SpeakerError

MIN_ORION_VERSION = (0, 99, 0)
REQUIRED_SOURCES = ("TUNEIN", "LOCAL_INTERNET_RADIO", "RADIO_BROWSER")
PROBE_USER_AGENT = "LoseSound/1.0"
RADIO_BROWSER_PROBE_UUID = "9610c454-0601-11e8-ae97-52543be04c81"


def parse_version(value: str) -> tuple[int, ...] | None:
    text = (value or "").strip().lstrip("vV")
    if not text or text.lower() in ("dev", "unknown", "snapshot"):
        return None
    parts = text.split("-")[0].split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            return None
    return tuple(numbers) if numbers else None


def version_at_least(value: str, minimum: tuple[int, ...]) -> bool | None:
    parsed = parse_version(value)
    if parsed is None:
        return None
    return parsed >= minimum


def _http_get(url: str, timeout: float = 4.0, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": PROBE_USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), ""
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(OSError):
            body = exc.read()
        return exc.code, body, f"HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, b"", str(getattr(exc, "reason", exc))


def _http_get_json(url: str, timeout: float = 4.0) -> tuple[dict | None, str]:
    status, body, error = _http_get(url, timeout=timeout, headers={"Accept": "application/json"})
    if status == 0:
        return None, error
    if not 200 <= status < 300:
        return None, error or f"HTTP {status}"
    try:
        return json.loads(body.decode("utf-8")), ""
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"Invalid JSON: {exc}"


def probe_service_url(url: str, timeout: float = 3.0) -> dict:
    """Probe a service base URL from the BMX registry."""
    status, _body, error = _http_get(url, timeout=timeout, headers={"Accept": "application/json"})
    reachable = status != 0 and status not in (404, 502, 503)
    return {"url": url, "reachable": reachable, "status": status, "error": error}


def probe_orion(base_url: str, timeout: float = 4.0) -> dict:
    payload = {"name": "LoseSound probe", "imageUrl": "", "streamUrl": "http://127.0.0.1/probe"}
    data = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    url = f"{base_url.rstrip('/')}/station?data={urllib.parse.quote_plus(data)}"
    status, body, error = _http_get(url, timeout=timeout, headers={"Accept": "application/json"})
    available = False
    if status == 200:
        try:
            data_json = json.loads(body.decode("utf-8"))
            available = isinstance(data_json, dict) and ("streamUrl" in data_json or "audio" in data_json)
        except (json.JSONDecodeError, UnicodeDecodeError):
            available = False
    return {"url": redact_url(url), "available": available, "status": status, "error": error}


def probe_radio_browser(base_url: str, timeout: float = 4.0) -> dict:
    url = f"{base_url.rstrip('/')}/stations/byuuid/{RADIO_BROWSER_PROBE_UUID}"
    data, error = _http_get_json(url, timeout=timeout)
    available = bool(data and isinstance(data, dict) and ("audio" in data or "streamUrl" in data))
    return {
        "url": redact_url(url),
        "available": available,
        "status": 200 if available else 0,
        "error": "" if available else (error or "unexpected response"),
    }


def probe_aftertouch(base_url: str, timeout: float = 4.0) -> dict:
    """Collect version, health, BMX registry and feature support of AfterTouch."""
    base = base_url.rstrip("/")
    result: dict = {
        "baseUrl": base,
        "reachable": False,
        "version": "",
        "build": {},
        "health": {},
        "registry": [],
        "registryReachable": False,
        "orion": {"available": False, "status": 0, "error": ""},
        "radioBrowser": {"available": False, "status": 0, "error": ""},
        "versionSupported": None,
        "errors": [],
    }

    version_data, version_error = _http_get_json(f"{base}/setup/version", timeout=timeout)
    if version_data is None:
        version_data, version_error = _http_get_json(f"{base}/api/setup/version", timeout=timeout)
    if version_data is not None:
        result["reachable"] = True
        result["version"] = str(version_data.get("version") or "")
        result["build"] = {
            "commit": version_data.get("commit", ""),
            "date": version_data.get("date", ""),
            "releaseUrl": version_data.get("release_url", ""),
        }
    else:
        status, _body, health_error = _http_get(f"{base}/health", timeout=timeout)
        if status:
            result["reachable"] = True
        else:
            result["errors"].append(f"AfterTouch unreachable: {health_error or version_error}")

    health_data, _health_error = _http_get_json(f"{base}/health", timeout=timeout)
    if health_data is not None:
        result["health"] = health_data

    registry_data, registry_error = _http_get_json(f"{base}/bmx/registry/v1/services", timeout=timeout)
    if registry_data is not None:
        result["registryReachable"] = True
        services = []
        for entry in registry_data.get("bmx_services", []) or []:
            identifier = entry.get("id") or {}
            name = identifier.get("name") or entry.get("name") or ""
            service_base = entry.get("baseUrl") or ""
            if not service_base:
                continue
            services.append({"name": name, "baseUrl": service_base})
        result["registry"] = services

        orion_service = next(
            (service for service in services if service["name"].upper() == "LOCAL_INTERNET_RADIO"), None
        )
        if orion_service:
            result["orion"] = probe_orion(orion_service["baseUrl"], timeout=timeout)

        radio_service = next((service for service in services if service["name"].upper() == "RADIO_BROWSER"), None)
        if radio_service:
            result["radioBrowser"] = probe_radio_browser(radio_service["baseUrl"], timeout=timeout)
    else:
        result["errors"].append(f"BMX registry unavailable: {registry_error}")

    result["versionSupported"] = version_at_least(result["version"], MIN_ORION_VERSION)
    log(
        "aftertouch.probe",
        base_url=base,
        reachable=result["reachable"],
        version=result["version"] or "-",
        orion=result["orion"]["available"],
        radio_browser=result["radioBrowser"]["available"],
    )
    return result


class ProbeCache:
    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._at = 0.0

    def get(self, callback, refresh: bool = False) -> dict:
        with self._lock:
            if not refresh and self._value is not None and time.monotonic() - self._at < self.ttl:
                return self._value
        value = callback()
        with self._lock:
            self._value = value
            self._at = time.monotonic()
        return value

    def peek(self):
        with self._lock:
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._at = 0.0


def probe_speaker(speaker_ip: str, speaker_port: int = 8090, timeout: float = 4.0) -> dict:
    result: dict = {
        "speakerIp": speaker_ip,
        "speakerPort": speaker_port,
        "speakerReachable": False,
        "deviceId": "",
        "name": "",
        "type": "",
        "firmware": "",
        "account": "",
        "currentMargeUrl": "",
        "margeReachable": None,
        "margeStatus": 0,
        "margeError": "",
        "sources": [],
        "sourcesReady": False,
        "missingSources": list(REQUIRED_SOURCES),
        "message": "",
    }
    if not speaker_ip:
        result["message"] = "SPEAKER_IP is not configured"
        return result

    client = SpeakerClient(speaker_ip, speaker_port, timeout)
    try:
        info = client.info()
    except SpeakerError as exc:
        result["message"] = str(exc)
        return result

    result.update(
        {
            "speakerReachable": True,
            "deviceId": info.get("deviceId", ""),
            "name": info.get("name", ""),
            "type": info.get("type", ""),
            "firmware": info.get("firmware", ""),
            "account": info.get("account", ""),
            "currentMargeUrl": info.get("margeUrl", ""),
        }
    )

    try:
        sources = client.sources()
        result["sources"] = sources
        available = {
            str(entry.get("source", "")).upper() for entry in sources if str(entry.get("status", "")).upper() == "READY"
        }
        result["missingSources"] = [source for source in REQUIRED_SOURCES if source not in available]
        result["sourcesReady"] = not result["missingSources"]
    except SpeakerError as exc:
        result["message"] = f"Could not read sources: {exc}"

    if result["currentMargeUrl"]:
        status, _body, error = _http_get(
            result["currentMargeUrl"], timeout=timeout, headers={"Accept": "application/xml"}
        )
        result["margeStatus"] = status
        result["margeReachable"] = status != 0 and 200 <= status < 400
        result["margeError"] = error

    log(
        "speaker.probe",
        speaker=speaker_ip,
        reachable=result["speakerReachable"],
        sources_ready=result["sourcesReady"],
        marge=redact_url(result["currentMargeUrl"]),
        marge_reachable=result["margeReachable"],
    )
    return result


def _marge_host(device: dict) -> str:
    try:
        return urllib.parse.urlsplit(device.get("currentMargeUrl") or "").hostname or ""
    except ValueError:
        return ""


def _speaker_findings(device: dict, aftertouch_host: str) -> list[dict]:
    findings: list[dict] = []
    if not device["speakerReachable"]:
        findings.append(
            {
                "severity": "error",
                "code": "speaker_unreachable",
                "message": f"{device['speakerIp']} is not reachable: {device['message']}",
                "hint": "Check SPEAKER_IP, the LAN connection and that the speaker is powered on.",
            }
        )
        return findings
    if device["margeReachable"] is False and _marge_host(device) == aftertouch_host:
        findings.append(
            {
                "severity": "warning",
                "code": "marge_unreachable",
                "message": f"The speaker's marge URL answers HTTP {device['margeStatus']}: {device['currentMargeUrl']}",
                "hint": "On AfterTouch versions before 0.99 the /marge suffix is not served. Upgrade AfterTouch and migrate the speaker again with the canonical base URL.",
            }
        )
    if not device["sourcesReady"]:
        findings.append(
            {
                "severity": "warning",
                "code": "sources_missing",
                "message": "The speaker does not report these radio sources as READY: "
                + ", ".join(device["missingSources"]),
                "hint": "Send sourcesUpdated or reboot the speaker. AfterTouch only registers RADIO_BROWSER/LOCAL_INTERNET_RADIO from Sources.xml at boot time.",
            }
        )
    return findings


def build_setup_status(
    *,
    base_url: str,
    aftertouch_url: str,
    speaker_ip: str,
    speaker_ips: list[str],
    speaker_port: int,
    aftertouch: dict,
    refresh: bool = False,
) -> dict:
    targets = [ip for ip in dict.fromkeys(speaker_ips or [speaker_ip]) if ip]
    devices: list[dict] = []
    if targets:
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
            devices = list(executor.map(lambda ip: probe_speaker(ip, speaker_port), targets))

    findings: list[dict] = []
    if not aftertouch.get("reachable"):
        findings.append(
            {
                "severity": "error",
                "code": "aftertouch_unreachable",
                "message": "AfterTouch is not reachable at " + aftertouch_url,
                "hint": "Start the aftertouch container and verify AFTERTOUCH_URL in .env.",
            }
        )
    else:
        if aftertouch.get("versionSupported") is False:
            findings.append(
                {
                    "severity": "warning",
                    "code": "aftertouch_old",
                    "message": f"AfterTouch {aftertouch.get('version') or 'unknown'} is older than 0.99",
                    "hint": "Update the image to 0.138.1 or newer to get the Orion station endpoint, relative preset locations and current fixes.",
                }
            )
        elif aftertouch.get("versionSupported") is None:
            findings.append(
                {
                    "severity": "info",
                    "code": "aftertouch_version_unknown",
                    "message": "AfterTouch did not report a release version",
                    "hint": "A locally built image reports 'dev'. Feature checks below still verify what actually works.",
                }
            )
        if not aftertouch.get("registryReachable"):
            findings.append(
                {
                    "severity": "warning",
                    "code": "registry_unreachable",
                    "message": "The BMX service registry is not readable",
                    "hint": "Radio sources (TUNEIN, LOCAL_INTERNET_RADIO, RADIO_BROWSER) cannot be resolved by the speaker without it.",
                }
            )
        if aftertouch.get("registryReachable") and not aftertouch["orion"]["available"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "orion_unavailable",
                    "message": "The Orion station endpoint (LOCAL_INTERNET_RADIO) is advertised but does not answer",
                    "hint": "Presets in the 'AfterTouch' mode cannot play. Use 'Direct' or 'Relay' until AfterTouch is updated.",
                }
            )
        if aftertouch.get("registryReachable") and not aftertouch["radioBrowser"]["available"]:
            findings.append(
                {
                    "severity": "info",
                    "code": "radio_browser_unavailable",
                    "message": "radio-browser.info is not reachable from this server",
                    "hint": "The Radio Browser preset mode needs internet access from the speaker. Other modes are unaffected.",
                }
            )

    aftertouch_host = urllib.parse.urlsplit(aftertouch_url).hostname or ""
    for device in devices:
        findings.extend(_speaker_findings(device, aftertouch_host))

    ready = not any(finding["severity"] == "error" for finding in findings)
    return {
        "status": "ok",
        "generatedAt": int(time.time()),
        "ready": ready,
        "baseUrl": base_url,
        "aftertouchUrl": aftertouch_url,
        "speakerIp": speaker_ip,
        "speakerIps": targets,
        "devices": devices,
        "aftertouch": aftertouch,
        "findings": findings,
    }
