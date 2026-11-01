"""Audio relay with a small ring buffer and real-time pacing.

The speaker requests ``/<station>`` and expects a continuous MP3/AAC byte
stream. We keep a configurable ring buffer between the upstream station and the
speaker, pace the downstream side at the measured stream bitrate and reconnect
upstream when the station drops the connection.
"""

from __future__ import annotations

import collections
import contextlib
import copy
import threading
import time
import urllib.error
import urllib.request

from .log import log, redact_url
from .stations import STREAM_USER_AGENT, bitrate_from_headers

MAX_CHUNK = 8192
UPSTREAM_TIMEOUT = 20.0
DOWNSTREAM_TIMEOUT = 30.0
HEALTH_INTERVAL = 5.0
PUBLISH_INTERVAL = 0.5
# Gaps shorter than this are normal bursty upstream delivery; the speaker buffers them.
UNDERRUN_ALERT_MS = 500


class StreamRegistry:
    """Shared stream statistics for the API and the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, dict] = {}
        self._stations: dict[str, dict] = {}

    # ------------------------------------------------------------- streams
    def register(self, stream_id: str, station: str, client: str) -> None:
        with self._lock:
            self._active[stream_id] = {
                "streamId": stream_id,
                "station": station,
                "client": client,
                "startedAt": int(time.time()),
                "bytesIn": 0,
                "bytesOut": 0,
                "reconnects": 0,
                "state": "starting",
                "lastError": "",
            }

    def update_stream(self, stream_id: str, **updates) -> None:
        with self._lock:
            item = self._active.get(stream_id)
            if item is not None:
                item.update({key: value for key, value in updates.items() if value is not None})
                item["updatedAt"] = int(time.time())

    def unregister(self, stream_id: str) -> None:
        with self._lock:
            self._active.pop(stream_id, None)

    # ------------------------------------------------------------ stations
    def update_station(self, station: str, **updates) -> None:
        with self._lock:
            item = self._stations.setdefault(station, {})
            item.update({key: value for key, value in updates.items() if value is not None})
            item["updatedAt"] = int(time.time())

    def append_event(
        self, station: str, side: str, event: str, message: str = "", stream_id: str | None = None, **details
    ) -> None:
        record = {"at": int(time.time()), "side": side, "event": event}
        if message:
            record["message"] = str(message)
        if stream_id:
            record["streamId"] = stream_id
        for key, value in details.items():
            if value is not None:
                record[key] = value
        with self._lock:
            item = self._stations.setdefault(station, {})
            history = list(item.get("streamEvents", []))
            history.append(record)
            item["streamEvents"] = history[-30:]
            item["lastEvent"] = record
            item["lastEventSide"] = side
            item["lastEventType"] = event
            item["lastEventMessage"] = record.get("message", "")
            item["updatedAt"] = record["at"]

    def status(self, station_names: list[str]) -> dict:
        now = int(time.time())
        with self._lock:
            active = copy.deepcopy(list(self._active.values()))
            stats = copy.deepcopy(self._stations)

        for item in active:
            item["durationSeconds"] = max(0, now - item.get("startedAt", now))

        station_stats = []
        for name in sorted(set(station_names) | set(stats.keys())):
            item = stats.get(name, {})
            if not item or not item.get("endedAt"):
                continue
            item["station"] = name
            if item.get("updatedAt"):
                item["ageSeconds"] = max(0, now - item["updatedAt"])
            if item.get("startedAt"):
                item["durationSeconds"] = max(0, item["endedAt"] - item["startedAt"])
            station_stats.append(item)

        return {"status": "ok", "now": now, "activeStreams": active, "stationStats": station_stats}


class RelaySession:
    def __init__(
        self,
        handler,
        station_name: str,
        station: dict,
        settings: dict,
        registry: StreamRegistry,
        log_chunks: bool = False,
    ) -> None:
        self.handler = handler
        self.station_name = station_name
        self.station = station
        self.settings = settings
        self.registry = registry
        self.log_chunks = log_chunks
        self.client_ip = handler.client_address[0] if handler.client_address else ""
        self.stream_id = f"{station_name}-{int(time.time() * 1000)}-{threading.get_ident()}"

        bitrate_kbps = int(station.get("bitrateKbps") or settings.get("bitrateKbps") or 128)
        self.rate_configured = max(4000.0, bitrate_kbps * 1000.0 / 8.0)
        buffer_seconds = float(settings.get("bufferSeconds") or 3)
        self.buffer_target = max(MAX_CHUNK * 4, int(buffer_seconds * self.rate_configured))
        self.max_chunks = max(8, self.buffer_target // MAX_CHUNK)
        self.prebuffer_seconds = float(settings.get("prebufferSeconds") or 0)

        self.buffer: collections.deque[bytes] = collections.deque(maxlen=self.max_chunks)
        self.buffer_lock = threading.Lock()
        self.buffer_bytes = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.overflow_count = 0
        self.underrun_count = 0
        self.underrun_started_at: float | None = None
        self.max_underrun_ms = 0
        self.reconnects = 0
        self.last_error = ""
        self.state = "starting"
        self.content_type = "audio/mpeg"
        self.resolved_url = station.get("dispatcherUrl", "")
        self.upstream_status: int | None = None
        self.measured_rate = 0.0
        self._rate_samples: collections.deque[tuple[float, int]] = collections.deque(maxlen=128)
        self._last_in_at: float | None = None
        self._last_out_at: float | None = None
        self._last_in_wall: float | None = None
        self._last_out_wall: float | None = None
        self._last_fill_log = 0.0
        self._last_publish = 0.0
        self._last_health = time.monotonic()
        self._started = time.monotonic()
        self.ready = threading.Event()
        self.done = threading.Event()
        self.producer_thread: threading.Thread | None = None
        self._reconnect_history: list[dict] = []

    # ---------------------------------------------------------------- model
    def _drain_rate(self) -> float:
        if self.measured_rate > 0:
            return max(2000.0, min(1_000_000.0, self.measured_rate))
        return self.rate_configured

    def _metrics(self, state: str | None = None) -> dict:
        now = time.monotonic()
        with self.buffer_lock:
            buffered_bytes = self.buffer_bytes
            chunks = len(self.buffer)
            bytes_in = self.bytes_in
            bytes_out = self.bytes_out
            last_in = self._last_in_at
            last_in_wall = self._last_in_wall
            last_out_wall = self._last_out_wall
            underrun_started = self.underrun_started_at
            underrun_count = self.underrun_count
            max_underrun_ms = self.max_underrun_ms
            overflow_count = self.overflow_count

        elapsed = max(0.001, now - self._started)
        current_underrun_ms = 0
        if underrun_started is not None:
            current_underrun_ms = int((now - underrun_started) * 1000)

        metrics = {
            "bytesIn": bytes_in,
            "bytesOut": bytes_out,
            "bufferedBytes": buffered_bytes,
            "bufferedChunks": chunks,
            "bufferFillPercent": round((buffered_bytes / self.buffer_target) * 100, 1) if self.buffer_target else 0,
            "bufferTargetBytes": self.buffer_target,
            "underrunCount": underrun_count,
            "currentUnderrunMs": current_underrun_ms,
            "maxUnderrunMs": max(max_underrun_ms, current_underrun_ms),
            "overflowCount": overflow_count,
            "upstreamReadGapMs": int((now - last_in) * 1000) if last_in else None,
            "producerBytesPerSecond": round(bytes_in / elapsed),
            "consumerBytesPerSecond": round(bytes_out / elapsed),
            "lastBytesInAt": int(last_in_wall) if last_in_wall else None,
            "lastBytesOutAt": int(last_out_wall) if last_out_wall else None,
        }
        if state:
            metrics["state"] = state
        return metrics

    def _publish(self, state: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish < PUBLISH_INTERVAL:
            return
        self._last_publish = now
        metrics = self._metrics(state)
        self.registry.update_stream(self.stream_id, **metrics)
        self.registry.update_station(self.station_name, **metrics)

    def _set_state(self, state: str, **extra) -> None:
        self.state = state
        updates = {"state": state, **extra}
        self.registry.update_stream(self.stream_id, **updates)
        self.registry.update_station(self.station_name, **updates)
        self._publish(state, force=True)

    def _log_health(self) -> None:
        now = time.monotonic()
        if now - self._last_health < HEALTH_INTERVAL:
            return
        self._last_health = now
        metrics = self._metrics()
        log(
            "stream.health",
            stream_id=self.stream_id,
            station=self.station_name,
            bytes_in=metrics["bytesIn"],
            bytes_out=metrics["bytesOut"],
            buffered_bytes=metrics["bufferedBytes"],
            buffer_fill_percent=metrics["bufferFillPercent"],
            upstream_read_gap_ms=metrics["upstreamReadGapMs"],
            underrun_count=metrics["underrunCount"],
            producer_bps=metrics["producerBytesPerSecond"],
            consumer_bps=metrics["consumerBytesPerSecond"],
        )

    # ------------------------------------------------------------- producer
    def _sample_rate(self, length: int, now: float) -> None:
        self._rate_samples.append((now, self.bytes_in))
        while self._rate_samples and now - self._rate_samples[0][0] > 5.0:
            self._rate_samples.popleft()
        if len(self._rate_samples) < 2:
            return
        first_time, first_bytes = self._rate_samples[0]
        span = now - first_time
        if span < 2.0:
            return
        rate = (self.bytes_in - first_bytes) / span
        if rate > 0:
            self.measured_rate = rate

    def _producer(self) -> None:
        backoff_ms = list(self.settings.get("reconnectBackoffMs") or [250, 500, 1000, 2000, 3000])
        backoff_idx = 0
        station = self.station
        headers = {"User-Agent": STREAM_USER_AGENT, "Accept": "*/*"}
        headers.update(station.get("headers") or {})

        while not self.done.is_set():
            retry_reason = ""
            try:
                request = urllib.request.Request(station.get("dispatcherUrl", ""), headers=headers)
                with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                    backoff_idx = 0
                    self.content_type = (response.headers.get("Content-Type") or "audio/mpeg").split(";")[0].strip()
                    self.resolved_url = response.url
                    self.upstream_status = response.status
                    detected = bitrate_from_headers({key.lower(): value for key, value in response.headers.items()})
                    log(
                        "stream.upstream.open",
                        stream_id=self.stream_id,
                        station=self.station_name,
                        status=response.status,
                        content_type=self.content_type,
                        resolved=redact_url(response.url),
                    )
                    self._set_state(
                        "upstream-open",
                        upstreamStatus=response.status,
                        resolvedUrl=response.url,
                        detectedBitrateKbps=detected,
                    )
                    while not self.done.is_set():
                        try:
                            chunk = response.read(MAX_CHUNK)
                        except (urllib.error.URLError, OSError, TimeoutError) as exc:
                            retry_reason = f"upstream read error: {exc}"
                            break
                        if not chunk:
                            retry_reason = "upstream returned EOF"
                            break

                        now = time.monotonic()
                        overflowed = 0
                        with self.buffer_lock:
                            if len(self.buffer) == self.max_chunks and self.buffer:
                                overflowed = len(self.buffer[0])
                                self.buffer_bytes = max(0, self.buffer_bytes - overflowed)
                                self.overflow_count += 1
                            self.buffer.append(chunk)
                            self.buffer_bytes += len(chunk)
                            self.bytes_in += len(chunk)
                            self._last_in_at = now
                            self._last_in_wall = time.time()
                            buffered = len(self.buffer)
                        self._sample_rate(len(chunk), now)
                        self.ready.set()

                        if overflowed:
                            message = "ring buffer full; dropped oldest audio chunk"
                            if now - self._last_fill_log >= 5.0:
                                self._last_fill_log = now
                                log(
                                    "stream.buffer.overflow",
                                    stream_id=self.stream_id,
                                    station=self.station_name,
                                    dropped_bytes=overflowed,
                                    overflow_count=self.overflow_count,
                                )
                            self.registry.append_event(
                                self.station_name,
                                "proxy",
                                "buffer-overflow",
                                message,
                                self.stream_id,
                                droppedBytes=overflowed,
                                overflowCount=self.overflow_count,
                            )
                        if self.log_chunks and self.bytes_in % (256 * 1024) < len(chunk):
                            log(
                                "stream.producer.bytes",
                                stream_id=self.stream_id,
                                bytes_in=self.bytes_in,
                                buffered_chunks=buffered,
                            )
                        self._publish("streaming")
            except Exception as exc:  # noqa: BLE001 - reported through station stats
                retry_reason = str(exc)

            if self.done.is_set():
                break

            self.last_error = retry_reason or "upstream lost"
            log("stream.upstream.error", stream_id=self.stream_id, station=self.station_name, message=self.last_error)
            self._set_state("upstream-error", lastError=self.last_error)
            self.registry.append_event(self.station_name, "upstream", "upstream-error", self.last_error, self.stream_id)

            backoff = backoff_ms[min(backoff_idx, len(backoff_ms) - 1)] / 1000.0
            backoff_idx += 1
            self.reconnects += 1
            self._reconnect_history.append(
                {"at": int(time.time()), "side": "upstream", "reason": self.last_error, "backoffSeconds": backoff}
            )
            self._reconnect_history = self._reconnect_history[-20:]
            self.registry.update_station(
                self.station_name, reconnects=self.reconnects, reconnectHistory=self._reconnect_history
            )
            self._set_state("reconnecting", reconnects=self.reconnects, backoffSeconds=backoff)
            log("stream.upstream.retry", stream_id=self.stream_id, station=self.station_name, backoff_seconds=backoff)
            self._sleep(backoff)

        self.ready.set()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.done.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

    # ------------------------------------------------------------- consumer
    def _write_headers(self) -> None:
        self.handler.send_response(200)
        content_type = self.content_type if self.content_type.startswith("audio/") else "audio/mpeg"
        self.handler.send_header("Content-Type", content_type)
        self.handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.handler.send_header("Pragma", "no-cache")
        self.handler.send_header("Connection", "close")
        self.handler.end_headers()
        self.handler.close_connection = True

    def _client_disconnected(self, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: downstream client closed the connection"
        log(
            "stream.client.disconnect",
            stream_id=self.stream_id,
            station=self.station_name,
            bytes_out=self.bytes_out,
            message=message,
        )
        self.registry.update_station(self.station_name, lastClientDisconnect=message)
        self.registry.append_event(
            self.station_name, "client", "client-disconnect", message, self.stream_id, bytesOut=self.bytes_out
        )
        self._set_state("client-disconnect", lastClientDisconnect=message)

    def run(self) -> None:
        self.registry.register(self.stream_id, self.station_name, self.client_ip)
        self.registry.update_station(
            self.station_name,
            state="starting",
            client=self.client_ip,
            startedAt=int(time.time()),
            bytesIn=0,
            bytesOut=0,
            reconnects=0,
            lastError="",
            endedAt=0,
            reconnectHistory=[],
            streamEvents=[],
            lastEvent={},
            lastEventSide="",
            lastEventType="",
            lastEventMessage="",
            bufferedBytes=0,
            bufferedChunks=0,
            bufferFillPercent=0,
            bufferTargetBytes=self.buffer_target,
            underrunCount=0,
            currentUnderrunMs=0,
            maxUnderrunMs=0,
            overflowCount=0,
        )
        log(
            "stream.start",
            stream_id=self.stream_id,
            station=self.station_name,
            client=self.client_ip,
            buffer_target_bytes=self.buffer_target,
            buffer_seconds=self.settings.get("bufferSeconds"),
            prebuffer_seconds=self.prebuffer_seconds,
            bitrate_kbps=int(self.rate_configured * 8 / 1000),
            max_chunks=self.max_chunks,
        )

        self.producer_thread = threading.Thread(target=self._producer, daemon=True, name=f"relay-{self.station_name}")
        self.producer_thread.start()

        prebuffer_target = min(self.buffer_target, int(self.prebuffer_seconds * self._drain_rate()))
        deadline = time.monotonic() + max(3.0, self.prebuffer_seconds + 3.0)
        while time.monotonic() < deadline:
            with self.buffer_lock:
                buffered = self.buffer_bytes
            if buffered >= max(MAX_CHUNK, prebuffer_target) or self.done.is_set():
                break
            if self.ready.is_set() and self.prebuffer_seconds <= 0:
                break
            time.sleep(0.02)

        with self.buffer_lock:
            buffered = self.buffer_bytes

        if buffered == 0:
            self.done.set()
            self._join_producer()
            message = "upstream did not produce data before timeout"
            log("stream.error", stream_id=self.stream_id, station=self.station_name, message=message)
            self._set_state("error", lastError=message)
            self.registry.append_event(self.station_name, "proxy", "upstream-timeout", message, self.stream_id)
            self.registry.unregister(self.stream_id)
            with contextlib.suppress(OSError):
                self.handler.send_error(502, "Upstream stream did not produce data")
            return

        try:
            self._write_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.done.set()
            self._join_producer()
            self.registry.unregister(self.stream_id)
            return

        self._set_state("streaming")
        capacity = float(self.buffer_target)
        # Send one chunk right away, keep the rest of the prebuffer as cushion.
        tokens = float(min(capacity, MAX_CHUNK))
        last = time.monotonic()
        underrun_started: float | None = None
        underrun_counted = False

        try:
            while True:
                now = time.monotonic()
                rate = self._drain_rate()
                tokens = min(capacity, tokens + rate * (now - last))
                last = now

                chunk: bytes | None = None
                with self.buffer_lock:
                    if self.buffer:
                        head = len(self.buffer[0])
                        if tokens >= head:
                            chunk = self.buffer.popleft()
                            self.buffer_bytes = max(0, self.buffer_bytes - head)
                            tokens -= head
                    elif self.done.is_set():
                        break

                if chunk is not None:
                    try:
                        self.handler.wfile.write(chunk)
                        self.handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
                        self._client_disconnected(exc)
                        break
                    with self.buffer_lock:
                        self.bytes_out += len(chunk)
                        self._last_out_at = now
                        self._last_out_wall = time.time()
                        buffered_now = self.buffer_bytes
                    if underrun_started is not None:
                        duration_ms = int((now - underrun_started) * 1000)
                        self.max_underrun_ms = max(self.max_underrun_ms, duration_ms)
                        underrun_started = None
                        with self.buffer_lock:
                            self.underrun_started_at = None
                        log(
                            "stream.buffer.underrun.end",
                            stream_id=self.stream_id,
                            station=self.station_name,
                            duration_ms=duration_ms,
                            buffered_chunks=len(self.buffer),
                        )
                        self.registry.append_event(
                            self.station_name,
                            "proxy",
                            "buffer-underrun-end",
                            f"buffer refilled after {duration_ms}ms",
                            self.stream_id,
                            durationMs=duration_ms,
                        )
                    if self.log_chunks and self.bytes_out % (256 * 1024) < len(chunk):
                        log(
                            "stream.consumer.bytes",
                            stream_id=self.stream_id,
                            bytes_out=self.bytes_out,
                            buffered_chunks=buffered_now,
                        )
                    self._publish("streaming")
                    continue

                with self.buffer_lock:
                    empty = not self.buffer
                    head = len(self.buffer[0]) if self.buffer else 0
                if empty and not self.done.is_set():
                    if underrun_started is None:
                        underrun_started = now
                        underrun_counted = False
                    elif not underrun_counted and int((now - underrun_started) * 1000) >= UNDERRUN_ALERT_MS:
                        underrun_counted = True
                        self.underrun_count += 1
                        with self.buffer_lock:
                            self.underrun_started_at = underrun_started
                        message = "proxy buffer is empty while upstream producer is still active"
                        log(
                            "stream.buffer.underrun.start",
                            stream_id=self.stream_id,
                            station=self.station_name,
                            underrun_count=self.underrun_count,
                        )
                        self.registry.append_event(
                            self.station_name,
                            "proxy",
                            "buffer-underrun-start",
                            message,
                            self.stream_id,
                            underrunCount=self.underrun_count,
                        )
                        self._set_state("buffer-underrun")
                    time.sleep(0.03)
                elif head:
                    need = max(0.005, (head - tokens) / max(1.0, rate))
                    time.sleep(min(0.05, need))
                else:
                    time.sleep(0.01)

                self._publish()
                self._log_health()
        finally:
            self.done.set()
            self._join_producer()
            with self.buffer_lock:
                remaining = self.buffer_bytes
            state = self.state if self.state in ("client-disconnect", "error") else "ended"
            metrics = self._metrics()
            metrics["state"] = state
            log(
                "stream.end",
                stream_id=self.stream_id,
                station=self.station_name,
                bytes_in=self.bytes_in,
                bytes_out=self.bytes_out,
                buffered_bytes=remaining,
                state=state,
            )
            self.registry.update_station(
                self.station_name,
                endedAt=int(time.time()),
                **metrics,
            )
            self.registry.unregister(self.stream_id)

    def _join_producer(self) -> None:
        thread = self.producer_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
