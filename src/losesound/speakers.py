"""Minimal client for the local SoundTouch HTTP API (port 8090)."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_PORT = 8090
USER_AGENT = "LoseSound/1.0"


class SpeakerError(RuntimeError):
    """Raised when a speaker request fails."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(root: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for node in root.iter():
        if _local(node.tag) in wanted and node.text:
            return node.text.strip()
    return ""


def _children(root: ET.Element, name: str) -> list[ET.Element]:
    wanted = name.lower()
    return [node for node in root.iter() if _local(node.tag) == wanted]


class SpeakerClient:
    def __init__(self, ip: str, port: int = DEFAULT_PORT, timeout: float = 4.0) -> None:
        self.ip = (ip or "").strip()
        self.port = int(port)
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    # ----------------------------------------------------------- low level
    def _request(
        self,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        if not self.ip:
            raise SpeakerError("No speaker IP configured")
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, data=data)
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise SpeakerError(
                f"HTTP {exc.code} {exc.reason} from {self.ip}:{self.port}{path}", exc.code, body
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SpeakerError(f"Speaker {self.ip}:{self.port} unreachable: {reason}") from exc

    def _get_xml(self, path: str, timeout: float | None = None) -> ET.Element:
        _status, body = self._request(path, timeout=timeout)
        try:
            return ET.fromstring(body)
        except ET.ParseError as exc:
            raise SpeakerError(f"Invalid XML from speaker {self.ip}: {exc}") from exc

    def _post(
        self, path: str, payload: str, content_type: str = "application/xml", timeout: float | None = None
    ) -> str:
        headers = {"Content-Type": content_type, "Accept": "application/xml"}
        _status, body = self._request(path, payload.encode("utf-8"), headers, timeout)
        return body.decode("utf-8", errors="replace")

    # ------------------------------------------------------------- reading
    def info(self) -> dict:
        root = self._get_xml("/info")
        attrs = {key.lower(): value for key, value in root.attrib.items()}
        device_id = attrs.get("deviceid") or attrs.get("device_id") or _text(root, "deviceID", "deviceId")
        return {
            "speakerIp": self.ip,
            "deviceId": device_id,
            "name": _text(root, "name") or attrs.get("name", ""),
            "type": _text(root, "type") or attrs.get("type", ""),
            "margeUrl": _text(root, "margeURL", "margeUrl"),
            "account": _text(root, "margeAccountUUID", "margeAccountUuid") or attrs.get("margeaccountuuid", ""),
            "firmware": _text(root, "softwareVersion"),
            "moduleType": _text(root, "moduleType"),
        }

    def sources(self) -> list[dict]:
        root = self._get_xml("/sources")
        items = []
        for node in _children(root, "sourceItem"):
            attrs = {key.lower(): value for key, value in node.attrib.items()}
            items.append(
                {
                    "source": attrs.get("source", ""),
                    "sourceAccount": attrs.get("sourceaccount", ""),
                    "status": attrs.get("status", ""),
                    "isLocal": attrs.get("islocal", "").lower() == "true",
                    "multiroom": attrs.get("multiroomallowed", "").lower() == "true",
                    "name": (node.text or "").strip(),
                }
            )
        return items

    def presets(self) -> dict[int, dict]:
        root = self._get_xml("/presets")
        presets: dict[int, dict] = {}
        for node in _children(root, "preset"):
            attrs = {key.lower(): value for key, value in node.attrib.items()}
            try:
                slot = int(attrs.get("id", ""))
            except ValueError:
                continue
            if not 1 <= slot <= 6:
                continue
            content = None
            for child in node.iter():
                if _local(child.tag) == "contentitem":
                    content = child
                    break
            if content is None:
                presets[slot] = {"source": "", "type": "", "location": "", "itemName": ""}
                continue
            cattrs = {key.lower(): value for key, value in content.attrib.items()}
            presets[slot] = {
                "source": cattrs.get("source", ""),
                "type": cattrs.get("type", ""),
                "location": cattrs.get("location", ""),
                "itemName": _text(content, "itemName"),
                "isPresetable": cattrs.get("ispresetable", "").lower() == "true",
            }
        return presets

    def now_playing(self) -> dict:
        root = self._get_xml("/now_playing", timeout=3.0)
        if _local(root.tag) == "errors":
            error = next((node for node in root.iter() if _local(node.tag) == "error"), None)
            return {
                "source": "",
                "sourceAccount": "",
                "location": "",
                "type": "",
                "itemName": "",
                "track": "",
                "artist": "",
                "album": "",
                "stationName": "",
                "playStatus": "",
                "streamType": "",
                "error": (error.text or "").strip() if error is not None else "speaker error",
                "errorName": error.attrib.get("name", "") if error is not None else "",
            }
        attrs = {key.lower(): value for key, value in root.attrib.items()}
        content = None
        for child in root.iter():
            if _local(child.tag) == "contentitem":
                content = child
                break
        cattrs = {key.lower(): value for key, value in content.attrib.items()} if content is not None else {}
        return {
            "source": attrs.get("source", "") or cattrs.get("source", ""),
            "sourceAccount": attrs.get("sourceaccount", ""),
            "location": cattrs.get("location", ""),
            "type": cattrs.get("type", ""),
            "itemName": _text(content, "itemName") if content is not None else "",
            "track": _text(root, "track"),
            "artist": _text(root, "artist"),
            "album": _text(root, "album"),
            "stationName": _text(root, "stationName"),
            "playStatus": _text(root, "playStatus"),
            "streamType": _text(root, "streamType"),
        }

    # ------------------------------------------------------------- writing
    def store_preset(self, slot: int, preset_xml: str) -> str:
        return self._post("/storePreset", preset_xml, timeout=5.0)

    def remove_preset(self, slot: int) -> str:
        return self._post("/removePreset", f'<preset id="{int(slot)}" />')

    def select_content(self, content_item_xml: str) -> str:
        return self._post("/select", content_item_xml, timeout=5.0)

    def select_source(self, source: str, source_account: str = "") -> str:
        payload = f'<ContentItem source="{source}" sourceAccount="{source_account}" />'
        return self._post("/select", payload, timeout=5.0)

    def select_preset(self, slot: int) -> str:
        return self._post("/select", f'<preset id="{int(slot)}" />', timeout=5.0)

    def wake(self) -> str:
        """Send a POWER press/release so the speaker leaves standby."""
        self._post("/key", '<key state="press" sender="LoseSound">POWER</key>')
        return self._post("/key", '<key state="release" sender="LoseSound">POWER</key>')

    def play_pause(self, action: str = "PLAY") -> str:
        return self._post("/key", f'<key state="press" sender="LoseSound">{action}</key>')

    def volume(self, value: int) -> str:
        value = max(0, min(100, int(value)))
        return self._post("/volume", f"<volume>{value}</volume>")


def client_for(speaker_ip: str, speaker_port: int = DEFAULT_PORT, timeout: float = 4.0) -> SpeakerClient:
    return SpeakerClient(speaker_ip, speaker_port, timeout)


def normalize_ip(value: str) -> str:
    """Accept ``host``, ``host:port`` or a URL and return the bare host."""
    text = (value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlsplit(text).hostname or ""
    else:
        text = text.split("/", 1)[0]
        if text.count(":") == 1:
            text = text.split(":", 1)[0]
    return text.strip()
