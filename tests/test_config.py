from __future__ import annotations

import json

from losesound.config import (
    ConfigStore,
    default_config,
    default_settings,
    normalize_config,
    normalize_mode,
    normalize_settings,
    parse_backoff_ms,
)
from tests.helpers import FakeSpeaker


def test_parse_backoff_filters_and_falls_back():
    assert parse_backoff_ms("100, 500, 999999, foo") == [100, 500]
    assert parse_backoff_ms([10, 250, 30000]) == [250, 30000]
    assert parse_backoff_ms("") == [250, 500, 1000, 2000, 3000]


def test_normalize_mode_migrates_legacy_values():
    assert normalize_mode("proxy", "relay") == "relay"
    assert normalize_mode("direct", "relay") == "speaker"
    assert normalize_mode("radio_browser") == "radioBrowser"
    assert normalize_mode("nonsense", "speaker") == "speaker"
    assert normalize_mode(None, "relay") == "relay"


def test_settings_are_bounded(monkeypatch):
    monkeypatch.setenv("BUFFER_SECONDS", "999")
    monkeypatch.setenv("PREBUFFER_SECONDS", "-3")
    monkeypatch.setenv("BITRATE_KBPS", "1")
    settings = default_settings()
    assert settings["bufferSeconds"] == 30
    assert settings["prebufferSeconds"] == 0
    assert settings["bitrateKbps"] == 32


def test_normalize_settings_fills_new_keys_and_derives_default_mode():
    settings = normalize_settings({"bufferSeconds": 5, "bitrateKbps": 192, "audioProxyEnabled": False})
    assert settings["bufferSeconds"] == 5
    assert settings["bitrateKbps"] == 192
    assert "prebufferSeconds" in settings
    assert settings["defaultPresetMode"] == "speaker"


def test_migrates_v1_configuration():
    raw = {
        "settings": {
            "bufferSeconds": 5,
            "bitrateKbps": 128,
            "reconnectBackoffMs": [250, 500],
            "audioProxyEnabled": True,
        },
        "stations": {
            "hr3": {
                "name": "HR3",
                "dispatcherUrl": "https://example.invalid/hr3",
                "presets": [1, 7, "2"],
                "presetModes": {"1": "proxy", "2": "direct", "9": "relay"},
            }
        },
    }
    normalized, changed = normalize_config(raw)
    assert changed is True
    assert normalized["schemaVersion"] == 2
    station = normalized["stations"]["hr3"]
    assert station["presets"] == [1, 2]
    assert station["presetModes"] == {"1": "relay", "2": "speaker"}
    assert station["radioBrowserUuid"] == ""
    assert normalized["settings"]["prebufferSeconds"] == 1
    assert normalized["settings"]["defaultPresetMode"] == "relay"


def test_normalize_config_drops_empty_and_invalid_stations():
    normalized, _changed = normalize_config({"stations": {"": {}, "ok": "nonsense"}})
    assert "" not in normalized["stations"]
    assert normalized["stations"]["ok"]["name"] == "ok"
    assert normalized["stations"]["ok"]["presets"] == []


def test_store_persists_atomically_and_keeps_backup(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.load()
    store.update(lambda config: config["settings"].__setitem__("bufferSeconds", 7))
    assert json.loads(path.read_text())["settings"]["bufferSeconds"] == 7
    assert (tmp_path / "config.json.bak").exists()
    reloaded = ConfigStore(path)
    reloaded.load()
    assert reloaded.snapshot()["settings"]["bufferSeconds"] == 7


def test_store_recovers_corrupt_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ definitely not json", encoding="utf-8")
    store = ConfigStore(path)
    loaded = store.load()
    assert loaded["stations"]
    assert any(tmp_path.glob("config.json.corrupt-*"))
    assert json.loads(path.read_text())["schemaVersion"] == 2


def test_default_config_is_valid_and_roundtrips():
    config = default_config()
    normalized, changed = normalize_config(config)
    assert changed is False
    assert normalized == config


def test_unknown_station_fields_are_preserved_with_defaults():
    normalized, _changed = normalize_config(
        {"stations": {"st": {"name": "St", "dispatcherUrl": "http://x", "presets": [3], "custom": "kept"}}}
    )
    station = normalized["stations"]["st"]
    assert station["presets"] == [3]
    assert station["presetModes"]["3"] == "relay"


def test_speaker_client_parses_presets_and_sources():
    with FakeSpeaker() as speaker:
        speaker.state["presets"][1] = {
            "source": "LOCAL_INTERNET_RADIO",
            "type": "stationurl",
            "location": "http://x/1.json",
            "name": "One",
        }
        from losesound.speakers import SpeakerClient

        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        presets = client.presets()
        assert presets[1]["location"] == "http://x/1.json"
        sources = client.sources()
        assert {item["source"] for item in sources} >= {"TUNEIN", "LOCAL_INTERNET_RADIO"}
        info = client.info()
        assert info["deviceId"] == "DEV1"
