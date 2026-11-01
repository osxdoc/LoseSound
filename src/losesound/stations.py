"""Station helpers: URLs, redirect probing, station JSON and radio-browser catalog."""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .log import log, redact_url

STREAM_USER_AGENT = "Mozilla/5.0 (compatible; SoundTouch/1.0)"
DEFAULT_RADIO_BROWSER_BASE_URL = "https://de1.api.radio-browser.info"
MAX_CATALOG_LIMIT = 100

# Query parameters that usually mark a short lived, tokenised stream URL.
TOKEN_QUERY_KEYS = {
    "token",
    "tvf",
    "skey",
    "sabc",
    "hdnts",
    "wmsauthsign",
    "authtoken",
    "access_token",
    "signature",
    "sig",
    "expires",
    "exp",
}

BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata"}


def slugify(value: str) -> str:
    slug: list[str] = []
    for char in (value or "").lower():
        if char.isalnum():
            slug.append(char)
        elif slug and slug[-1] != "_":
            slug.append("_")
    text = "".join(slug).strip("_")
    return text[:48] or "station"


def unique_station_name(display_name: str, existing: set[str]) -> str:
    base = slugify(display_name)
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def validate_stream_url(url: str) -> tuple[bool, str]:
    text = (url or "").strip()
    if not text:
        return False, "URL is required"
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        return False, f"Invalid URL: {exc}"
    if parts.scheme not in ("http", "https"):
        return False, "Only http:// and https:// URLs are supported"
    if not parts.hostname:
        return False, "URL has no host"
    if parts.hostname.lower() in BLOCKED_HOSTS:
        return False, "Refusing link-local metadata address"
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return True, ""
    if address.is_link_local:
        return False, "Refusing link-local address"
    return True, ""


def bitrate_from_headers(headers) -> int | None:
    for key in ("icy-br", "icy-bitrate", "x-audiocast-bitrate"):
        value = headers.get(key)
        if not value:
            continue
        try:
            return int(str(value).strip().split()[0])
        except ValueError:
            continue
    return None


def is_tokenized(url: str) -> bool:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query, keep_blank_values=True)
    except ValueError:
        return False
    return any(key.lower() in TOKEN_QUERY_KEYS for key in query)


def resolve_stream(url: str, timeout: float = 8.0, extra_headers: dict[str, str] | None = None) -> dict:
    """Open *url* just far enough to follow redirects and read response headers."""
    started = time.time()
    headers = {"User-Agent": STREAM_USER_AGENT, "Accept": "*/*"}
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            resolved = response.url
            content_type = response_headers.get("content-type", "")
            result = {
                "ok": True,
                "status": response.status,
                "resolvedUrl": resolved,
                "requestedUrl": url,
                "redirected": resolved != url,
                "contentType": content_type,
                "bitrateKbps": bitrate_from_headers(response_headers),
                "elapsedMs": int((time.time() - started) * 1000),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        result = {
            "ok": False,
            "status": exc.code,
            "resolvedUrl": url,
            "requestedUrl": url,
            "redirected": False,
            "contentType": exc.headers.get("Content-Type", "") if exc.headers else "",
            "bitrateKbps": None,
            "elapsedMs": int((time.time() - started) * 1000),
            "error": f"HTTP {exc.code} {exc.reason}",
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        result = {
            "ok": False,
            "status": None,
            "resolvedUrl": url,
            "requestedUrl": url,
            "redirected": False,
            "contentType": "",
            "bitrateKbps": None,
            "elapsedMs": int((time.time() - started) * 1000),
            "error": str(reason),
        }
    log(
        "station.resolve",
        requested=redact_url(url),
        resolved=redact_url(result["resolvedUrl"]),
        status=result["status"],
        content_type=result["contentType"] or "-",
        elapsed_ms=result["elapsedMs"],
    )
    return result


def probe_station(station: dict, timeout: float = 8.0) -> dict:
    """Check whether a station is reachable and recommend a playback mode."""
    dispatcher = station.get("dispatcherUrl", "")
    result = resolve_stream(dispatcher, timeout=timeout, extra_headers=station.get("headers") or {})
    tokenized = is_tokenized(result["resolvedUrl"]) or is_tokenized(dispatcher)
    recommendation = "relay" if not result["ok"] or tokenized else "speaker"
    probe = {
        "at": int(time.time()),
        "ok": result["ok"],
        "status": result["status"],
        "dispatcherUrl": dispatcher,
        "resolvedUrl": result["resolvedUrl"],
        "contentType": result["contentType"],
        "bitrateKbps": result["bitrateKbps"],
        "tokenized": tokenized,
        "redirected": result["redirected"],
        "recommendation": recommendation,
        "elapsedMs": result["elapsedMs"],
        "error": result["error"],
    }
    log(
        "station.probe",
        station=station.get("name", ""),
        ok=probe["ok"],
        tokenized=probe["tokenized"],
        recommendation=recommendation,
    )
    return probe


def build_station_response(display_name: str, artwork: str, stream_urls: list[str], mode: str) -> dict:
    """Build the ``BmxPlaybackResponse`` JSON the speaker expects at a preset location."""
    urls = [url for url in stream_urls if url]
    if not urls:
        raise ValueError("No stream URL available")
    streams = [{"hasPlaylist": False, "isRealtime": True, "streamUrl": url} for url in urls]
    return {
        "name": display_name,
        "imageUrl": artwork or "",
        "streamType": "liveRadio",
        "isRealtime": True,
        "streamUrl": urls[0],
        "playbackMode": mode,
        "audio": {
            "hasPlaylist": False,
            "isRealtime": True,
            "streamUrl": urls[0],
            "streams": streams,
        },
    }


def fetch_radio_browser(
    params: dict, base_url: str = DEFAULT_RADIO_BROWSER_BASE_URL, timeout: float = 12.0
) -> list[dict]:
    """Search radio-browser.info and return a compact station list."""
    query = {
        "hidebroken": "true",
        "order": params.get("order", ["clickcount"])[0] or "clickcount",
        "reverse": "true",
    }
    try:
        limit = int(params.get("limit", ["30"])[0] or 30)
    except ValueError:
        limit = 30
    query["limit"] = str(max(1, min(MAX_CATALOG_LIMIT, limit)))

    allowed = ("name", "countrycode", "language", "tag", "codec", "bitrateMin", "bitrateMax")
    for key in allowed:
        value = params.get(key, [""])[0].strip()
        if value:
            query[key] = value

    url = f"{base_url.rstrip('/')}/json/stations/search?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": "LoseSound/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        stations = json.loads(response.read().decode("utf-8"))

    result = []
    for station in stations:
        stream_url = station.get("url_resolved") or station.get("url")
        if not stream_url:
            continue
        result.append(
            {
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
                "lastcheckok": station.get("lastcheckok", 0),
            }
        )
    log("catalog.search.ok", count=len(result), query=redact_url(url))
    return result
