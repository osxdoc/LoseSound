from __future__ import annotations

import http.client
import time

from tests.helpers import AppServer, FakeAfterTouch, FakeSpeaker, FakeUpstream, request


def _read_some(port: int, path: str, seconds: float) -> tuple[int, int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    deadline = time.monotonic() + seconds
    total = 0
    try:
        while time.monotonic() < deadline:
            chunk = response.read1(4096)
            if not chunk:
                break
            total += len(chunk)
    finally:
        connection.close()
    return response.status, total, response.getheader("Content-Type", "")


def test_relay_streams_audio_and_records_stats(tmp_path):
    upstream = FakeUpstream(duration=4, interval=0.05, chunk_size=1024, bitrate_kbps=128)
    speaker = FakeSpeaker()
    aftertouch = FakeAfterTouch()
    for context in (upstream, speaker, aftertouch):
        context.__enter__()
    app = AppServer(tmp_path, speaker, aftertouch, station_url=upstream.url + "/stream")
    app.__enter__()
    try:
        status, received, content_type = _read_some(app.port, "/test", 1.2)
        assert status == 200
        assert "audio/mpeg" in content_type
        assert received > 0

        deadline = time.monotonic() + 3
        stats = None
        while time.monotonic() < deadline:
            _status, data, _ = request(app.port, "/api/proxy/status")
            stats = data
            if not data["activeStreams"] and any(item["station"] == "test" for item in data["stationStats"]):
                break
            time.sleep(0.1)
        assert stats is not None
        recent = next(item for item in stats["stationStats"] if item["station"] == "test")
        assert recent["bytesOut"] >= received
        assert recent["state"] in ("ended", "client-disconnect")
        assert recent["bufferTargetBytes"] > 0
    finally:
        app.__exit__(None, None, None)
        for context in (upstream, speaker, aftertouch):
            context.__exit__(None, None, None)


def test_relay_reconnects_after_upstream_eof(tmp_path):
    upstream = FakeUpstream(duration=0.5, interval=0.02, chunk_size=1024, bitrate_kbps=128)
    speaker = FakeSpeaker()
    aftertouch = FakeAfterTouch()
    for context in (upstream, speaker, aftertouch):
        context.__enter__()
    app = AppServer(tmp_path, speaker, aftertouch, station_url=upstream.url + "/stream")
    app.__enter__()
    try:
        _read_some(app.port, "/test", 1.5)
        deadline = time.monotonic() + 3
        reconnects = 0
        while time.monotonic() < deadline:
            _status, data, _ = request(app.port, "/api/proxy/status")
            values = [item.get("reconnects", 0) for item in data["activeStreams"]]
            values += [item.get("reconnects", 0) for item in data["stationStats"] if item["station"] == "test"]
            reconnects = max(values) if values else 0
            if reconnects >= 1:
                break
            time.sleep(0.1)
        assert reconnects >= 1
    finally:
        app.__exit__(None, None, None)
        for context in (upstream, speaker, aftertouch):
            context.__exit__(None, None, None)
