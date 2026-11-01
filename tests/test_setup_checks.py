from __future__ import annotations

from losesound.setup_checks import (
    build_setup_status,
    parse_version,
    probe_aftertouch,
    probe_radio_browser,
    probe_speaker,
    version_at_least,
)
from tests.helpers import FakeAfterTouch, FakeSpeaker


def test_parse_version():
    assert parse_version("0.138.1") == (0, 138, 1)
    assert parse_version("v0.99.0") == (0, 99, 0)
    assert parse_version("dev") is None
    assert parse_version("") is None
    assert version_at_least("0.138.1", (0, 99, 0)) is True
    assert version_at_least("0.77.0", (0, 99, 0)) is False
    assert version_at_least("dev", (0, 99, 0)) is None


def test_probe_speaker_reports_sources_and_marge():
    with FakeSpeaker() as speaker:
        speaker.state["marge_url"] = speaker.url + "/marge"
        status = probe_speaker("127.0.0.1", speaker.port, timeout=2)
        assert status["speakerReachable"] is True
        assert status["deviceId"] == "DEV1"
        assert status["sourcesReady"] is True
        assert status["margeReachable"] is True
        assert status["missingSources"] == []


def test_probe_speaker_detects_missing_sources_and_bad_marge():
    with FakeSpeaker() as speaker:
        speaker.state["sources"] = ["TUNEIN"]
        speaker.state["marge_url"] = speaker.url + "/marge"
        speaker.state["marge_status"] = 502
        status = probe_speaker("127.0.0.1", speaker.port, timeout=2)
        assert status["sourcesReady"] is False
        assert "LOCAL_INTERNET_RADIO" in status["missingSources"]
        assert status["margeReachable"] is False
        assert status["margeStatus"] == 502


def test_probe_speaker_unreachable():
    status = probe_speaker("127.0.0.1", 1, timeout=0.5)
    assert status["speakerReachable"] is False
    assert status["message"]


def test_probe_aftertouch_features():
    with FakeAfterTouch() as aftertouch:
        status = probe_aftertouch(aftertouch.url, timeout=2)
        assert status["reachable"] is True
        assert status["version"] == "0.138.1"
        assert status["versionSupported"] is True
        assert status["registryReachable"] is True
        assert status["orion"]["available"] is True
        assert status["radioBrowser"]["available"] is True


def test_probe_aftertouch_old_version_and_missing_orion():
    with FakeAfterTouch() as aftertouch:
        aftertouch.state["version"] = "0.77.0"
        aftertouch.state["orion_ok"] = False
        status = probe_aftertouch(aftertouch.url, timeout=2)
        assert status["versionSupported"] is False
        assert status["orion"]["available"] is False
        assert status["radioBrowser"]["available"] is True


def test_probe_radio_browser_unavailable():
    result = probe_radio_browser("http://127.0.0.1:1/soundtouch", timeout=0.5)
    assert result["available"] is False


def test_build_setup_status_findings():
    with FakeSpeaker() as speaker, FakeAfterTouch() as aftertouch:
        speaker.state["marge_url"] = speaker.url + "/marge"
        speaker.state["marge_status"] = 502
        aftertouch.state["orion_ok"] = False
        aftertouch.state["version"] = "0.77.0"
        status = build_setup_status(
            base_url="http://127.0.0.1:8092",
            aftertouch_url=aftertouch.url,
            speaker_ip="127.0.0.1",
            speaker_ips=["127.0.0.1"],
            speaker_port=speaker.port,
            aftertouch=probe_aftertouch(aftertouch.url, timeout=2),
        )
        codes = {finding["code"] for finding in status["findings"]}
        assert "marge_unreachable" in codes
        assert "aftertouch_old" in codes
        assert "orion_unavailable" in codes
        assert status["ready"] is True  # warnings do not block


def test_build_setup_status_error_when_speaker_unreachable():
    status = build_setup_status(
        base_url="http://127.0.0.1:8092",
        aftertouch_url="http://127.0.0.1:1",
        speaker_ip="127.0.0.1",
        speaker_ips=["127.0.0.1"],
        speaker_port=1,
        aftertouch={
            "reachable": False,
            "registryReachable": False,
            "orion": {"available": False},
            "radioBrowser": {"available": False},
        },
    )
    codes = {finding["code"] for finding in status["findings"]}
    assert "aftertouch_unreachable" in codes
    assert "speaker_unreachable" in codes
    assert status["ready"] is False
