"""Preset engines: turn a station into a SoundTouch ``ContentItem`` and store it."""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from xml.sax.saxutils import escape, quoteattr

from .config import normalize_mode
from .log import log, redact_url
from .stations import slugify


class PresetError(RuntimeError):
    """Raised when a preset cannot be built or verified."""


def orion_location(display_name: str, artwork: str, stream_url: str) -> str:
    payload = {"name": display_name, "imageUrl": artwork or "", "streamUrl": stream_url}
    data = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return "/station?data=" + urllib.parse.quote_plus(data)


def resolve_mode(requested: str | None, station: dict, slot: int, settings: dict) -> str:
    """Resolve the effective playback mode for a preset slot."""
    fallback = settings.get("defaultPresetMode", "relay")
    if fallback == "auto":
        fallback = "relay" if settings.get("audioProxyEnabled", True) else "speaker"

    candidate = requested or (station.get("presetModes") or {}).get(str(slot)) or fallback
    mode = normalize_mode(candidate, fallback)
    if mode == "auto":
        recommendation = (station.get("lastProbe") or {}).get("recommendation")
        mode = recommendation if recommendation in ("relay", "speaker") else fallback
    return mode


def station_slug(station_name: str, station: dict) -> str:
    # Presets use the configuration key, which is unique and stable even when
    # the display name changes. Old LoseSound versions used the same form.
    return slugify(station_name)


def build_content_item(station_name: str, station: dict, mode: str, base_url: str) -> dict:
    """Build the ContentItem attributes for one playback mode."""
    slug = station_slug(station_name, station)
    base = (base_url or "").rstrip("/")
    display = station.get("name") or station_name

    if mode in ("relay", "speaker"):
        location = f"{base}/{slug}.json"
        if mode == "speaker":
            location += "?mode=speaker"
        return {
            "source": "LOCAL_INTERNET_RADIO",
            "type": "stationurl",
            "location": location,
            "itemName": display,
            "sourceAccount": "",
            "isPresetable": True,
            "mode": mode,
        }

    if mode == "radioBrowser":
        uuid = (station.get("radioBrowserUuid") or "").strip()
        if not uuid:
            raise PresetError("Station has no radio-browser UUID")
        return {
            "source": "RADIO_BROWSER",
            "type": "stationurl",
            "location": f"/stations/byuuid/{uuid}",
            "itemName": display,
            "sourceAccount": "",
            "isPresetable": True,
            "mode": mode,
        }

    if mode == "orion":
        dispatcher = (station.get("dispatcherUrl") or "").strip()
        if not dispatcher:
            raise PresetError("Station has no stream URL")
        return {
            "source": "LOCAL_INTERNET_RADIO",
            "type": "stationurl",
            "location": orion_location(display, station.get("artwork", ""), dispatcher),
            "itemName": display,
            "sourceAccount": "",
            "isPresetable": True,
            "mode": mode,
        }

    raise PresetError(f"Unsupported playback mode: {mode}")


def content_item_xml(item: dict) -> str:
    return (
        f"<ContentItem source={quoteattr(item['source'])} type={quoteattr(item['type'])} "
        f"location={quoteattr(item['location'])} sourceAccount={quoteattr(item.get('sourceAccount', ''))} "
        f'isPresetable="{str(bool(item.get("isPresetable", True))).lower()}">'
        f"<itemName>{escape(item.get('itemName', ''))}</itemName>"
        f"</ContentItem>"
    )


def preset_xml(item: dict, slot: int, created_on: int | None = None) -> str:
    timestamp = int(created_on or time.time())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<preset id="{int(slot)}" createdOn="{timestamp}" updatedOn="{timestamp}">\n'
        f"  {content_item_xml(item)}\n"
        "</preset>"
    )


def _normalized_location(value: str) -> str:
    return urllib.parse.unquote(value or "").rstrip("/")


def _same_location(left: str, right: str) -> bool:
    return _normalized_location(left) == _normalized_location(right)


def store_and_verify(client, station_name: str, station: dict, slot: int, mode: str, base_url: str) -> dict:
    """Store a preset on the speaker and verify what the speaker actually kept."""
    item = build_content_item(station_name, station, mode, base_url)
    xml = preset_xml(item, slot)
    started = time.time()
    log(
        "preset.store.start",
        station=station_name,
        slot=slot,
        mode=mode,
        source=item["source"],
        speaker=client.ip,
        location=redact_url(item["location"]),
    )
    try:
        client.store_preset(slot, xml)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log("preset.store.error", station=station_name, slot=slot, mode=mode, message=str(exc))
        raise PresetError(str(exc)) from exc

    try:
        stored = client.presets().get(slot, {})
    except Exception as exc:  # noqa: BLE001 - verification is best effort
        log("preset.verify.error", station=station_name, slot=slot, message=str(exc))
        stored = {}

    if stored and (
        stored.get("source") != item["source"] or not _same_location(stored.get("location", ""), item["location"])
    ):
        message = (
            f"Speaker kept source={stored.get('source')} location={redact_url(stored.get('location', ''))} "
            f"instead of source={item['source']} location={redact_url(item['location'])}"
        )
        log("preset.verify.mismatch", station=station_name, slot=slot, mode=mode, message=message)
        raise PresetError(message)

    log("preset.store.ok", station=station_name, slot=slot, mode=mode, elapsed_ms=int((time.time() - started) * 1000))
    return {
        "status": "ok",
        "slot": slot,
        "station": station_name,
        "mode": mode,
        "source": item["source"],
        "location": item["location"],
        "item": item,
        "verified": bool(stored),
        "speakerPreset": stored,
    }


def _now_playing_matches(now_playing: dict, item: dict) -> bool:
    if now_playing.get("source") and now_playing["source"] != item["source"]:
        return False
    location = now_playing.get("location", "")
    # A speaker may resolve a relative location; only compare when both are absolute URLs.
    if (
        location
        and "://" in location
        and item["location"].startswith("http")
        and not _same_location(location, item["location"])
    ):
        return False
    if now_playing.get("playStatus") == "PAUSE_STATE":
        return False
    return bool(
        now_playing.get("stationName") or now_playing.get("track") or now_playing.get("playStatus") == "PLAY_STATE"
    )


def play_station(client, station_name: str, station: dict, mode: str, base_url: str, timeout: float = 8.0) -> dict:
    """Select a station on the speaker and wait until it reports playback."""
    item = build_content_item(station_name, station, mode, base_url)
    started = time.time()
    log(
        "station.play.start",
        station=station_name,
        mode=mode,
        source=item["source"],
        location=redact_url(item["location"]),
    )
    try:
        client.select_content(content_item_xml(item))
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log("station.play.error", station=station_name, mode=mode, message=str(exc))
        raise PresetError(str(exc)) from exc

    deadline = time.time() + timeout
    now_playing: dict = {}
    while time.time() < deadline:
        try:
            now_playing = client.now_playing()
        except Exception:  # noqa: BLE001 - keep polling until the timeout
            time.sleep(0.5)
            continue
        if _now_playing_matches(now_playing, item):
            log(
                "station.play.ok",
                station=station_name,
                mode=mode,
                elapsed_ms=int((time.time() - started) * 1000),
                play_status=now_playing.get("playStatus", ""),
            )
            return {
                "status": "ok",
                "station": station_name,
                "mode": mode,
                "item": item,
                "nowPlaying": now_playing,
                "elapsedMs": int((time.time() - started) * 1000),
            }
        time.sleep(0.5)

    log("station.play.timeout", station=station_name, mode=mode, now_playing=now_playing.get("source", ""))
    return {
        "status": "error",
        "station": station_name,
        "mode": mode,
        "item": item,
        "nowPlaying": now_playing,
        "message": "Speaker did not report playback within the timeout",
        "elapsedMs": int((time.time() - started) * 1000),
    }
