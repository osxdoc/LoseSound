from __future__ import annotations

import http.client
import io
import threading
import time

from losesound.relay import MAX_CHUNK, RelaySession, StreamRegistry
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
        app.store.update(lambda config: config["settings"].update({"bufferSeconds": 2, "prebufferSeconds": 1}))
        holder = {}

        def read():
            holder["result"] = _read_some(app.port, "/test", 2.0)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        active = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _status, live, _ = request(app.port, "/api/proxy/status")
            if live["activeStreams"] and live["activeStreams"][0].get("bytesOut", 0) > 0:
                active = live["activeStreams"][0]
                break
            time.sleep(0.1)
        assert active is not None, "relay stream should be active and send bytes"
        assert active["bufferFillPercent"] >= 5, active
        assert active["underrunCount"] <= 2, active
        assert active["upstreamReadGapMs"] < 3000, active
        assert active["maxUnderrunMs"] < 3000, active
        assert active["bytesOut"] > 0
        reader.join(timeout=5)
        status, received, content_type = holder["result"]
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


class _FakeHandler:
    client_address = ("127.0.0.1", 12345)

    def __init__(self):
        self.wfile = io.BytesIO()


def test_metrics_use_a_consistent_clock():
    session = RelaySession(
        _FakeHandler(),
        "test",
        {"dispatcherUrl": "http://example.invalid/stream", "bitrateKbps": 128},
        {"bufferSeconds": 2, "prebufferSeconds": 1, "bitrateKbps": 128, "reconnectBackoffMs": [100]},
        StreamRegistry(),
    )
    now = time.monotonic()
    session._started = now - 10
    with session.buffer_lock:
        session.bytes_in = 4096
        session.bytes_out = 2048
        session._last_in_at = now - 0.5
        session._last_in_wall = time.time() - 0.5
        session._last_out_at = now - 0.5
        session._last_out_wall = time.time() - 0.5
        session.underrun_started_at = now - 1.0
        session.underrun_count = 2
        session.max_underrun_ms = 800
        session.buffer_bytes = MAX_CHUNK
        session.buffer.append(b"x" * MAX_CHUNK)
    metrics = session._metrics()
    assert 0 <= metrics["upstreamReadGapMs"] < 2000
    assert 0 <= metrics["currentUnderrunMs"] < 2000
    assert metrics["maxUnderrunMs"] < 2000
    assert metrics["lastBytesInAt"] is not None
    assert metrics["lastBytesOutAt"] is not None
