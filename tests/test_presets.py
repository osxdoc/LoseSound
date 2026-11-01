from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

from losesound import presets
from losesound.speakers import SpeakerClient
from tests.helpers import FakeSpeaker

STATION = {
    "name": "Test Station",
    "dispatcherUrl": "https://example.invalid/stream",
    "radioBrowserUuid": "uuid-1",
    "artwork": "https://example.invalid/logo.png",
}


def test_build_relay_item():
    item = presets.build_content_item("test", STATION, "relay", "http://proxy:8092/")
    assert item["source"] == "LOCAL_INTERNET_RADIO"
    assert item["type"] == "stationurl"
    assert item["location"] == "http://proxy:8092/test.json"


def test_build_speaker_item():
    item = presets.build_content_item("test", STATION, "speaker", "http://proxy:8092")
    assert item["location"] == "http://proxy:8092/test.json?mode=speaker"


def test_build_radio_browser_item_requires_uuid():
    item = presets.build_content_item("test", STATION, "radioBrowser", "http://proxy:8092")
    assert item["source"] == "RADIO_BROWSER"
    assert item["location"] == "/stations/byuuid/uuid-1"
    without_uuid = dict(STATION, radioBrowserUuid="")
    with pytest.raises(presets.PresetError):
        presets.build_content_item("test", without_uuid, "radioBrowser", "http://proxy:8092")


def test_build_orion_item_encodes_stream_url():
    item = presets.build_content_item("test", STATION, "orion", "http://proxy:8092")
    assert item["source"] == "LOCAL_INTERNET_RADIO"
    assert item["location"].startswith("/station?data=")
    encoded = item["location"].split("data=", 1)[1]
    payload = json.loads(base64.b64decode(urllib.parse.unquote_plus(encoded)).decode())
    assert payload["streamUrl"] == "https://example.invalid/stream"
    assert payload["name"] == "Test Station"
    assert payload["imageUrl"] == "https://example.invalid/logo.png"


def test_unsupported_mode_raises():
    with pytest.raises(presets.PresetError):
        presets.build_content_item("test", STATION, "auto", "http://proxy:8092")


def test_content_item_xml_escapes_values():
    item = presets.build_content_item("test", dict(STATION, name='A & B "quoted" <tag>'), "relay", "http://proxy:8092")
    xml = presets.content_item_xml(item)
    assert "&amp;" in xml
    assert 'A &amp; B "quoted" &lt;tag&gt;' in xml
    assert "&lt;tag&gt;" in xml
    assert 'source="LOCAL_INTERNET_RADIO"' in xml


def test_preset_xml_contains_slot_and_timestamps():
    item = presets.build_content_item("test", STATION, "relay", "http://proxy:8092")
    xml = presets.preset_xml(item, 4, created_on=1234567890)
    assert '<preset id="4" createdOn="1234567890" updatedOn="1234567890">' in xml
    assert "<ContentItem" in xml
    assert "<itemName>Test Station</itemName>" in xml


def test_resolve_mode_prefers_request_then_slot_then_default():
    settings = {"defaultPresetMode": "relay", "audioProxyEnabled": True}
    station = dict(STATION, presetModes={"2": "speaker"}, lastProbe={"recommendation": "speaker"})
    assert presets.resolve_mode("relay", station, 1, settings) == "relay"
    assert presets.resolve_mode(None, station, 2, settings) == "speaker"
    assert presets.resolve_mode(None, dict(STATION), 1, settings) == "relay"
    assert presets.resolve_mode("auto", station, 1, settings) == "speaker"
    assert presets.resolve_mode("auto", dict(STATION), 1, settings) == "relay"


def test_store_and_verify_roundtrip():
    with FakeSpeaker() as speaker:
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        result = presets.store_and_verify(client, "test", STATION, 2, "relay", "http://127.0.0.1:8092")
        assert result["status"] == "ok"
        assert result["verified"] is True
        assert speaker.state["presets"][2]["location"] == "http://127.0.0.1:8092/test.json"
        assert speaker.state["presets"][2]["source"] == "LOCAL_INTERNET_RADIO"


def test_store_and_verify_detects_speaker_mismatch():
    with FakeSpeaker() as speaker:
        speaker.state["mismatch_location"] = "http://elsewhere/other.json"
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        with pytest.raises(presets.PresetError):
            presets.store_and_verify(client, "test", STATION, 2, "relay", "http://127.0.0.1:8092")


def test_store_and_verify_reports_speaker_rejection():
    with FakeSpeaker() as speaker:
        speaker.state["store_mode"] = "reject"
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        with pytest.raises(presets.PresetError):
            presets.store_and_verify(client, "test", STATION, 2, "relay", "http://127.0.0.1:8092")


def test_remove_preset():
    with FakeSpeaker() as speaker:
        speaker.state["presets"][1] = {"source": "LOCAL_INTERNET_RADIO", "location": "http://x/1.json", "name": "One"}
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        client.remove_preset(1)
        assert 1 not in speaker.state["presets"]


def test_play_station_success_and_timeout():
    with FakeSpeaker() as speaker:
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        result = presets.play_station(client, "test", STATION, "relay", "http://127.0.0.1:8092", timeout=2)
        assert result["status"] == "ok"
        assert result["nowPlaying"]["source"] == "LOCAL_INTERNET_RADIO"

        speaker.state["select_mode"] = "ignore"
        failed = presets.play_station(client, "test", STATION, "relay", "http://127.0.0.1:8092", timeout=0.3)
        assert failed["status"] == "error"


def test_now_playing_error_is_reported():
    with FakeSpeaker() as speaker:
        speaker.state["inactive"] = True
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        now = client.now_playing()
        assert now["error"]
        assert now["errorName"] == "EVENT_IN_WRONG_STATE"


def test_play_wakes_an_inactive_speaker():
    with FakeSpeaker() as speaker:
        speaker.state["inactive"] = True
        client = SpeakerClient("127.0.0.1", speaker.port, timeout=3)
        result = presets.play_station(client, "test", STATION, "relay", "http://127.0.0.1:8092", timeout=3)
        assert result["status"] == "ok"
        assert ("press", "POWER") in speaker.state["keys"]
        assert ("release", "POWER") in speaker.state["keys"]


def test_now_playing_matching_rejects_stale_location():
    item = {"source": "LOCAL_INTERNET_RADIO", "location": "http://proxy:8092/test.json"}
    stale = {
        "source": "LOCAL_INTERNET_RADIO",
        "location": "http://proxy:8092/other.json",
        "stationName": "Other",
        "playStatus": "PLAY_STATE",
    }
    fresh = {
        "source": "LOCAL_INTERNET_RADIO",
        "location": "http://proxy:8092/test.json",
        "stationName": "Test",
        "playStatus": "PLAY_STATE",
    }
    assert not presets._now_playing_matches(stale, item)
    assert presets._now_playing_matches(fresh, item)
