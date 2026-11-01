"""Configuration handling: defaults, validation, migrations and persistence.

The on-disk file is versioned so that upgrades can migrate old ``proxy`` /
``direct`` preset modes without losing user data. Writes are atomic and keep a
``.bak`` copy of the previous file.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .log import log

SCHEMA_VERSION = 2

PLAYBACK_MODES = ("auto", "relay", "speaker", "radioBrowser", "orion")
LEGACY_PLAYBACK_MODES = {
    "proxy": "relay",
    "direct": "speaker",
    "radio_browser": "radioBrowser",
    "radiobrowser": "radioBrowser",
}

DEFAULT_BACKOFF_MS = [250, 500, 1000, 2000, 3000]
CONFIG_ENV = "CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/data/config.json"


def parse_backoff_ms(value) -> list[int]:
    """Parse a comma separated backoff list, keeping values between 50 and 30000 ms."""
    items = list(value) if isinstance(value, (list, tuple)) else str(value).split(",")

    backoff: list[int] = []
    for item in items:
        try:
            wait_ms = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 50 <= wait_ms <= 30000:
            backoff.append(wait_ms)
    return backoff or list(DEFAULT_BACKOFF_MS)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def normalize_mode(value, default: str = "relay") -> str:
    """Return a canonical playback mode name.

    Unknown values fall back to *default*. Legacy ``proxy``/``direct`` values
    are migrated so old configuration files keep working.
    """
    if not isinstance(value, str):
        return default
    cleaned = value.strip()
    cleaned = LEGACY_PLAYBACK_MODES.get(cleaned, cleaned)
    if cleaned in PLAYBACK_MODES:
        return cleaned
    if default in PLAYBACK_MODES:
        return default
    return "relay"


def default_settings() -> dict:
    audio_proxy = _env_bool("AUDIO_PROXY_ENABLED", True)
    default_mode = os.getenv("DEFAULT_PRESET_MODE", "").strip()
    if default_mode not in PLAYBACK_MODES:
        default_mode = "relay" if audio_proxy else "speaker"
    return {
        "bufferSeconds": _env_int("BUFFER_SECONDS", 3, 1, 30),
        "prebufferSeconds": _env_int("PREBUFFER_SECONDS", 1, 0, 10),
        "bitrateKbps": _env_int("BITRATE_KBPS", 128, 32, 320),
        "reconnectBackoffMs": parse_backoff_ms(
            os.getenv("RECONNECT_BACKOFF_MS", ",".join(str(v) for v in DEFAULT_BACKOFF_MS))
        ),
        "audioProxyEnabled": audio_proxy,
        "defaultPresetMode": default_mode,
    }


def default_config() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "settings": default_settings(),
        "stations": {
            "hr3": {
                "name": "HR3",
                "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/hr3/live/mp3/high",
                "radioBrowserUuid": "",
                "artwork": "",
                "bitrateKbps": None,
                "headers": {},
                "presets": [],
                "presetModes": {},
                "lastProbe": None,
            },
            "youfm": {
                "name": "YOU FM",
                "dispatcherUrl": "https://dispatcher.rndfnk.com/hr/youfm/live/mp3/high",
                "radioBrowserUuid": "",
                "artwork": "",
                "bitrateKbps": None,
                "headers": {},
                "presets": [],
                "presetModes": {},
                "lastProbe": None,
            },
        },
    }


def normalize_settings(data) -> dict:
    data = data if isinstance(data, dict) else {}

    def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(data.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def bool_setting(name: str, default: bool) -> bool:
        value = data.get(name, default)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    audio_proxy = bool_setting("audioProxyEnabled", True)
    raw_mode = data.get("defaultPresetMode")
    if raw_mode is None:
        default_mode = "relay" if audio_proxy else "speaker"
    else:
        default_mode = normalize_mode(raw_mode, "relay" if audio_proxy else "speaker")

    return {
        "bufferSeconds": bounded_int("bufferSeconds", 3, 1, 30),
        "prebufferSeconds": bounded_int("prebufferSeconds", 1, 0, 10),
        "bitrateKbps": bounded_int("bitrateKbps", 128, 32, 320),
        "reconnectBackoffMs": parse_backoff_ms(data.get("reconnectBackoffMs", DEFAULT_BACKOFF_MS)),
        "audioProxyEnabled": audio_proxy,
        "defaultPresetMode": default_mode,
    }


def _slot_list(value) -> list[int]:
    slots: set[int] = set()
    if isinstance(value, list):
        for item in value:
            try:
                slot = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= slot <= 6:
                slots.add(slot)
    return sorted(slots)


def _string_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def normalize_station(name: str, data, default_mode: str) -> tuple[dict, bool]:
    before = copy.deepcopy(data)
    data = data if isinstance(data, dict) else {}

    display_name = data.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = name

    dispatcher_url = data.get("dispatcherUrl")
    if not isinstance(dispatcher_url, str):
        dispatcher_url = ""

    radio_browser_uuid = data.get("radioBrowserUuid")
    if not isinstance(radio_browser_uuid, str):
        radio_browser_uuid = ""

    artwork = data.get("artwork")
    if not isinstance(artwork, str):
        artwork = ""

    bitrate = data.get("bitrateKbps")
    if bitrate is not None:
        try:
            bitrate = int(bitrate)
        except (TypeError, ValueError):
            bitrate = None
        if bitrate is not None and not 8 <= bitrate <= 1000:
            bitrate = None

    presets = _slot_list(data.get("presets"))
    raw_modes = data.get("presetModes")
    raw_modes = raw_modes if isinstance(raw_modes, dict) else {}
    preset_modes = {}
    for slot in presets:
        preset_modes[str(slot)] = normalize_mode(raw_modes.get(str(slot)), default_mode)

    last_probe = data.get("lastProbe")
    if not isinstance(last_probe, dict):
        last_probe = None

    station = {
        "name": display_name.strip(),
        "dispatcherUrl": dispatcher_url.strip(),
        "radioBrowserUuid": radio_browser_uuid.strip(),
        "artwork": artwork.strip(),
        "bitrateKbps": bitrate,
        "headers": _string_map(data.get("headers")),
        "presets": presets,
        "presetModes": preset_modes,
        "lastProbe": last_probe,
    }
    return station, station != before


def normalize_config(data) -> tuple[dict, bool]:
    """Normalize a loaded configuration and migrate old schemas."""
    if not isinstance(data, dict):
        return default_config(), True

    before = copy.deepcopy(data)
    data.setdefault("stations", {})
    if not isinstance(data["stations"], dict):
        data["stations"] = {}
    data.setdefault("settings", {})
    data["settings"] = normalize_settings(data.get("settings"))
    data["schemaVersion"] = SCHEMA_VERSION

    default_mode = data["settings"]["defaultPresetMode"]
    if default_mode == "auto":
        default_mode = "relay" if data["settings"]["audioProxyEnabled"] else "speaker"

    stations = {}
    for raw_name, raw_station in data["stations"].items():
        name = str(raw_name).strip()
        if not name:
            continue
        station, _changed = normalize_station(name, raw_station, default_mode)
        stations[name] = station
    data["stations"] = stations

    return data, data != before


class ConfigStore:
    """Thread safe access to the JSON configuration file."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.getenv(CONFIG_ENV, DEFAULT_CONFIG_PATH))
        self.lock = threading.RLock()
        self.config: dict = {}

    # ------------------------------------------------------------------ load
    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                self.config = default_config()
                self.save()
                log("config.created", path=str(self.path))
                return self.config

            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
                try:
                    shutil.copy2(self.path, backup)
                except OSError:
                    backup = None
                log("config.load.error", path=str(self.path), backup=str(backup) if backup else None, message=str(exc))
                self.config = default_config()
                self.save()
                return self.config

            self.config, changed = normalize_config(raw)
            if changed:
                log("config.migrated", path=str(self.path), schema=SCHEMA_VERSION)
            self.save()
            return self.config

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with contextlib.suppress(OSError):
                    shutil.copy2(self.path, self.path.with_name(f"{self.path.name}.bak"))
            tmp = self.path.with_name(f"{self.path.name}.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self.config, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)

    # --------------------------------------------------------------- helpers
    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.config)

    def update(self, callback) -> dict:
        """Run ``callback(config)`` under the lock, normalize, persist and return a snapshot."""
        with self.lock:
            result = callback(self.config)
            if isinstance(result, dict):
                self.config = result
            self.config, _changed = normalize_config(self.config)
            self.save()
            return copy.deepcopy(self.config)

    def station_names(self) -> list[str]:
        with self.lock:
            return sorted(self.config.get("stations", {}).keys())
