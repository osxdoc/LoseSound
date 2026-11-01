from __future__ import annotations

from losesound.stations import (
    build_station_response,
    is_tokenized,
    probe_station,
    resolve_stream,
    slugify,
    unique_station_name,
    validate_stream_url,
)
from tests.helpers import FakeUpstream


def test_slugify_and_unique_name():
    assert slugify("HR3") == "hr3"
    assert slugify("90s90s House HQ") == "90s90s_house_hq"
    assert slugify("!!!") == "station"
    assert unique_station_name("HR3", set()) == "hr3"
    assert unique_station_name("HR3", {"hr3"}) == "hr3_2"
    assert unique_station_name("HR3", {"hr3", "hr3_2"}) == "hr3_3"


def test_validate_stream_url():
    assert validate_stream_url("https://example.com/stream")[0]
    assert validate_stream_url("http://192.168.1.10:8000/stream")[0]
    assert not validate_stream_url("file:///etc/passwd")[0]
    assert not validate_stream_url("ftp://example.com/x")[0]
    assert not validate_stream_url("http://169.254.169.254/latest/meta-data")[0]
    assert not validate_stream_url("")[0]


def test_token_detection():
    assert is_tokenized("https://host/stream?token=abc")
    assert is_tokenized("https://host/stream?sABC=1&skey=2")
    assert is_tokenized("https://host/stream?hdnts=exp=1")
    assert not is_tokenized("https://host/stream")
    assert not is_tokenized("https://host/stream?playerid=x")


def test_build_station_response_contains_failover_streams():
    payload = build_station_response("HR3", "https://img/logo.png", ["http://a", "http://b"], "speaker")
    assert payload["name"] == "HR3"
    assert payload["imageUrl"] == "https://img/logo.png"
    assert payload["streamType"] == "liveRadio"
    assert payload["audio"]["streamUrl"] == "http://a"
    assert [stream["streamUrl"] for stream in payload["audio"]["streams"]] == ["http://a", "http://b"]
    assert payload["playbackMode"] == "speaker"


def test_resolve_and_probe_follow_redirects():
    with FakeUpstream() as upstream:
        result = resolve_stream(upstream.url + "/redirect", timeout=3)
        assert result["ok"] is True
        assert result["resolvedUrl"].endswith("/stream")
        assert result["redirected"] is True
        assert result["contentType"] == "audio/mpeg"
        assert result["bitrateKbps"] == 128

        probe = probe_station({"name": "Stable", "dispatcherUrl": upstream.url + "/stream"})
        assert probe["ok"] is True
        assert probe["tokenized"] is False
        assert probe["recommendation"] == "speaker"

        token_probe = probe_station({"name": "Token", "dispatcherUrl": upstream.url + "/token"})
        assert token_probe["ok"] is True
        assert token_probe["tokenized"] is True
        assert token_probe["recommendation"] == "relay"


def test_probe_failure_recommends_relay():
    probe = probe_station({"name": "Broken", "dispatcherUrl": "http://127.0.0.1:1/nothing"}, timeout=1)
    assert probe["ok"] is False
    assert probe["recommendation"] == "relay"
    assert probe["error"]
