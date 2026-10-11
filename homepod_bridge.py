"""HomePod Bridge: play Windows audio on a HomePod over AirPlay.

A taskbar (tray) icon with an AirPods-style flyout to connect/disconnect.
While connected, Windows output is switched to VB-Audio Virtual Cable (if
installed) so the laptop speakers stay silent, and the cable is captured and
streamed to the HomePod as ALAC. Windows volume keys then control the HomePod volume.
Without VB-Cable it falls back to mirroring the default speakers.
"""

import asyncio
import contextlib
import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from collections import deque
from pathlib import Path

import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageTk
import pyaudiowpatch as pyaudio
import soxr

import pyatv
from pyatv import exceptions
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import MediaMetadata
from pyatv.protocols import raop as raop_protocol
from pyatv.protocols.raop.protocols import StreamContext, TimingServer, airplayv2
from pyatv.protocols.raop.audio_source import AudioSource

import updater
from airpods import AirPodsEngine
from audio_route import (
    DEVICE_NAME,
    AudioRoute,
    default_output_id,
    ensure_cable_named,
    find_cable_output_id,
)

_LOGGER = logging.getLogger("homepod_bridge")

APP_NAME = "HomePod Bridge"
APP_VERSION = "1.1.0"
UPDATE_REPO = "ahmadashwah/HomePod-Bridge"  # GitHub releases carry the exe

# MessageBoxW flags / results
MB_YESNO, MB_ICONERROR, MB_ICONQUESTION, MB_ICONINFO = 0x4, 0x10, 0x20, 0x40
MB_TOPMOST, IDYES = 0x40000, 6

SAMPLE_RATE = 44100
CHANNELS = 2
PREFILL_SEC = 0.06  # audio held in reserve so capture jitter never starves AirPlay
TRIM_ABOVE_SEC = 0.12  # beyond this, skip ahead to PREFILL_SEC (keeps delay low)
MAX_BUFFER_SEC = 0.4  # hard cap while nothing is reading (e.g. during connect)
VIRTUAL_CABLE_HINTS = ("cable output", "vb-audio")


class LiveSource(AudioSource):
    """AudioSource fed by a capture thread; never ends, pads with silence."""

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._frame_size = CHANNELS * 2  # 16-bit samples
        self._max_bytes = int(SAMPLE_RATE * MAX_BUFFER_SEC) * self._frame_size
        self._prefill_bytes = int(SAMPLE_RATE * PREFILL_SEC) * self._frame_size
        self._trim_bytes = int(SAMPLE_RATE * TRIM_ABOVE_SEC) * self._frame_size
        self._priming = True

    def push(self, pcm: bytes) -> None:
        with self._lock:
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._size > self._max_bytes and self._chunks:
                self._size -= len(self._chunks.popleft())

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._priming = True

    async def readframes(self, nframes: int) -> bytes:
        wanted = nframes * self._frame_size
        out = bytearray()
        with self._lock:
            # Send whole packets of silence until the reserve is built up, and
            # rebuild it after running dry, rather than splicing gaps into audio.
            if self._priming:
                if self._size < self._prefill_bytes:
                    return b"\x00" * wanted
                self._priming = False
            elif self._size < wanted:
                self._priming = True
                return b"\x00" * wanted
            # Audio that piled up (while connecting, or from clock drift) is
            # pure delay; skip ahead so the reserve stays small.
            if self._size > self._trim_bytes:
                excess = self._size - self._prefill_bytes
                excess -= excess % self._frame_size
                while excess > 0 and self._chunks:
                    chunk = self._chunks.popleft()
                    if len(chunk) > excess:
                        self._chunks.appendleft(chunk[excess:])
                        self._size -= excess
                        break
                    excess -= len(chunk)
                    self._size -= len(chunk)
            while self._chunks and len(out) < wanted:
                chunk = self._chunks.popleft()
                take = wanted - len(out)
                out += chunk[:take]
                if len(chunk) > take:
                    self._chunks.appendleft(chunk[take:])
            self._size -= len(out)
        if len(out) < wanted:
            out += b"\x00" * (wanted - len(out))
        return bytes(out)

    async def get_metadata(self) -> MediaMetadata:
        return MediaMetadata(title="Windows audio", artist="HomePod Bridge")

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def sample_size(self) -> int:
        return 2

    @property
    def duration(self) -> int:
        return 0


# pyatv's stream_file only knows files/URLs/byte streams; hand our live source
# straight through instead of decoding it.
_original_open_source = raop_protocol.open_source


async def _open_source(source, sample_rate, channels, sample_size):
    if isinstance(source, LiveSource):
        if (sample_rate, channels, sample_size) != (SAMPLE_RATE, CHANNELS, 2):
            raise RuntimeError(
                f"HomePod wants {sample_rate} Hz/{channels} ch/{sample_size * 8}-bit,"
                " which this bridge does not support"
            )
        return source
    return await _original_open_source(source, sample_rate, channels, sample_size)


raop_protocol.open_source = _open_source


# HomePods garble pyatv's raw-PCM mode, so announce ALAC (what iOS/macOS send)
# and wrap each packet as an *uncompressed* ALAC frame. That needs no encoder
# library: a small header, the samples bit-packed big-endian, and an end tag.
_ALAC_FORMAT = 0x40000  # ALAC, 44100 Hz, 16-bit, stereo
_ALAC_CT = 2  # compression type: ALAC

_ALAC_HEADER_BITS = np.array(
    [0, 0, 1]  # element type: channel pair (stereo)
    + [0] * 4  # element instance tag
    + [0] * 12  # unused
    + [1]  # has explicit frame size
    + [0, 0]  # wasted bytes
    + [1],  # not compressed
    dtype=np.uint8,
)
_ALAC_END_BITS = np.array([1, 1, 1], dtype=np.uint8)  # end-of-frame tag


def pcm_to_alac(pcm: bytes) -> bytes:
    """Wrap little-endian 16-bit stereo PCM in an uncompressed ALAC frame."""
    samples = np.frombuffer(pcm, dtype="<i2")
    nframes = len(samples) // CHANNELS
    size_bits = np.unpackbits(np.array([nframes], dtype=">u4").view(np.uint8))
    sample_bits = np.unpackbits(samples.astype(">i2").view(np.uint8))
    bits = np.concatenate([_ALAC_HEADER_BITS, size_bits, sample_bits, _ALAC_END_BITS])
    return np.packbits(bits).tobytes()  # packbits zero-pads to a whole byte


_original_setup_audio_stream = airplayv2.AirPlayV2.setup_audio_stream
_original_send_audio_packet = airplayv2.AirPlayV2.send_audio_packet


async def _setup_audio_stream(self, control_client_port):
    rtsp_setup = self.rtsp.setup

    async def setup_with_alac(*args, body=None, **kwargs):
        for stream in (body or {}).get("streams", []):
            stream.update(audioFormat=_ALAC_FORMAT, ct=_ALAC_CT)
        return await rtsp_setup(*args, body=body, **kwargs)

    self.rtsp.setup = setup_with_alac
    try:
        await _original_setup_audio_stream(self, control_client_port)
    finally:
        del self.rtsp.setup


async def _send_audio_packet(self, transport, rtp_header, audio):
    return await _original_send_audio_packet(
        self, transport, rtp_header, pcm_to_alac(audio)
    )


airplayv2.AirPlayV2.setup_audio_stream = _setup_audio_stream
airplayv2.AirPlayV2.send_audio_packet = _send_audio_packet


# pyatv tells the speaker to play 1.5 s behind real time. Measured on a HomePod
# mini with the laptop mic: 0.5 s gives 0.51 s end to end with no gaps; lower
# settings get erratic. So use the floor.
LATENCY_SEC = 0.5
_original_context_reset = StreamContext.reset


def _context_reset(self) -> None:
    _original_context_reset(self)
    self.latency = int(LATENCY_SEC * self.sample_rate)


StreamContext.reset = _context_reset


# Takeover detection. While a speaker plays our stream it asks us for the time
# every ~1.5-2 s. When another device (an iPhone, say) takes the speaker over,
# those requests stop -- but nothing else tells us (measured on a HomePod mini:
# the old stream just goes unheard). Each Bridge runs its own event loop, so
# requests are tracked per loop.
TAKEOVER_SILENCE_SEC = 6
_last_timing_request: dict[int, float] = {}
_original_timing_received = TimingServer.datagram_received


def _timing_received(self, data, addr) -> None:
    _last_timing_request[id(asyncio.get_running_loop())] = time.monotonic()
    _original_timing_received(self, data, addr)


TimingServer.datagram_received = _timing_received


def find_capture_device(pa: pyaudio.PyAudio):
    """Return (device info, description). Prefer a virtual cable."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    devices = list(pa.get_device_info_generator_by_host_api(host_api_index=wasapi["index"]))
    for dev in devices:
        if dev["maxInputChannels"] > 0 and any(
            hint in dev["name"].lower() for hint in VIRTUAL_CABLE_HINTS
        ):
            return dev, f"virtual cable ({dev['name']})"
    default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    for dev in devices:
        if dev.get("isLoopbackDevice") and dev["name"].startswith(default_out["name"]):
            return dev, f"loopback of '{default_out['name']}'"
    raise RuntimeError("no capture device found")


def capture_loop(source: LiveSource, stop: threading.Event) -> None:
    pa = pyaudio.PyAudio()
    try:
        while not stop.is_set():
            try:
                dev, desc = find_capture_device(pa)
                rate = int(dev["defaultSampleRate"])
                dev_channels = min(dev["maxInputChannels"], 2)
                _LOGGER.info("Capturing from %s at %d Hz", desc, rate)
                resampler = (
                    soxr.ResampleStream(rate, SAMPLE_RATE, CHANNELS, dtype="float32")
                    if rate != SAMPLE_RATE else None
                )

                def callback(in_data, frame_count, time_info, status):
                    pcm = np.frombuffer(in_data, dtype="<f4").reshape(-1, dev_channels)
                    if dev_channels == 1:
                        pcm = np.repeat(pcm, 2, axis=1)
                    if resampler is not None:
                        pcm = resampler.resample_chunk(pcm)
                    if len(pcm):
                        pcm = np.clip(pcm, -1.0, 1.0) * 32767
                        # Native little-endian; pcm_to_alac does the
                        # big-endian packing ALAC requires.
                        source.push(pcm.astype("<i2").tobytes())
                    return (None, pyaudio.paContinue)

                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=dev_channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=rate // 100,  # 10 ms
                    stream_callback=callback,
                )
                # Re-check the default device every few seconds so switching
                # speakers in Windows is picked up.
                while not stop.is_set() and stream.is_active():
                    if stop.wait(3):
                        break
                    if find_capture_device(pa)[0]["index"] != dev["index"]:
                        _LOGGER.info("Default audio device changed")
                        break
                stream.stop_stream()
                stream.close()
            except Exception as ex:
                _LOGGER.warning("Capture error (%s), retrying", ex)
                stop.wait(2)
    finally:
        pa.terminate()


def unsupported_reason(conf) -> str | None:
    """Why this speaker can't be used, or None if it can.

    Speakers say in their announcement whether they need pairing: a HomePod
    allowing "Everyone"/"Same network" says NotNeeded; a Mac whose AirPlay
    Receiver is set to "Current User" says Unsupported (it wants an Apple ID).
    """
    raop = conf.get_service(Protocol.RAOP)
    if raop is None:
        return "Not an AirPlay speaker"
    if raop.requires_password:
        return "Needs a password - not supported"
    if raop.pairing in (PairingRequirement.NotNeeded, PairingRequirement.Optional):
        return None
    if str(conf.device_info.model_str).startswith("Mac"):
        return "On the Mac, set AirPlay Receiver to\n“Anyone on the same network”"
    return "Needs Apple ID sign-in - not supported"


def speaker_model(conf) -> str:
    """Readable model name; Macs report an internal id like "Mac16,12"."""
    model = conf.device_info.model_str or ""
    if model.startswith("Mac") and "," in model:
        return "Mac"
    return model or "AirPlay speaker"


class Bridge:
    """Streams to one AirPlay speaker at a time. Methods are called from the GUI."""

    def __init__(self, on_status) -> None:
        self.on_status = on_status  # called from worker threads
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.source = LiveSource()
        self.route = AudioRoute()
        self.volume = 35.0
        self.atv = None
        self._stream_future = None
        self._capture_stop: threading.Event | None = None

    @property
    def connected(self) -> bool:
        return self._stream_future is not None

    def scan(self, timeout: int = 5):
        """Blocking: return AirPlay speakers, HomePods first."""
        async def _scan():
            return await pyatv.scan(self.loop, timeout=timeout, protocol=Protocol.RAOP)

        confs = asyncio.run_coroutine_threadsafe(_scan(), self.loop).result()
        return sorted(
            confs,
            key=lambda c: (
                unsupported_reason(c) is not None,
                "homepod" not in str(c.device_info.model).lower(),
                c.name,
            ),
        )

    def connect(self, conf) -> bool:
        """Start streaming; returns True if Windows output was moved to VB-Cable."""
        routed = False
        try:
            routed = self.route.route_to_cable()
            if routed:
                # Windows' volume on the "HomePod" device is the HomePod volume.
                level, muted = self.route.read_volume()
                self.volume = 0 if muted else round(level * 100)
        except Exception as ex:
            _LOGGER.warning("Could not switch Windows output (%s)", ex)
        self.source.clear()
        self._capture_stop = threading.Event()
        threading.Thread(
            target=capture_loop, args=(self.source, self._capture_stop), daemon=True
        ).start()
        self._stream_future = asyncio.run_coroutine_threadsafe(
            self._stream(conf), self.loop
        )
        return routed

    CONNECT_ATTEMPTS = 3  # tries before giving up on a speaker
    CONNECT_TIMEOUT = 5  # s; Windows' own TCP timeout is ~21 s (sleeping Mac)

    async def _stream(self, conf) -> None:
        """Play until cancelled or the speaker goes away.

        Ends with a final status of "Refused" (wants sign-in), "Ended" (the
        speaker stopped us, e.g. another device took over or a Mac's AirPlay
        Receiver was switched off) or "Unreachable" (couldn't connect). Once
        playing it never reconnects by itself: that would steal the speaker
        back from whoever took it over.
        """
        failures = 0
        while True:
            playing = False
            try:
                self.on_status(f"Connecting to {conf.name}...")
                # pyatv's connect() doesn't touch the network for AirPlay
                # speakers (that happens once audio starts, with Windows' ~21 s
                # timeout), so check the speaker answers first.
                await self._check_reachable(conf)
                self.atv = await asyncio.wait_for(
                    pyatv.connect(conf, self.loop), self.CONNECT_TIMEOUT
                )
                await self.atv.audio.set_volume(self.volume)
                self.source.clear()
                self.on_status(f"Playing on {conf.name}")
                playing = True
                if await self._play_until_taken_over(conf):
                    _LOGGER.info("%s was taken over by another device", conf.name)
                else:
                    _LOGGER.info("%s ended the stream", conf.name)
                self.on_status("Ended")
                return
            except asyncio.CancelledError:
                raise
            except exceptions.AuthenticationError as ex:
                # The speaker wants an Apple ID / pairing (e.g. a Mac set to
                # "Current User"); retrying can't fix that.
                _LOGGER.warning("%s refused the connection: %s", conf.name, ex)
                self.on_status("Refused")
                return
            except Exception as ex:
                _LOGGER.warning("Stream error (%s): %s", "playing" if playing
                                else "connecting", ex)
                if playing:
                    self.on_status("Ended")
                    return
                failures += 1
                if failures >= self.CONNECT_ATTEMPTS:
                    self.on_status("Unreachable")
                    return
                self.on_status(f"Lost {conf.name}, retrying...")
            finally:
                if self.atv:
                    pending = self.atv.close()
                    self.atv = None
                    await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(3)

    async def _check_reachable(self, conf) -> None:
        service = conf.get_service(Protocol.RAOP)
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(str(conf.address), service.port),
            self.CONNECT_TIMEOUT,
        )
        writer.close()

    async def _play_until_taken_over(self, conf) -> bool:
        """Stream until the speaker ends it (False) or stops asking us for the
        time, meaning another device took it over (True)."""
        loop_id = id(self.loop)
        _last_timing_request.pop(loop_id, None)
        stream = asyncio.ensure_future(self.atv.stream.stream_file(self.source))
        try:
            while not stream.done():
                await asyncio.wait([stream], timeout=1)
                last = _last_timing_request.get(loop_id)
                # Only judge speakers that do ask (some may sync differently).
                if last is not None and time.monotonic() - last > TAKEOVER_SILENCE_SEC:
                    return True
            stream.result()  # re-raise anything the stream failed with
            return False
        finally:
            if not stream.done():
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stream

    def disconnect(self, wait: bool = False, switch_back: bool = True) -> None:
        """switch_back=False keeps Windows' current output (the user chose it)."""
        if self._stream_future:
            self._stream_future.cancel()
            self._stream_future = None
        if self._capture_stop:
            self._capture_stop.set()
            self._capture_stop = None
        try:
            self.route.restore(switch_back)
        except Exception as ex:
            _LOGGER.warning("Could not restore Windows output (%s)", ex)
        # Give the speaker session a moment to close cleanly when quitting.
        deadline = time.monotonic() + 2
        while wait and self.atv is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.on_status("Disconnected")

    def set_volume(self, volume: float) -> None:
        self.volume = volume
        atv = self.atv
        if atv:
            asyncio.run_coroutine_threadsafe(atv.audio.set_volume(volume), self.loop)


SETTINGS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "."), "HomePodBridge")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "HomePodBridge"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):  # the PyInstaller exe
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Per-user startup entry (the same list as Task Manager > Startup apps)."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_VALUE)
            except FileNotFoundError:
                pass


def migrate_from_airpodstray(settings: dict) -> None:
    """AirPodsTray now lives in this app. Ask a running copy to quit (its --quit
    event), and the first time, take over its Start-with-Windows entry and its
    auto-connect setting."""
    event = ctypes.windll.kernel32.OpenEventW(0x0002, False, "Local\\AirPodsTray-quit")
    if event:  # only exists while AirPodsTray runs
        ctypes.windll.kernel32.SetEvent(event)
        ctypes.windll.kernel32.CloseHandle(event)
        _LOGGER.info("asked the old AirPodsTray to quit")
    if settings.get("airpods_migrated"):
        return
    try:
        old = json.loads(Path(os.environ["APPDATA"], "AirPodsTray", "settings.json").read_text())
        settings["airpods_auto_connect"] = bool(old.get("auto_connect", False))
    except (OSError, ValueError):
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            winreg.QueryValueEx(key, "AirPodsTray")
            winreg.DeleteValue(key, "AirPodsTray")
            if not autostart_enabled():
                set_autostart(True)
            _LOGGER.info("moved AirPodsTray's Start with Windows to this app")
    except OSError:
        pass  # AirPodsTray didn't start with Windows
    settings["airpods_migrated"] = True
    save_settings(settings)


def resource_path(name: str) -> str:
    """Path to a bundled file, both from source and inside the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


BG = "#202020"
FG = "#ffffff"
SUBTLE = "#9a9a9a"
ACCENT = "#0a84ff"
ACCENT_HOVER = "#3395ff"
NEUTRAL = "#3a3a3a"
NEUTRAL_HOVER = "#474747"
GOOD = "#30d158"
DISABLED_BG = "#2b2b2b"
DISABLED_FG = "#6b6b6b"
ROW_HOVER = "#2d2d2d"
DIVIDER = "#333333"
# Speaker icon from Windows 11's own icon font.
ICON_FONT = "Segoe Fluent Icons"
AIRPODS_ROW = "\0airpods"  # key of the AirPods row (can't clash with a speaker name)
LID_FLYOUT_SECONDS = 20  # a flyout the AirPods case opened closes by itself after this
SPEAKER_GLYPH = "\ue7f5"
REFRESH_GLYPH = "\ue72c"
FONT = "Segoe UI"


def device_kind(conf) -> str:
    """Which icon a speaker gets, from the model it announces."""
    model = f"{conf.device_info.model} {conf.device_info.model_str}".lower()
    if "homepod" in model:
        return "homepod_mini" if "mini" in model else "homepod"
    if "appletv" in model.replace(" ", "") or "tv" in model.split():
        return "tv"
    if (conf.device_info.model_str or "").startswith("Mac"):
        # Macs only give an id like "Mac16,12"; the name usually says which.
        return "laptop" if "macbook" in conf.name.lower() else "desktop"
    return "speaker"


def airpods_kind(model: str) -> str:
    """Icon for an AirPods model name ("AirPods Pro 3", "AirPods Max"...)."""
    model = model.lower()
    if "max" in model or "solo" in model:
        return "headphones"
    return "airpods_pro" if "pro" in model else "airpods"


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def device_icon(kind: str, color: str, size: int) -> Image.Image:
    """Mac-AirPlay-menu style device icon, drawn at 4x and scaled down so the
    edges stay smooth."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (*_hex_rgb(color), 255)
    cut = (*_hex_rgb(BG), 255)  # "engraved" details in the background colour
    line = max(4, s // 14)

    def u(*values):  # coordinates in 0..100 units
        return [round(v * s / 100) for v in values]

    if kind == "airpods_pro":
        for side in (0, 1):  # mirror image for the right bud
            flip = (lambda x: x) if side == 0 else (lambda x: 100 - x)

            def box(x0, y0, x1, y1):
                a, b = sorted((flip(x0), flip(x1)))
                return u(a, y0, b, y1)

            # short stem, angling outwards towards the bottom
            d.polygon([tuple(u(flip(x), y)) for x, y in ((17, 46), (31, 46), (25, 80), (13, 80))],
                      fill=fill)
            d.ellipse(box(12, 74, 25, 87), fill=fill)  # rounded stem end
            d.ellipse(box(6, 20, 40, 54), fill=fill)  # big rounded head
            d.ellipse(box(25, 9, 45, 29), fill=cut)  # ear tip, set off by a thin gap
            d.ellipse(box(27, 11, 43, 27), fill=fill)
    elif kind == "airpods":  # classic AirPods: small heads, long stems
        for cx, tilt in ((34, -1), (66, 1)):
            d.ellipse(u(cx - 13, 14, cx + 13, 42), fill=fill)
            d.rounded_rectangle(u(cx - 5 + 4 * tilt, 32, cx + 5 + 4 * tilt, 86),
                                radius=s * 5 // 100, fill=fill)
            d.ellipse(u(cx - 5 - 3 * tilt, 22, cx + 3 - 3 * tilt, 30), fill=cut)  # speaker grille
    elif kind == "headphones":  # AirPods Max, Beats Solo
        d.arc(u(14, 8, 86, 80), 180, 360, fill=fill, width=s * 9 // 100)  # headband
        for x0 in (8, 70):
            d.rounded_rectangle(u(x0, 40, x0 + 22, 84), radius=s * 9 // 100, fill=fill)
    elif kind == "homepod_mini":
        d.ellipse(u(14, 18, 86, 90), fill=fill)  # sphere
        d.ellipse(u(30, 21, 70, 33), fill=cut)  # flat top
        d.ellipse(u(36, 24, 64, 30), fill=fill)
    elif kind == "homepod":
        d.rounded_rectangle(u(24, 6, 76, 94), radius=s * 24 // 100, fill=fill)
        d.ellipse(u(31, 10, 69, 22), fill=cut)  # top surface
        d.ellipse(u(37, 13, 63, 19), fill=fill)
    elif kind == "laptop":
        d.rounded_rectangle(u(17, 22, 83, 70), radius=s // 25, outline=fill, width=line)
        d.rounded_rectangle(u(6, 74, 94, 82), radius=s // 25, fill=fill)
    elif kind == "desktop":
        d.rounded_rectangle(u(10, 16, 90, 68), radius=s // 25, outline=fill, width=line)
        d.polygon(u(42, 68, 58, 68, 62, 84, 38, 84), fill=fill)  # stand
        d.rounded_rectangle(u(30, 82, 70, 88), radius=s // 40, fill=fill)
    elif kind == "tv":
        d.rounded_rectangle(u(8, 20, 92, 72), radius=s // 25, outline=fill, width=line)
        d.rounded_rectangle(u(32, 80, 68, 86), radius=s // 40, fill=fill)
    else:  # generic speaker
        d.rounded_rectangle(u(26, 8, 74, 92), radius=s // 10, outline=fill, width=line)
        d.ellipse(u(38, 18, 62, 42), outline=fill, width=line)  # tweeter
        d.ellipse(u(34, 50, 66, 82), outline=fill, width=line)  # woofer
    return img.resize((size, size), Image.LANCZOS)


def tray_image(kind: str | None) -> Image.Image:
    """The device the sound is going to (its icon, with a green "connected" dot),
    or the app's own HomePod silhouette when nothing is connected."""
    size = 64
    if kind is None:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((10, 4, 54, 60), radius=20, fill=(235, 235, 240, 255))
        draw.ellipse((18, 9, 46, 23), fill=(120, 120, 128, 255))
        return img
    img = device_icon(kind, "#ebebf0", size)
    draw = ImageDraw.Draw(img)
    draw.ellipse((40, 40, 63, 63), fill=(48, 209, 88, 255), outline=(28, 28, 30, 255), width=4)
    return img


def work_area() -> tuple[int, int, int, int]:
    """Screen area not covered by the taskbar (left, top, right, bottom)."""
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


class FlatButton(tk.Label):
    """A rounded-looking flat button made from a label (tk buttons can't be styled)."""

    def __init__(self, parent, command, **kwargs) -> None:
        super().__init__(parent, cursor="hand2", **kwargs)
        self.command = command
        self.colors = (ACCENT, ACCENT_HOVER)
        self.enabled = True
        self.bind("<Button-1>", lambda _e: self.enabled and self.command())
        self.bind("<Enter>", lambda _e: self.enabled and self.config(bg=self.colors[1]))
        self.bind("<Leave>", lambda _e: self.config(bg=self.colors[0]))

    def set_style(self, text: str, colors: tuple[str, str], enabled: bool = True) -> None:
        """enabled=False shades the button and makes it do nothing."""
        self.enabled = enabled
        self.colors = colors if enabled else (DISABLED_BG, DISABLED_BG)
        self.config(text=text, bg=self.colors[0], fg=FG if enabled else DISABLED_FG,
                    cursor="hand2" if enabled else "arrow")


class TrayApp:
    """Tray icon plus a Bluetooth-style speaker list above the taskbar."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.scale = self.root.winfo_fpixels("1i") / 96

        self.events: queue.Queue = queue.Queue()
        self.settings = load_settings()
        self.settings.pop("low_delay", None)  # option removed; always lowest delay
        self.bridge = Bridge(lambda text: self.events.put(("status", text)))
        self.bridge.volume = float(self.settings.get("volume", 35))
        self.devices: dict = {}
        self.selected: str | None = self.settings.get("device")
        self.scanning = False
        self.last_scan = 0.0
        self.playing = False
        self.visible = False
        self._hidden_at = 0.0
        self.connected_name: str | None = None
        self.status_text: str | None = None
        self.notice: str | None = None
        self.refused: dict[str, str] = {}  # speakers that rejected us this scan
        self._shown_rows: tuple | None = None
        self.rows: dict[str, dict] = {}
        self._updating = False
        self._icons: dict[tuple[str, str], ImageTk.PhotoImage] = {}
        self._shown_size: tuple[int, int] | None = None
        self._last_volume = None
        self._cable_id: str | None = None
        self._was_on_cable = False
        self._pending_connect = False
        self._shown_by_lid = False
        self._shown_at = 0.0

        migrate_from_airpodstray(self.settings)
        self.airpods = AirPodsEngine(
            lambda *event: self.events.put(("airpods", *event)),
            auto_connect=self.settings.get("airpods_auto_connect", False),
        )

        # VB-Cable shows up in Windows' sound menus as "HomePod"; picking it
        # there is how the user connects (see watch_output).
        self.settings.pop("restore_output", None)  # from older versions
        save_settings(self.settings)
        self.sync_device_name()
        try:
            self._cable_id = find_cable_output_id()
        except Exception as ex:
            _LOGGER.warning("Could not find the HomePod output device (%s)", ex)

        self.build_flyout()
        self.icon = pystray.Icon(
            "HomePodBridge",
            tray_image(None),
            "HomePod Bridge",
            menu=pystray.Menu(
                # Hidden default item: what a left click on the icon does.
                pystray.MenuItem("Show", lambda: self.events.put(("toggle",)),
                                 default=True, visible=False),
                pystray.MenuItem(f"HomePod Bridge {APP_VERSION}", None, enabled=False),
                pystray.MenuItem(
                    "Check for updates", lambda: self.events.put(("update",)),
                ),
                pystray.MenuItem(
                    "Auto-connect AirPods (case opens or a bud goes in)",
                    lambda: self.events.put(("airpods_auto_connect",)),
                    checked=lambda _i: self.airpods.auto_connect,
                    visible=self.airpods.paired,
                ),
                pystray.MenuItem(
                    "Start with Windows",
                    lambda: self.events.put(("autostart",)),
                    checked=lambda _i: autostart_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self.events.put(("quit",))),
            ),
        )
        self.icon.run_detached()
        self.airpods.start()
        # Clicking into a flyout the case opened means you're using it: keep it open.
        self.root.bind_all("<Button-1>", lambda _e: setattr(self, "_shown_by_lid", False),
                           add="+")
        self.root.after(50, self.pump_events)
        self.scan()
        self.root.after(400, self.show)
        self.root.after(500, self.watch_output)
        if os.environ.get("HB_UPDATE_SELFTEST") == "1":  # end-to-end updater test
            self.root.after(3000, lambda: self.check_for_updates(unattended=True))

    # ----- flyout -----------------------------------------------------------

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def build_flyout(self) -> None:
        card = tk.Frame(self.root, bg=BG, padx=self.px(18), pady=self.px(16))
        card.pack(fill="both", expand=True)
        card.columnconfigure(0, weight=1, minsize=self.px(330))

        header = tk.Frame(card, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(header, text="Sound", bg=BG, fg=FG,
                 font=(FONT, 14, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        # Search again: a refresh icon, left of the close button.
        self.refresh_icon = tk.Label(header, text=REFRESH_GLYPH, bg=BG,
                                     font=(ICON_FONT, 13), padx=self.px(6))
        self.refresh_icon.grid(row=0, column=2)
        self.refresh_icon.bind("<Button-1>", lambda _e: self.scan())
        self.refresh_icon.bind(
            "<Enter>", lambda _e: None if self.scanning
            else self.refresh_icon.config(fg=FG)
        )
        self.refresh_icon.bind(
            "<Leave>", lambda _e: None if self.scanning
            else self.refresh_icon.config(fg=SUBTLE)
        )
        close = tk.Label(header, text="✕", bg=BG, fg=SUBTLE, font=(FONT, 12),
                         cursor="hand2", padx=self.px(6))
        close.grid(row=0, column=3)
        close.bind("<Button-1>", lambda _e: self.hide())

        # The line under the header; while searching it becomes moving dots.
        self.divider = tk.Canvas(card, height=self.px(4), bg=BG, bd=0,
                                 highlightthickness=0)
        self.divider.grid(row=2, column=0, columnspan=2, sticky="ew", pady=self.px(9))
        self.divider.bind("<Configure>", lambda _e: self.draw_divider())
        self._dots_start = 0.0
        self._dots_job = None

        self.list_frame = tk.Frame(card, bg=BG)
        self.list_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.list_frame.columnconfigure(0, weight=1)

        self.hint = tk.Label(card, bg=BG, fg=SUBTLE, font=(FONT, 10), anchor="w",
                             justify="left", wraplength=self.px(330))
        self.hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0))

        self.root.bind("<FocusOut>", lambda _e: self.root.after(150, self.check_focus))
        self.root.bind("<Escape>", lambda _e: self.hide())
        self.refresh_ui()

    def refresh_ui(self) -> None:
        if self.scanning:
            self.refresh_icon.config(fg=NEUTRAL_HOVER, cursor="")
            if self._dots_job is None:
                self._dots_start = time.monotonic()
                self.animate_dots()
        else:
            self.refresh_icon.config(fg=SUBTLE, cursor="hand2")
            if self._dots_job is not None:
                self.root.after_cancel(self._dots_job)
                self._dots_job = None
            self.draw_divider()

        # Rows are created once per speaker and then only updated: destroying and
        # recreating them left see-through holes in the flyout for a frame.
        # One list: the AirPods (when paired with this PC) first, then AirPlay speakers.
        names = ((AIRPODS_ROW,) if self.airpods.paired else ()) + tuple(self.devices)
        if names != self._shown_rows:
            self._shown_rows = names
            for child in self.list_frame.winfo_children():
                child.destroy()
            self.rows = {
                name: self.build_device_row(
                    row, self.toggle_airpods if name == AIRPODS_ROW
                    else lambda n=name: self.toggle_device(n)
                )
                for row, name in enumerate(names)
            }
        if self.airpods.paired:
            self.update_airpods_row(self.rows[AIRPODS_ROW])
        for name, conf in self.devices.items():
            self.update_device_row(self.rows[name], conf)
        # An emptied Tk frame keeps its old height, so hide it instead.
        if names:
            self.list_frame.grid()
        else:
            self.list_frame.grid_remove()

        if self.notice:
            self.hint.config(text=self.notice)
        elif not names and not self.scanning:
            self.hint.config(text="No speakers found. Make sure this PC is on the "
                                  "same Wi-Fi as your HomePod, then search again.")
        else:
            self.hint.config(text="")
        if self.hint.cget("text"):
            self.hint.grid()
        else:
            self.hint.grid_remove()

        self.icon_state(self.bridge.connected and self.playing)
        if self.visible:
            self.place_flyout()  # height changed; keep it sitting on the taskbar

    # Windows' "searching" dots (the Bluetooth "Add a device" screen): five
    # dots enter one after another, ease into a cluster in the middle, then
    # speed off to the right; a short pause and it repeats.
    DOTS = 5
    DOT_STAGGER_SEC = 0.17  # delay between consecutive dots
    DOT_TRAVEL_SEC = 2.4  # time for one dot to cross
    DOTS_CYCLE_SEC = 3.5  # whole pattern, including the pause
    DOT_MID_SPEED = 0.3  # speed through the middle, relative to an even pace

    def draw_divider(self) -> None:
        """A thin line, or (while searching) the moving dots."""
        canvas = self.divider
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.scanning:
            middle = height // 2
            canvas.create_rectangle(0, middle, width, middle + 1, fill=DIVIDER, width=0)
            return
        t = (time.monotonic() - self._dots_start) % self.DOTS_CYCLE_SEC
        size = height
        for i in range(self.DOTS):
            # The first dot leads; each later one starts a little after it.
            u = (t - i * self.DOT_STAGGER_SEC) / self.DOT_TRAVEL_SEC
            if not 0 <= u <= 1:
                continue
            # Fast at the edges, a slow crawl (not a stop) through the middle,
            # so the dots bunch up but stay apart: 0.5 + a*v + b*v^3 with
            # v = u - 0.5, middle speed a, and b chosen so it ends at 1.
            v = u - 0.5
            progress = 0.5 + self.DOT_MID_SPEED * v + (4 - 4 * self.DOT_MID_SPEED) * v ** 3
            x = progress * (width + size) - size
            canvas.create_oval(x, 0, x + size, size, fill=ACCENT, width=0)

    def animate_dots(self) -> None:
        self.draw_divider()
        self._dots_job = self.root.after(16, self.animate_dots)  # ~60 fps

    def build_device_row(self, row: int, on_button) -> dict:
        frame = tk.Frame(self.list_frame, bg=BG, padx=self.px(8), pady=self.px(8))
        frame.grid(row=row, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        icon = tk.Label(frame, bg=BG)
        icon.grid(row=0, column=0, rowspan=2, padx=(0, self.px(12)))
        title = tk.Label(frame, bg=BG, font=(FONT, 11, "bold"), anchor="w")
        title.grid(row=0, column=1, sticky="w")
        state = tk.Label(frame, bg=BG, font=(FONT, 9), anchor="w", justify="left")
        state.grid(row=1, column=1, sticky="w")
        # Fixed width so "Connect" <-> "Disconnect" doesn't shift the row.
        button = FlatButton(frame, on_button, fg=FG,
                            font=(FONT, 10, "bold"), width=10, pady=self.px(5))
        button.grid(row=0, column=2, rowspan=2, padx=(self.px(12), 0))
        return {"icon": icon, "title": title, "state": state, "button": button}

    def update_airpods_row(self, row: dict) -> None:
        pods = self.airpods
        battery = pods.battery_text()
        # Title: their own name ("Ahmad's AirPods Pro"); the line below says the model,
        # like the speaker rows ("Kitchen" / "HomePod Mini").
        model = pods.model_name() if pods.display_name() != pods.model_name() else ""

        def line(*parts: str) -> str:
            return "  ·  ".join(part for part in parts if part)

        if pods.connecting:
            state, colour, active = "Connecting...", ACCENT, True
        elif pods.disconnecting:
            state, colour, active = "Disconnecting...", SUBTLE, True
        elif pods.connected:
            state, colour, active = line("Connected", battery), GOOD, True
        elif pods.connect_failed:
            state, colour, active = line("Couldn't connect", battery), SUBTLE, False
        else:
            # Last known battery even when they've gone quiet (the shaded Connect
            # says they're out of range).
            hint = "" if battery or pods.in_range else "open the case nearby to connect"
            state = line(model, battery, hint) or "Nearby"
            state = state[0].upper() + state[1:]
            colour, active = SUBTLE, False
        row["icon"].config(image=self.icon_image(
            airpods_kind(pods.model_name()),
            GOOD if pods.connected else ACCENT if pods.connecting else SUBTLE))
        row["title"].config(text=pods.display_name(), fg=FG)
        row["state"].config(text=state, fg=colour)
        button = row["button"]
        if active:
            button.set_style("Disconnect", (NEUTRAL, NEUTRAL_HOVER))
        else:
            # Shaded while they're out of range: connecting couldn't work.
            button.set_style("Connect", (ACCENT, ACCENT_HOVER), enabled=pods.in_range)
        button.grid()

    def toggle_airpods(self) -> None:
        self._shown_by_lid = False  # the user is using the flyout; let it stay
        if self.airpods.connected or self.airpods.connecting:
            self.airpods.request_disconnect()
        else:
            self.airpods.request_connect()

    def update_device_row(self, row: dict, conf) -> None:
        is_active = self.bridge.connected and conf.name == self.connected_name
        blocked = None if is_active else self.blocked_reason(conf)
        if is_active:
            state = self.status_text or "Connecting..."
            dot = GOOD if self.playing else ACCENT
        elif blocked:
            state, dot = blocked, NEUTRAL_HOVER
        else:
            state, dot = speaker_model(conf), SUBTLE

        row["icon"].config(image=self.icon_image(device_kind(conf), dot))
        row["title"].config(text=conf.name, fg=SUBTLE if blocked else FG)
        row["state"].config(text=state,
                            fg=GOOD if is_active and self.playing else SUBTLE)
        button = row["button"]
        if blocked:
            button.grid_remove()  # no Connect button: it could only fail
            return
        if is_active:
            button.set_style("Disconnect", (NEUTRAL, NEUTRAL_HOVER))
        else:
            button.set_style("Connect", (ACCENT, ACCENT_HOVER))
        button.grid()

    def icon_image(self, kind: str, color: str) -> ImageTk.PhotoImage:
        """Cached Tk image (Tk shows nothing if the Python object is collected)."""
        key = (kind, color)
        if key not in self._icons:
            self._icons[key] = ImageTk.PhotoImage(device_icon(kind, color, self.px(30)))
        return self._icons[key]

    def blocked_reason(self, conf) -> str | None:
        return self.refused.get(conf.name) or unsupported_reason(conf)

    def place_flyout(self) -> None:
        """Anchor the flyout's bottom-right corner just above the taskbar."""
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.visible and (w, h) == self._shown_size:
            return  # nothing moved; re-placing an open window makes it flash
        self._shown_size = (w, h)
        _left, _top, right, bottom = work_area()
        margin = self.px(12)
        self.root.geometry(f"+{right - w - margin}+{bottom - h - margin}")

    def show(self, from_lid: bool = False) -> None:
        """from_lid: opened because the AirPods case opened. Then it doesn't take the
        keyboard focus from what you're doing, closes again with the lid, and goes
        away by itself after a while - unless you click into it."""
        if from_lid and self.visible and not self._shown_by_lid:
            return  # already open because you opened it; leave it be
        self._shown_by_lid = from_lid
        self._shown_at = time.monotonic()
        if not self.devices and not self.scanning:
            self.scan()
        elif not self.bridge.connected and time.monotonic() - self.last_scan > 60:
            self.scan()
        self.place_flyout()
        self.root.deiconify()
        self.root.lift()
        self.round_corners()
        if not from_lid:
            self.root.focus_force()
        self.visible = True
        if from_lid:
            self.root.after(LID_FLYOUT_SECONDS * 1000, self._hide_lid_flyout)

    def _hide_lid_flyout(self) -> None:
        if (self.visible and self._shown_by_lid
                and time.monotonic() - self._shown_at >= LID_FLYOUT_SECONDS - 0.5):
            self.hide()

    def hide(self) -> None:
        if self.visible:
            self.root.withdraw()
            self.visible = False
            self._shown_by_lid = False
            self._hidden_at = time.monotonic()

    def toggle_flyout(self) -> None:
        if self.visible:
            self.hide()
        elif time.monotonic() - self._hidden_at > 0.4:
            # (A click on the tray icon first hides us via focus loss;
            # don't immediately pop back up.)
            self.show()

    def check_focus(self) -> None:
        if self.visible and self.root.focus_get() is None:
            self.hide()

    def round_corners(self) -> None:
        """Windows 11 rounded corners for the borderless flyout."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
        except (AttributeError, OSError):
            pass

    def icon_state(self, active: bool) -> None:
        if not hasattr(self, "icon"):
            return  # first refresh happens before the tray icon is created
        # Icon = where the sound goes: the speaker we stream to, else connected AirPods.
        kind = None
        if active and self.connected_name in self.devices:
            kind = device_kind(self.devices[self.connected_name])
        elif self.airpods.paired and self.airpods.connected:
            kind = airpods_kind(self.airpods.model_name())
        if getattr(self, "_icon_kind", "unset") != kind:
            self._icon_kind = kind
            self.icon.icon = tray_image(kind)
        # Tooltip: what's playing where, and the AirPods battery (like AirPodsTray's).
        lines = [f"Playing on {self.connected_name}" if active else APP_NAME]
        if self.airpods.paired:
            battery = self.airpods.battery_text()
            pods = self.airpods.display_name()
            lines.append(f"{pods}: {battery}" if battery else
                         f"{pods}: connected" if self.airpods.connected else "")
        title = "\n".join(line for line in lines if line)[:127]
        if title != getattr(self, "_icon_title", None):
            self._icon_title = title
            self.icon.title = title

    # ----- actions ----------------------------------------------------------

    def pump_events(self) -> None:
        """Handle requests from the tray and bridge threads on the Tk thread."""
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.on_status(event[1])
                elif kind == "toggle":
                    self.toggle_flyout()
                elif kind == "devices":
                    self.on_devices(event[1])
                elif kind == "autostart":
                    set_autostart(not autostart_enabled())
                elif kind == "update":
                    self.check_for_updates()
                elif kind == "airpods":
                    self.on_airpods_event(*event[1:])
                elif kind == "airpods_auto_connect":
                    self.airpods.auto_connect = not self.airpods.auto_connect
                    self.settings["airpods_auto_connect"] = self.airpods.auto_connect
                    save_settings(self.settings)
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self.pump_events)

    def scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.notice = None
        self.refresh_ui()

        def work():
            try:
                confs = self.bridge.scan()
            except Exception as ex:
                _LOGGER.warning("Scan failed: %s", ex)
                confs = []
            self.events.put(("devices", confs))

        threading.Thread(target=work, daemon=True).start()

    def on_devices(self, confs) -> None:
        self.scanning = False
        self.last_scan = time.monotonic()
        found = {c.name: c for c in confs}
        # Keep the speaker we're playing on listed even if this scan missed it.
        if self.bridge.connected and self.connected_name in self.devices:
            found.setdefault(self.connected_name, self.devices[self.connected_name])
        self.devices = found
        self.refused.clear()  # settings may have changed; a fresh scan re-checks
        usable = [n for n, c in self.devices.items() if not self.blocked_reason(c)]
        if self.selected not in usable and usable:
            self.selected = usable[0]
            self.sync_device_name()
        if self._pending_connect:
            if self.devices:
                self.connect()
                return
            self._pending_connect = False
        self.refresh_ui()

    def on_status(self, text: str) -> None:
        if text == "Refused" and self.connected_name:
            # Stop, put the sound back on the laptop, and say why.
            name = self.connected_name
            conf = self.devices.get(name)
            mac = conf is not None and str(conf.device_info.model_str).startswith("Mac")
            self.refused[name] = (
                "On the Mac, set AirPlay Receiver to\n“Anyone on the same network”"
                if mac else "Refused the connection - needs sign-in"
            )
            self.disconnect()
            self.notice = f"{name} wouldn't accept this PC."
            usable = [n for n, c in self.devices.items() if not self.blocked_reason(c)]
            self.selected = usable[0] if usable else None
            self.settings["device"] = self.selected
            save_settings(self.settings)
            self.sync_device_name()
            self.refresh_ui()
            return
        if text in ("Ended", "Unreachable") and self.connected_name:
            # Don't fight for the speaker: stop and give the laptop its sound back.
            name = self.connected_name
            self.disconnect()
            self.notice = (
                f"{name} stopped playing from this PC - another device may have "
                "taken over." if text == "Ended"
                else f"Couldn't reach {name}. Is it on and on the same Wi-Fi?"
            )
            self.refresh_ui()
            return
        self.playing = text.startswith("Playing")
        if text == "Disconnected":
            self.status_text = None
        elif self.playing:
            self.status_text = "Playing from this PC"
        elif text.startswith("Connecting"):
            self.status_text = "Connecting..."
        else:  # "Lost ...", "... ended the stream"
            self.status_text = "Reconnecting..."
        self.refresh_ui()

    def toggle_device(self, name: str) -> None:
        """Connect/Disconnect button on a speaker row."""
        if self.bridge.connected and name == self.connected_name:
            self.disconnect()
            return
        if self.bridge.connected:
            # Hop straight to the other speaker; Windows stays on "HomePod".
            self.disconnect(switch_back=False)
        self.selected = name
        self.connect()

    def connect(self) -> None:
        conf = self.devices.get(self.selected or "")
        if conf is None:
            # Not found yet; on_devices connects once the scan finds it.
            self._pending_connect = True
            self.scan()
            return
        self._pending_connect = False
        if self.blocked_reason(conf):
            self.notice = f"{conf.name} can't be used from this PC."
            self.refresh_ui()
            return
        self.notice = None
        self.connected_name = conf.name
        self.status_text = "Connecting..."
        self.sync_device_name()
        routed = self.bridge.connect(conf)
        if routed:
            self.notice = None
            self._last_volume = self.bridge.volume
            self.poll_windows_volume()
        else:
            self.notice = ("VB-Cable isn't installed, so your laptop speakers "
                           "play too. Get it free at vb-audio.com/Cable.")
        self.settings["device"] = conf.name
        save_settings(self.settings)
        self.refresh_ui()

    def disconnect(self, switch_back: bool = True) -> None:
        self._pending_connect = False
        if self.bridge.connected:
            self.bridge.disconnect(switch_back=switch_back)
        self.playing = False
        self.status_text = None
        self.connected_name = None
        self.refresh_ui()

    def sync_device_name(self) -> None:
        """Name Windows' output device after the speaker it plays to, so the
        sound menu says "Kitchen" or "Living Room", not just "HomePod"."""
        try:
            ensure_cable_named(self.selected or DEVICE_NAME)
        except Exception as ex:
            _LOGGER.warning("Could not rename the output device (%s)", ex)

    def watch_output(self) -> None:
        """Follow Windows' output choice: picking "HomePod" in the sound menu
        connects, picking anything else disconnects."""
        try:
            if self._cable_id is None:
                self._cable_id = find_cable_output_id()  # VB-Cable just installed?
                if self._cable_id:
                    self.sync_device_name()
            on_cable = bool(self._cable_id) and default_output_id() == self._cable_id
        except Exception as ex:
            _LOGGER.warning("Checking the Windows output failed (%s)", ex)
            on_cable = self._was_on_cable
        # Act on changes only, so the app's own Connect/Disconnect (which
        # switch the output themselves) don't trigger anything here.
        if on_cable and not self._was_on_cable and not self.bridge.connected:
            self.connect()
        elif not on_cable and self._was_on_cable and self.bridge.connected:
            self.disconnect(switch_back=False)
        self._was_on_cable = on_cable
        self.root.after(500, self.watch_output)

    def poll_windows_volume(self) -> None:
        """Mirror the cable's Windows volume (volume keys) onto the HomePod."""
        if not (self.bridge.connected and self.bridge.route.routed):
            return
        try:
            level, muted = self.bridge.route.read_volume()
        except Exception as ex:
            _LOGGER.warning("Reading Windows volume failed (%s)", ex)
        else:
            # VB-Cable passes audio through at full level whatever Windows'
            # volume says (measured), so this only drives the HomePod volume.
            volume = 0 if muted else round(level * 100)
            if volume != self._last_volume:
                self._last_volume = volume
                self.bridge.set_volume(volume)
        self.root.after(200, self.poll_windows_volume)

    def on_airpods_event(self, kind: str, *args) -> None:
        if kind == "changed":
            self.refresh_ui()
        elif kind == "lid_opened":
            self.show(from_lid=True)
        elif kind == "lid_closed":
            if self.visible and self._shown_by_lid:
                self.hide()
        elif kind == "low_battery":
            self.icon.notify(args[0], "AirPods battery low")
        elif kind == "relaunch":
            self.relaunch("Bluetooth scanning keeps failing")

    def relaunch(self, reason: str) -> None:
        """Start a fresh copy and quit this one (it waits for us to exit)."""
        _LOGGER.info("%s - relaunching the app", reason)
        command = ([sys.executable] if getattr(sys, "frozen", False)
                   else [sys.executable, os.path.abspath(__file__)])
        # A one-file exe started from a running one must not reuse our unpack folder.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen(command + [updater.AFTER_UPDATE_FLAG], env=env, close_fds=True)
        self.quit()

    def check_for_updates(self, unattended: bool = False) -> None:
        """unattended=True (HB_UPDATE_SELFTEST only) installs without asking."""
        if self._updating:
            return
        self._updating = True

        def ask(text: str, flags: int) -> int:
            if unattended:
                _LOGGER.info("Update dialog (auto-answered): %s", text.splitlines()[0])
                return IDYES
            return ctypes.windll.user32.MessageBoxW(
                None, text, "HomePod Bridge", flags | MB_TOPMOST
            )

        def work():
            try:
                if not updater.can_self_update():
                    ask("Updates install into the HomePodBridge.exe app; this copy "
                        "is running from Python source.", MB_ICONINFO)
                    return
                release = updater.latest_release(UPDATE_REPO)
                if release is None or not updater.is_newer(release, APP_VERSION):
                    ask(f"You have the latest version ({APP_VERSION}).", MB_ICONINFO)
                    return
                notes = f"\n\nWhat's new:\n{release.notes[:600]}" if release.notes else ""
                answer = ask(
                    f"Version {release.version} is available (you have {APP_VERSION})."
                    f"{notes}\n\nInstall it now? The app restarts, which stops any "
                    "playback for a few seconds.",
                    MB_YESNO | MB_ICONQUESTION,
                )
                if answer != IDYES:
                    return
                updater.install(release)
                _LOGGER.info("Updated to %s; restarting", release.version)
                self.events.put(("quit",))
            except Exception as ex:
                _LOGGER.warning("Update failed: %s", ex)
                ask(f"Couldn't update: {ex}", MB_ICONERROR)
            finally:
                self._updating = False

        threading.Thread(target=work, daemon=True).start()

    def quit(self) -> None:
        self.airpods.stop()
        if self.bridge.connected:
            self.bridge.disconnect(wait=True)
        self.settings["volume"] = round(self.bridge.volume)
        save_settings(self.settings)
        self.icon.stop()
        self.root.destroy()


def main() -> None:
    log_dir = SETTINGS_DIR
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.FileHandler(os.path.join(log_dir, "log.txt"), "w", "utf-8")],
    )
    logging.getLogger("pyatv").setLevel(logging.WARNING)

    # One bridge at a time: a second copy would fight over the HomePod. Right
    # after an update the old copy is still shutting down, so wait for it.
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    deadline = time.monotonic() + (20 if updater.AFTER_UPDATE_FLAG in sys.argv else 0)
    while True:
        mutex = kernel32.CreateMutexW(None, False, "Local\\HomePodBridge")
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            break
        kernel32.CloseHandle(mutex)
        if time.monotonic() > deadline:
            ctypes.windll.user32.MessageBoxW(
                None, "HomePod Bridge is already running in the taskbar.",
                "HomePod Bridge", MB_ICONINFO,
            )
            return
        time.sleep(0.25)
    updater.clean_up_after_update()
    _LOGGER.info("HomePod Bridge %s starting", APP_VERSION)

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on HiDPI
    except (AttributeError, OSError):
        pass

    TrayApp().root.mainloop()


if __name__ == "__main__":
    main()
