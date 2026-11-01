from __future__ import annotations

from tests.helpers import AppServer, FakeAfterTouch, FakeSpeaker, FakeUpstream, request


def _setup(tmp_path):
    upstream = FakeUpstream(duration=10)
    speaker = FakeSpeaker()
    aftertouch = FakeAfterTouch()
    upstream.__enter__()
    speaker.__enter__()
    aftertouch.__enter__()
    speaker.state["marge_url"] = aftertouch.url + "/marge"
    app = AppServer(tmp_path, speaker, aftertouch, station_url=upstream.url + "/stream")
    app.__enter__()
    return upstream, speaker, aftertouch, app


def _teardown(*contexts):
    for context in reversed(contexts):
        context.__exit__(None, None, None)


def test_health_index_and_static(tmp_path):
    contexts = _setup(tmp_path)
    app = contexts[3]
    try:
        status, data, content_type = request(app.port, "/api/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["version"]
        assert "settings" in data

        status, data, content_type = request(app.port, "/")
        assert status == 200
        assert "text/html" in content_type
        assert "<title>LoseSound</title>" in data

        status, data, content_type = request(app.port, "/static/app.js")
        assert status == 200
        assert "javascript" in content_type

        status, _data, _content_type = request(app.port, "/static/../etc/passwd")
        assert status == 404
    finally:
        _teardown(*contexts)


def test_settings_roundtrip(tmp_path):
    contexts = _setup(tmp_path)
    app = contexts[3]
    try:
        status, data, _ = request(app.port, "/api/settings")
        assert status == 200
        assert data["bufferSeconds"] == 1

        status, data, _ = request(
            app.port,
            "/api/settings",
            method="POST",
            body={
                "bufferSeconds": 6,
                "prebufferSeconds": 2,
                "bitrateKbps": 192,
                "reconnectBackoffMs": [100, 500],
                "defaultPresetMode": "speaker",
            },
        )
        assert status == 200
        assert data["settings"]["bufferSeconds"] == 6
        assert data["settings"]["defaultPresetMode"] == "speaker"

        status, data, _ = request(app.port, "/api/settings")
        assert data["bufferSeconds"] == 6
    finally:
        _teardown(*contexts)


def test_station_crud_and_validation(tmp_path):
    contexts = _setup(tmp_path)
    app = contexts[3]
    try:
        status, data, _ = request(app.port, "/api/stations")
        assert status == 200
        assert [station["name"] for station in data["stations"]] == ["test"]

        status, data, _ = request(
            app.port,
            "/api/stations",
            method="POST",
            body={"displayName": "Neue Welle", "dispatcherUrl": "http://192.168.1.9/stream"},
        )
        assert status == 200
        assert data["name"] == "neue_welle"

        status, data, _ = request(
            app.port, "/api/stations", method="POST", body={"displayName": "Bad", "dispatcherUrl": "file:///etc/passwd"}
        )
        assert status == 400

        status, data, _ = request(
            app.port,
            "/api/stations",
            method="POST",
            body={"displayName": "Test Station", "dispatcherUrl": "http://x/y"},
        )
        assert status == 409

        status, data, _ = request(app.port, "/api/stations/neue_welle", method="DELETE")
        assert status == 200
        assert data["deleted"] == "neue_welle"
        status, data, _ = request(app.port, "/api/stations")
        assert [station["name"] for station in data["stations"]] == ["test"]
    finally:
        _teardown(*contexts)


def test_preset_set_verify_and_station_json(tmp_path):
    contexts = _setup(tmp_path)
    speaker, app = contexts[1], contexts[3]
    try:
        status, data, _ = request(app.port, "/api/stations/test/preset/2", method="POST", body={"mode": "relay"})
        assert status == 200, data
        assert data["mode"] == "relay"
        stored = speaker.state["presets"][2]
        assert stored["location"] == app.ctx.base_url + "/test.json"
        assert stored["source"] == "LOCAL_INTERNET_RADIO"

        status, stations, _ = request(app.port, "/api/stations")
        station = stations["stations"][0]
        assert station["presets"] == [2]
        assert station["presetModes"] == {"2": "relay"}

        status, payload, content_type = request(app.port, "/test.json")
        assert status == 200
        assert "application/json" in content_type
        assert payload["audio"]["streamUrl"] == app.ctx.base_url + "/test"

        status, data, _ = request(app.port, "/api/stations/test/preset/2", method="POST", body={"mode": "speaker"})
        assert status == 200
        stored = speaker.state["presets"][2]
        assert stored["location"] == app.ctx.base_url + "/test.json?mode=speaker"

        status, payload, _ = request(app.port, "/test.json?mode=speaker")
        assert status == 200
        streams = [item["streamUrl"] for item in payload["audio"]["streams"]]
        assert streams[0].endswith("/stream")

        status, data, _ = request(app.port, "/api/presets/2/remove", method="POST")
        assert status == 200
        assert 2 not in speaker.state["presets"]
        status, stations, _ = request(app.port, "/api/stations")
        assert stations["stations"][0]["presets"] == []
    finally:
        _teardown(*contexts)


def test_preset_reports_speaker_mismatch(tmp_path):
    contexts = _setup(tmp_path)
    speaker, app = contexts[1], contexts[3]
    try:
        speaker.state["mismatch_location"] = "http://wrong/location.json"
        status, data, _ = request(app.port, "/api/stations/test/preset/1", method="POST", body={"mode": "relay"})
        assert status == 400
        assert "instead of" in data["message"]
    finally:
        _teardown(*contexts)


def test_play_endpoint(tmp_path):
    contexts = _setup(tmp_path)
    app = contexts[3]
    try:
        status, data, _ = request(app.port, "/api/stations/test/play", method="POST", body={"mode": "relay"})
        assert status == 200
        assert data["status"] == "ok"
        assert data["nowPlaying"]["source"] == "LOCAL_INTERNET_RADIO"
    finally:
        _teardown(*contexts)


def test_api_token_protects_writes(tmp_path):
    upstream = FakeUpstream(duration=5)
    speaker = FakeSpeaker()
    aftertouch = FakeAfterTouch()
    for context in (upstream, speaker, aftertouch):
        context.__enter__()
    app = AppServer(tmp_path, speaker, aftertouch, api_token="secret", station_url=upstream.url + "/stream")
    app.__enter__()
    try:
        status, _data, _ = request(app.port, "/api/settings", method="POST", body={"bufferSeconds": 4})
        assert status == 401

        status, _data, _ = request(app.port, "/api/settings", method="POST", body={"bufferSeconds": 4}, token="wrong")
        assert status == 401

        status, data, _ = request(app.port, "/api/settings", method="POST", body={"bufferSeconds": 4}, token="secret")
        assert status == 200
        assert data["settings"]["bufferSeconds"] == 4

        status, _data, _ = request(app.port, "/api/health")
        assert status == 200
    finally:
        app.__exit__(None, None, None)
        _teardown(upstream, speaker, aftertouch)


def test_setup_status_surfaces_findings(tmp_path):
    contexts = _setup(tmp_path)
    speaker, aftertouch, app = contexts[1], contexts[2], contexts[3]
    try:
        speaker.state["sources"] = ["TUNEIN"]
        speaker.state["marge_status"] = 502
        aftertouch.state["orion_ok"] = False
        status, data, _ = request(app.port, "/api/setup/status?refresh=1", timeout=20)
        assert status == 200
        codes = {finding["code"] for finding in data["findings"]}
        assert "sources_missing" in codes
        assert "marge_unreachable" in codes
        assert "orion_unavailable" in codes
        assert data["aftertouch"]["version"] == "0.138.1"
    finally:
        _teardown(*contexts)


def test_radio_browser_and_orion_preset_modes(tmp_path):
    contexts = _setup(tmp_path)
    speaker, app = contexts[1], contexts[3]
    try:
        status, data, _ = request(app.port, "/api/stations/test/preset/3", method="POST", body={"mode": "radioBrowser"})
        assert status == 200, data
        assert speaker.state["presets"][3]["source"] == "RADIO_BROWSER"
        assert speaker.state["presets"][3]["location"] == "/stations/byuuid/uuid-1"

        status, data, _ = request(app.port, "/api/stations/test/preset/4", method="POST", body={"mode": "orion"})
        assert status == 200, data
        assert speaker.state["presets"][4]["source"] == "LOCAL_INTERNET_RADIO"
        assert speaker.state["presets"][4]["location"].startswith("/station?data=")
    finally:
        _teardown(*contexts)


def test_catalog_add_with_preset_and_delete_cleanup(tmp_path):
    contexts = _setup(tmp_path)
    speaker, app = contexts[1], contexts[3]
    try:
        body = {
            "displayName": "Katalog Sender",
            "dispatcherUrl": "http://192.168.1.20/stream",
            "radioBrowserUuid": "uuid-9",
            "artwork": "http://192.168.1.20/logo.png",
            "preset": 5,
            "mode": "relay",
        }
        status, data, _ = request(app.port, "/api/catalog/add", method="POST", body=body)
        assert status == 200, data
        name = data["name"]
        assert name == "katalog_sender"
        assert speaker.state["presets"][5]["source"] == "LOCAL_INTERNET_RADIO"

        status, data, _ = request(app.port, f"/api/stations/{name}?removePresets=1", method="DELETE")
        assert status == 200
        assert 5 not in speaker.state["presets"]
        status, stations, _ = request(app.port, "/api/stations")
        assert name not in [station["name"] for station in stations["stations"]]
    finally:
        _teardown(*contexts)
