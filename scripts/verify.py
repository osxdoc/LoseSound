#!/usr/bin/env python3
"""Read-only health check for a running LoseSound installation.

The script never writes to the speaker or the service. It verifies the proxy,
AfterTouch, the speaker and every preset location, and explains what to fix.

Exit codes: 0 = all checks passed, 1 = at least one check failed,
2 = a check could not be decided (unreachable service).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

ORION_PROBE_PAYLOAD = {"name": "LoseSound verify", "imageUrl": "", "streamUrl": "http://127.0.0.1/probe"}
RADIO_BROWSER_PROBE_UUID = "9610c454-0601-11e8-ae97-52543be04c81"
UA = "LoseSound-verify/1.0"


def prefer_ipv4() -> None:
    """Prefer IPv4 for outbound checks, matching the service behaviour."""
    if os.getenv("NET_FORCE_IPV4", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - mirrors socket API
        if family in (0, socket.AF_UNSPEC):
            try:
                return original(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                return original(host, port, family, type, proto, flags)
        return original(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo


class Checker:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self.results: list[dict] = []
        self.metadata: dict = {}

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> None:
        self.results.append({"check": name, "status": status, "detail": detail, "hint": hint})

    def request(self, url: str, timeout: float | None = None, headers: dict | None = None) -> tuple[int, bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, response.read(), ""
        except urllib.error.HTTPError as exc:
            body = b""
            with contextlib.suppress(OSError):
                body = exc.read()
            return exc.code, body, f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 0, b"", str(getattr(exc, "reason", exc))

    def json(self, url: str, timeout: float | None = None) -> tuple[dict | None, str]:
        status, body, error = self.request(url, timeout=timeout, headers={"Accept": "application/json"})
        if status == 0:
            return None, error
        if not 200 <= status < 300:
            return None, error or f"HTTP {status}"
        try:
            return json.loads(body.decode("utf-8")), ""
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, f"invalid JSON: {exc}"


def read_env(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def xml_text(root: ET.Element, name: str) -> str:
    wanted = name.lower()
    for node in root.iter():
        if local_name(node.tag) == wanted and node.text:
            return node.text.strip()
    return ""


def parse_presets(body: bytes) -> dict[int, dict]:
    presets: dict[int, dict] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return presets
    for node in root.iter():
        if local_name(node.tag) != "preset":
            continue
        try:
            slot = int(node.attrib.get("id", ""))
        except ValueError:
            continue
        if not 1 <= slot <= 6:
            continue
        content = next((child for child in node.iter() if local_name(child.tag) == "contentitem"), None)
        if content is None:
            continue
        presets[slot] = {
            "source": content.attrib.get("source", ""),
            "type": content.attrib.get("type", ""),
            "location": content.attrib.get("location", ""),
            "name": xml_text(content, "itemName"),
        }
    return presets


def probe_stream_url(checker: Checker, url: str, label: str) -> None:
    if not url:
        checker.add(label, FAIL, "empty stream URL")
        return
    status, _body, error = checker.request(url, timeout=min(4.0, checker.timeout), headers={"Accept": "*/*"})
    if status == 0:
        checker.add(label, FAIL, f"not reachable: {error}")
        return
    if status >= 400:
        checker.add(label, FAIL, f"HTTP {status}")
        return
    checker.add(label, PASS, f"HTTP {status}")


def probe_orion(checker: Checker, base_url: str) -> None:
    data = base64.b64encode(json.dumps(ORION_PROBE_PAYLOAD, separators=(",", ":")).encode()).decode("ascii")
    url = f"{base_url.rstrip('/')}/station?data={urllib.parse.quote_plus(data)}"
    status, body, error = checker.request(url, timeout=4.0, headers={"Accept": "application/json"})
    if status != 200:
        checker.add(
            "AfterTouch Orion station endpoint",
            WARN,
            f"HTTP {status} {error}",
            "AfterTouch < 0.99 does not serve this endpoint. Update AfterTouch, then re-check.",
        )
        return
    try:
        parsed = json.loads(body.decode("utf-8"))
        checker.add("AfterTouch Orion station endpoint", PASS, f"streamType={parsed.get('streamType', '-')}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        checker.add("AfterTouch Orion station endpoint", FAIL, "endpoint answered but not with JSON")


def probe_radio_browser(checker: Checker, base_url: str) -> None:
    url = f"{base_url.rstrip('/')}/stations/byuuid/{RADIO_BROWSER_PROBE_UUID}"
    data, error = checker.json(url, timeout=5.0)
    if data and isinstance(data, dict) and ("audio" in data or "streamUrl" in data):
        checker.add("Radio Browser SoundTouch API", PASS, f"baseUrl={base_url}")
    else:
        checker.add("Radio Browser SoundTouch API", WARN, error or "unexpected response")


def classify_preset(checker: Checker, slot: int, preset: dict, proxy_host: str, proxy_base: str = "") -> None:
    source = (preset.get("source") or "").upper()
    location = preset.get("location") or ""
    label = f"Preset {slot} ({preset.get('name') or '-'})"
    if source == "RADIO_BROWSER":
        if location.startswith("/stations/byuuid/"):
            checker.add(label, PASS, f"radio-browser location {location}")
        else:
            checker.add(label, WARN, f"unexpected RADIO_BROWSER location: {location}")
        return
    if source != "LOCAL_INTERNET_RADIO":
        checker.add(label, PASS, f"source {source or '-'}")
        return
    if location.startswith("/station?data="):
        checker.add(label, PASS, "AfterTouch Orion station location (relative)")
        return
    if location.rstrip("/").endswith(".json"):
        if "://" not in location:
            checker.add(label, WARN, f"relative JSON location: {location}")
            return
        parsed = urllib.parse.urlsplit(location)
        if proxy_host and parsed.hostname != proxy_host:
            checker.add(label, WARN, f"points at {parsed.hostname}, not at this proxy ({proxy_host})")
        data, error = checker.json(location, timeout=4.0)
        if data is None:
            checker.add(label, FAIL, f"station description not readable: {error}")
            return
        streams = (data.get("audio") or {}).get("streams") or []
        stream_url = (data.get("audio") or {}).get("streamUrl") or (streams[0].get("streamUrl") if streams else "")
        if not stream_url:
            checker.add(label, FAIL, "station description has no streamUrl")
            return
        checker.add(label, PASS, f"JSON description -> {len(streams) or 1} stream(s)")
        proxy_netloc = urllib.parse.urlsplit(proxy_base).netloc
        stream_parsed = urllib.parse.urlsplit(stream_url)
        if (
            proxy_netloc
            and stream_parsed.netloc == proxy_netloc
            and not stream_parsed.path.rstrip("/").endswith(".json")
        ):
            checker.add(f"Preset {slot} stream", PASS, "relay endpoint (not opened by this check)")
        else:
            probe_stream_url(checker, stream_url, f"Preset {slot} stream")
        return
    if location.startswith(("http://", "https://")):
        checker.add(
            label,
            FAIL,
            f"raw stream URL as preset location: {location}",
            "The speaker expects a station description (JSON) at this location, not audio. Re-save the preset with mode 'Direct' or 'Relay'.",
        )
        return
    checker.add(label, WARN, f"unknown location form: {location}")


def summarize(checker: Checker) -> int:
    print()
    width = max([len(item["check"]) for item in checker.results] + [10])
    for item in checker.results:
        print(f"{item['status']:<5} {item['check']:<{width}}  {item['detail']}")
        if item["hint"] and item["status"] in (WARN, FAIL):
            print(f"{'':<5} {'':<{width}}  -> {item['hint']}")
    failures = [item for item in checker.results if item["status"] == FAIL]
    warnings = [item for item in checker.results if item["status"] == WARN]
    undecided = [item for item in checker.results if item["status"] == SKIP]
    print()
    print(
        f"Summary: {len(checker.results)} checks, {len(failures)} failed, {len(warnings)} warnings, {len(undecided)} undecided"
    )
    if failures:
        return 1
    if undecided:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only health check for LoseSound")
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    parser.add_argument("--service-host", help="host running Docker (default from .env)")
    parser.add_argument("--speaker-ip", help="speaker IP (default from .env)")
    parser.add_argument("--proxy-port", type=int, help="LoseSound port (default 8092)")
    parser.add_argument("--aftertouch-port", type=int, help="AfterTouch port (default 8091)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    prefer_ipv4()
    env = read_env(Path(args.env_file))
    host = args.service_host or env.get("SERVICE_HOST", "")
    speaker_ip = args.speaker_ip or env.get("SPEAKER_IP", "")
    proxy_port = args.proxy_port or int(env.get("PROXY_PORT", "8092"))
    aftertouch_port = args.aftertouch_port or int(env.get("AFTERTOUCH_HTTP_PORT", "8091"))

    if not host:
        print("FAIL  configuration               SERVICE_HOST is missing (use --service-host or a .env file)")
        return 2

    checker = Checker(timeout=args.timeout)
    proxy_base = f"http://{host}:{proxy_port}"
    aftertouch_base = f"http://{host}:{aftertouch_port}"
    speaker_base = f"http://{speaker_ip}:{env.get('SPEAKER_PORT', '8090')}" if speaker_ip else ""

    checker.metadata = {
        "serviceHost": host,
        "speakerIp": speaker_ip,
        "proxyUrl": proxy_base,
        "aftertouchUrl": aftertouch_base,
    }

    # --- proxy
    health, error = checker.json(proxy_base + "/api/health", timeout=4.0)
    if health:
        checker.add("LoseSound API", PASS, f"version={health.get('version', '-')} base={health.get('baseUrl', '-')}")
    else:
        checker.add(
            "LoseSound API",
            FAIL,
            error or "not reachable",
            "Start the preset-proxy container and check PROXY_PORT/BASE_URL.",
        )

    setup, setup_error = checker.json(proxy_base + "/api/setup/status?refresh=1", timeout=15.0)
    if setup:
        findings = setup.get("findings", [])
        errors = [item for item in findings if item.get("severity") == "error"]
        warnings = [item for item in findings if item.get("severity") == "warning"]
        detail = f"aftertouch={((setup.get('aftertouch') or {}).get('version') or '-')} findings={len(findings)}"
        if errors:
            checker.add(
                "LoseSound setup status",
                FAIL,
                detail + " (errors)",
                "; ".join(item.get("message", "") for item in errors)[:200],
            )
        elif warnings:
            checker.add(
                "LoseSound setup status",
                WARN,
                detail + " (warnings)",
                "; ".join(item.get("message", "") for item in warnings)[:200],
            )
        else:
            checker.add("LoseSound setup status", PASS, detail)
    else:
        checker.add("LoseSound setup status", WARN, setup_error or "not readable")

    # --- aftertouch
    version, error = checker.json(aftertouch_base + "/setup/version", timeout=4.0)
    if version is None:
        version, error = checker.json(aftertouch_base + "/api/setup/version", timeout=4.0)
    if version:
        checker.add("AfterTouch version", PASS, str(version.get("version", "")))
    else:
        checker.add("AfterTouch version", FAIL, error or "not reachable")

    registry, registry_error = checker.json(aftertouch_base + "/bmx/registry/v1/services", timeout=5.0)
    services: list[dict] = []
    if registry:
        for entry in registry.get("bmx_services", []) or []:
            identifier = entry.get("id") or {}
            services.append(
                {"name": identifier.get("name") or entry.get("name") or "", "baseUrl": entry.get("baseUrl") or ""}
            )
        checker.add(
            "AfterTouch service registry", PASS, ", ".join(service["name"] for service in services if service["name"])
        )
    else:
        checker.add("AfterTouch service registry", WARN, registry_error or "not readable")

    orion_service = next((service for service in services if service["name"].upper() == "LOCAL_INTERNET_RADIO"), None)
    if orion_service:
        probe_orion(checker, orion_service["baseUrl"])
    radio_service = next((service for service in services if service["name"].upper() == "RADIO_BROWSER"), None)
    if radio_service:
        probe_radio_browser(checker, radio_service["baseUrl"])

    # --- speaker
    presets: dict[int, dict] = {}
    if speaker_base:
        info_status, info_body, info_error = checker.request(speaker_base + "/info", timeout=4.0)
        if info_status == 200:
            try:
                info_root = ET.fromstring(info_body)
                name = xml_text(info_root, "name")
                marge = xml_text(info_root, "margeURL")
                firmware = xml_text(info_root, "softwareVersion")
                checker.add("Speaker info", PASS, f"{name or '-'} fw={firmware or '-'} marge={marge or '-'}")
                if marge:
                    marge_host = urllib.parse.urlsplit(marge).hostname or ""
                    if marge_host and marge_host != host:
                        checker.add(
                            "Speaker marge URL",
                            SKIP,
                            f"external marge {marge_host}; DNS migration not asserted from this host",
                        )
                    else:
                        marge_status, _body, marge_error = checker.request(marge, timeout=4.0)
                        if marge_status and 200 <= marge_status < 400:
                            checker.add("Speaker marge URL", PASS, f"HTTP {marge_status}")
                        else:
                            checker.add(
                                "Speaker marge URL",
                                WARN,
                                f"HTTP {marge_status} {marge_error}",
                                "AfterTouch before 0.99 does not serve /marge. Upgrade AfterTouch and migrate again with the plain base URL.",
                            )
            except ET.ParseError:
                checker.add("Speaker info", FAIL, "invalid XML")
        else:
            checker.add(
                "Speaker info", FAIL, info_error or f"HTTP {info_status}", "Check SPEAKER_IP and the LAN connection."
            )

        sources_status, sources_body, sources_error = checker.request(speaker_base + "/sources", timeout=4.0)
        if sources_status == 200:
            try:
                root = ET.fromstring(sources_body)
                ready = {
                    node.attrib.get("source", "").upper()
                    for node in root.iter()
                    if local_name(node.tag) == "sourceitem" and node.attrib.get("status", "").upper() == "READY"
                }
                missing = [
                    source for source in ("TUNEIN", "LOCAL_INTERNET_RADIO", "RADIO_BROWSER") if source not in ready
                ]
                if missing:
                    checker.add(
                        "Speaker radio sources",
                        WARN,
                        "missing: " + ", ".join(missing),
                        "Reboot the speaker or send sourcesUpdated; sources are registered at boot.",
                    )
                else:
                    checker.add(
                        "Speaker radio sources",
                        PASS,
                        ", ".join(sorted(ready & {"TUNEIN", "LOCAL_INTERNET_RADIO", "RADIO_BROWSER"})),
                    )
            except ET.ParseError:
                checker.add("Speaker radio sources", FAIL, "invalid XML")
        else:
            checker.add("Speaker radio sources", FAIL, sources_error or f"HTTP {sources_status}")

        presets_status, presets_body, presets_error = checker.request(speaker_base + "/presets", timeout=4.0)
        if presets_status == 200:
            presets = parse_presets(presets_body)
            checker.add("Speaker presets", PASS if presets else WARN, f"{len(presets)} preset(s)")
        else:
            checker.add("Speaker presets", FAIL, presets_error or f"HTTP {presets_status}")
    else:
        checker.add("Speaker info", SKIP, "SPEAKER_IP is not configured")

    # --- preset locations
    proxy_host = urllib.parse.urlsplit(proxy_base).hostname or ""
    if presets:
        for slot, preset in sorted(presets.items()):
            classify_preset(checker, slot, preset, proxy_host, proxy_base)
    else:
        checker.add("Preset locations", SKIP, "no presets readable")

    # --- station descriptions of all configured stations
    stations, stations_error = checker.json(proxy_base + "/api/stations", timeout=5.0)
    if stations and isinstance(stations.get("stations"), list):
        checker.add("Configured stations", PASS, f"{len(stations['stations'])} station(s)")
        for station in stations["stations"]:
            name = station.get("name", "")
            label = f"Station JSON {name}"
            data, describe_error = checker.json(f"{proxy_base}/{urllib.parse.quote(name)}.json", timeout=5.0)
            if data is None:
                checker.add(label, FAIL, describe_error or "not readable")
                continue
            streams = (data.get("audio") or {}).get("streams") or []
            if not streams:
                checker.add(label, FAIL, "no streams in station description")
                continue
            checker.add(label, PASS, f"{len(streams)} stream(s), name={data.get('name', '-')}")
    else:
        checker.add("Configured stations", WARN, stations_error or "not readable")

    # --- proxy runtime
    proxy_status, proxy_error = checker.json(proxy_base + "/api/proxy/status", timeout=5.0)
    if proxy_status:
        checker.add("Relay status", PASS, f"active={len(proxy_status.get('activeStreams', []))}")
    else:
        checker.add("Relay status", WARN, proxy_error or "not readable")

    if args.json:
        print(json.dumps({"checks": checker.results, "metadata": checker.metadata}, indent=2, ensure_ascii=False))
        failures = [item for item in checker.results if item["status"] == FAIL]
        undecided = [item for item in checker.results if item["status"] == SKIP]
        return 1 if failures else 2 if undecided else 0
    return summarize(checker)


if __name__ == "__main__":
    sys.exit(main())
