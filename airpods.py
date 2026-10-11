"""AirPods support (moved from AirPodsTray): battery, lid and in-ear detection,
connect / disconnect, auto-pause and low-battery warnings.

Runs on its own thread with its own asyncio loop and tells the tray app about
changes through `on_event(kind, *args)` (called on that thread):

  "changed"               something shown in the flyout / tooltip changed
  "lid_opened"            the case was opened near the PC
  "lid_closed"            ...and closed again
  "low_battery", message  a bud or the case is low
  "relaunch"              Bluetooth scanning keeps failing; restart the app

What it always does (no settings): pause media when a bud comes out and resume
when it goes back in, pause when the AirPods disconnect, and disconnect when
both buds come out - each only while the AirPods are the PC's sound output, so
playback on a HomePod isn't touched. Auto-connect is optional (off by default).
"""

import asyncio
import logging
import threading
import time
import winreg

import comtypes
from bleak import BleakScanner
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

from airpods_scanner import APPLE_COMPANY_ID, MODELS, AirPodsState, decode
from audio_route import default_output_id, set_default_output
from auto_connect import A2DP_UUID, MMDEVICES_RENDER, PKEY_DEVICE_PARENT_ID, PairedAirPods

_LOGGER = logging.getLogger("airpods")

MIN_RSSI = -60        # how close the case must be for "lid opened" to count
LID_COOLDOWN = 15     # seconds to ignore further "lid opened" signals (each bud announces it)
LID_CLOSED_AFTER = 3  # no case broadcast for this long (buds in the case) = lid closed
EAR_DEBOUNCE = 0.1    # an in-ear change must hold this long before acting on it (each change
                      # arrives as one clean advert, so this only guards against stray ones)
EAR_RSSI = -85        # buds in your ears are weak (your head is in the way), so track them from far
LOW_BATTERY = 20
OTHER_PAIR_MARGIN = 8       # dB weaker than the strongest same-model AirPods = someone else's
PAIR_MEMORY = 15            # seconds an address counts as "recently seen" for that comparison
SCAN_SILENCE_RESTART = 30   # restart the Bluetooth scan if nothing at all is heard for this long
MAX_SCAN_FAILURES = 5       # failed scan restarts in a row before the app relaunches itself
HEARTBEAT_SECONDS = 600     # write an "alive" line to the log this often


def log(*parts) -> None:
    _LOGGER.info(" ".join(map(str, parts)))


# ---- media control ------------------------------------------------------------------

async def pause_media() -> list[str]:
    """Pause every session that is playing (not just Windows' "current" one, which may be
    a different app). Returns the ids of the apps that were paused."""
    paused = []
    for session in (await MediaManager.request_async()).get_sessions():
        if session.get_playback_info().playback_status == PlaybackStatus.PLAYING:
            if await session.try_pause_async():
                paused.append(session.source_app_user_model_id)
    log("paused:", paused or "nothing was playing")
    return paused


async def resume_media(app_ids: list[str]) -> None:
    """Resume these apps - only if they're still paused (not if the user stopped or switched)."""
    resumed = []
    for session in (await MediaManager.request_async()).get_sessions():
        if (session.source_app_user_model_id in app_ids
                and session.get_playback_info().playback_status == PlaybackStatus.PAUSED):
            if await session.try_play_async():
                resumed.append(session.source_app_user_model_id)
    log("resumed:", resumed or f"nothing ({app_ids} no longer paused)")


class PlayingTracker:
    """Remembers which apps were playing recently. AirPods send their own "pause" when a bud
    comes out, which can beat us to it - so "was playing a moment ago" decides what to resume."""

    def __init__(self):
        self.last_playing: dict[str, float] = {}

    async def run(self) -> None:
        manager = await MediaManager.request_async()
        statuses: dict[str, str] = {}
        while True:
            now = time.monotonic()
            for session in manager.get_sessions():
                app, status = session.source_app_user_model_id, session.get_playback_info().playback_status
                if status == PlaybackStatus.PLAYING:
                    self.last_playing[app] = now
                name = PlaybackStatus(status).name
                if statuses.get(app) != name:  # log every play/pause, whoever caused it
                    if app in statuses:
                        log(f"  media: {app} {statuses[app]} -> {name}")
                    statuses[app] = name
            await asyncio.sleep(0.2)

    def playing_within(self, seconds: float) -> list[str]:
        now = time.monotonic()
        return [app for app, t in self.last_playing.items() if now - t <= seconds]


def airpods_endpoint_id(mac: int) -> str | None:
    """Windows' id for the AirPods' stereo ("Headphones") output, to compare with the
    default output. Same registry lookup as auto_connect.audio_endpoint_state."""
    address = f"{mac:012x}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MMDEVICES_RENDER) as endpoints:
        for i in range(winreg.QueryInfoKey(endpoints)[0]):
            key = winreg.EnumKey(endpoints, i)
            try:
                with winreg.OpenKey(endpoints, key + r"\Properties") as props:
                    parent = winreg.QueryValueEx(props, PKEY_DEVICE_PARENT_ID)[0].lower()
            except OSError:
                continue
            if A2DP_UUID in parent and address in parent:
                return "{0.0.0.00000000}." + key
    return None


# ---- the engine ---------------------------------------------------------------------

class AirPodsEngine:
    def __init__(self, on_event, auto_connect: bool = False):
        self.on_event = on_event
        self.auto_connect = auto_connect
        try:
            self.airpods: PairedAirPods | None = PairedAirPods()
        except RuntimeError:
            self.airpods = None  # no AirPods paired with this PC: the flyout leaves them out

        self.loop: asyncio.AbstractEventLoop | None = None
        self.quitting = False
        self.state: AirPodsState | None = None
        self.battery: dict[str, tuple[int | None, bool]] = {}  # last known value per part
        self.connected = False
        self.connecting = False
        self.connect_failed = False
        self.disconnecting = False
        self.is_output = False           # the AirPods are Windows' current sound output
        self._endpoint_id: str | None = None

        self.lid_counters: dict[str, int] = {}
        self.last_lid_open = 0.0
        self.lid_open = False            # between a "lid_opened" and its "lid_closed"
        self.started = time.monotonic()
        self.case_seen_at = 0.0          # last advert showing the case battery (= lid open)
        self.both_in_case = False
        self.lid_closed_seen = False     # latest case advert had the lid-closed bit

        self.ears_seen: int | None = None       # latest in-ear count from adverts
        self.ears_stable: int | None = None     # debounced in-ear count
        self.ears_pending: int | None = None
        self.ears_pending_since = 0.0
        self.paused_by_us = False
        self.ears_before_pause = 0
        self.paused_apps: list[str] = []
        self.playing = PlayingTracker()
        self.ears_rssi = 0
        self.low_warned: set[str] = set()
        self._errors_logged: dict[str, float] = {}
        self._raw_seen: dict[str, tuple] = {}
        self._signal: dict[str, tuple[float, float]] = {}  # address -> (smoothed dBm, last seen)
        self._last_signature = None
        self.last_advert_at = time.monotonic()
        self.advert_count = 0

    # ---- called from the tray app's thread ----

    @property
    def paired(self) -> bool:
        return self.airpods is not None

    def start(self) -> None:
        threading.Thread(target=self._thread_main, name="airpods", daemon=True).start()

    def stop(self) -> None:
        self.quitting = True

    def request_connect(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.connect, True)

    def request_disconnect(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.disconnect)

    def model_name(self) -> str:
        if self.state:
            return self.state.model
        if self.airpods:
            return MODELS.get(self.airpods.model_id, "AirPods")
        return "AirPods"

    def battery_text(self) -> str:
        """"L 100%  R 100%  Case 40%" (with a bolt while charging), or "" if nothing heard yet."""
        parts = []
        for part, short in (("Left", "L"), ("Right", "R"), ("Case", "Case")):
            value, charging = self.battery.get(part, (None, False))
            if value is not None:
                parts.append(f"{short} {value}%" + (" ⚡" if charging else ""))
        return "   ".join(parts)

    # ---- the thread ----

    def _thread_main(self) -> None:
        # A plain (multi-threaded COM) thread: Bleak's WinRT callbacks then arrive without the
        # message pumping AirPodsTray needed on its GUI thread.
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            asyncio.run(self._main())
        except Exception as e:
            log("AirPods engine stopped:", repr(e))

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(
            lambda loop, ctx: log("error:", ctx.get("exception") or ctx["message"]))
        if not self.airpods:
            log("no paired AirPods found - AirPods features are off")
            return
        log(f"AirPods: {self.model_name()} ({self.airpods.mac:012X})")
        jobs = [self.loop.create_task(self._keep_running(self._watch_connection, "connection watcher")),
                self.loop.create_task(self._keep_running(self.playing.run, "media tracker")),
                self.loop.create_task(self._keep_running(self._tick, "ear/lid watcher"))]
        failures = 0
        while not self.quitting:
            try:
                await self._scan()
                failures = 0
            except Exception as e:
                failures += 1
                log(f"Bluetooth scan stopped ({failures} in a row): {e!r} - restarting")
                if failures >= MAX_SCAN_FAILURES:
                    # A fresh process has always been able to scan, even after sleep.
                    self.on_event("relaunch")
                    break
                await asyncio.sleep(2)
        for job in jobs:
            job.cancel()

    async def _keep_running(self, job, name: str) -> None:
        """Run a background job forever: if it crashes, log why and start it again."""
        while not self.quitting:
            try:
                await job()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"{name} crashed: {e!r} - restarting in 5 s")
                await asyncio.sleep(5)

    async def _scan(self) -> None:
        """Scan for adverts. Returns (so the caller restarts the scan) if adverts stop arriving -
        there are always other Bluetooth devices around, so total silence means Windows stopped
        the scan (e.g. the PC slept)."""
        self.last_advert_at = time.monotonic()
        last_heartbeat = time.monotonic()
        # Passive scanning: we only need the advertisement itself, never the scan response, and in
        # testing Windows delivered ~3x more AirPods adverts this way while audio was streaming.
        async with BleakScanner(detection_callback=lambda device, adv: self.on_advert(device, adv),
                                scanning_mode="passive"):
            while not self.quitting:
                now = time.monotonic()
                if now - self.last_advert_at > SCAN_SILENCE_RESTART:
                    log(f"no Bluetooth adverts for {SCAN_SILENCE_RESTART} s - restarting the scan")
                    return
                if now - last_heartbeat > HEARTBEAT_SECONDS:
                    log(f"alive: {self.advert_count} adverts in the last {HEARTBEAT_SECONDS // 60} min, "
                        f"connected={self.connected}")
                    self.advert_count, last_heartbeat = 0, now
                await asyncio.sleep(0.25)

    async def _tick(self) -> None:
        """What AirPodsTray did in its UI pump: act on in-ear changes, notice the lid closing."""
        while not self.quitting:
            self._check_ears()
            self._check_lid_closed()
            await asyncio.sleep(0.05)

    # ---- state ----

    def _changed(self) -> None:
        """Tell the app when something it shows changed (adverts arrive many times a second)."""
        signature = (self.model_name(), self.battery_text(), self.connected, self.connecting,
                     self.disconnecting, self.connect_failed)
        if signature != self._last_signature:
            self._last_signature = signature
            self.on_event("changed")

    def _log_error(self, what: str, error: BaseException) -> None:
        """Log an error, but each distinct error only once a minute (adverts arrive constantly)."""
        key, now = f"{what}: {error!r}", time.monotonic()
        if now - self._errors_logged.get(key, -1e9) > 60:
            self._errors_logged[key] = now
            log(key)

    def _check_low_battery(self) -> None:
        for part in ("Left", "Right", "Case"):
            value, charging = self.battery.get(part, (None, False))
            if value is None:
                continue
            if value <= LOW_BATTERY and not charging and part not in self.low_warned:
                self.low_warned.add(part)
                name = "Case" if part == "Case" else f"{part} AirPod"
                self.on_event("low_battery", f"{name} at {value}%")
            elif value > LOW_BATTERY + 10 or charging:
                self.low_warned.discard(part)

    # ---- Bluetooth adverts ----

    def on_advert(self, device, adv) -> None:
        self.last_advert_at = time.monotonic()
        self.advert_count += 1
        try:
            self._handle_advert(device, adv)
        except Exception as e:
            self._log_error("advert handling failed", e)

    def _log_raw_advert(self, address: str, rssi: int, data: bytes, s: AirPodsState, ignored: bool,
                        other_pair: bool = False) -> None:
        """Log each change in what a bud broadcasts (status bits + decoded result), to debug ear detection."""
        where = lambda ear, case: "ear" if ear else "case" if case else "out"  # noqa: E731
        key = (data[5], s.left_in_ear, s.right_in_ear, s.left_in_case, s.right_in_case)
        if self._raw_seen.get(address) == key:
            return
        self._raw_seen[address] = key
        sender = "L" if data[5] & 0x20 else "R"
        note = ("  (ignored: too far)" if rssi < EAR_RSSI else "  (ignored: no state)" if ignored
                else "  (ignored: someone else's AirPods)" if other_pair else "")
        log(f"  advert from {sender} bud {address[-5:]} {rssi} dBm status={data[5]:08b} -> "
            f"L {where(s.left_in_ear, s.left_in_case)}, R {where(s.right_in_ear, s.right_in_case)}{note}")

    def _is_other_pair(self, address: str, rssi: int) -> bool:
        """The broadcasts don't say whose AirPods they are, so lock onto the strongest signal of our
        model (ours sit next to the PC) and ignore addresses clearly weaker than it. Each address keeps
        a smoothed signal level so one strong or weak reading doesn't flip the choice."""
        now = time.monotonic()
        level = self._signal.get(address, (rssi, now))[0]
        self._signal[address] = (0.7 * level + 0.3 * rssi, now)
        recent = {a: lvl for a, (lvl, seen) in self._signal.items() if now - seen < PAIR_MEMORY}
        return self._signal[address][0] < max(recent.values()) - OTHER_PAIR_MARGIN

    def _handle_advert(self, device, adv) -> None:
        data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
        if not data:
            return
        data = bytes(data)
        s = decode(data)
        if s is None:
            return
        if self.airpods and ((data[3] << 8) | data[4]) != self.airpods.model_id:
            return  # a different AirPods model - not ours
        nothing_known = s.case is None and not (s.left_in_ear or s.right_in_ear
                                                or s.left_in_case or s.right_in_case)
        other_pair = not nothing_known and adv.rssi >= EAR_RSSI and self._is_other_pair(device.address, adv.rssi)
        self._log_raw_advert(device.address, adv.rssi, data, s, nothing_known, other_pair)
        if adv.rssi < EAR_RSSI or nothing_known or other_pair:
            return  # too far / lid closed and buds in a pocket / someone else's AirPods

        self.state = s
        for part, value, charging in (("Left", s.left, s.left_charging), ("Right", s.right, s.right_charging),
                                      ("Case", s.case, s.case_charging)):
            if value is not None:
                self.battery[part] = (value, charging)
        self.ears_seen = int(s.left_in_ear) + int(s.right_in_ear)
        if s.case is not None:  # the case battery is broadcast while the lid is open (and briefly after)
            self.lid_closed_seen = s.lid_closed
            if not s.lid_closed:
                self.case_seen_at = time.monotonic()
                self.both_in_case = s.left_in_case and s.right_in_case
        self.ears_rssi = adv.rssi

        # Lid opened: a fresh address with the case open, or a changed lid counter.
        before = self.lid_counters.get(device.address)
        self.lid_counters[device.address] = s.lid_counter
        now = time.monotonic()
        if (s.case is not None and not s.lid_closed and before != s.lid_counter
                and adv.rssi >= MIN_RSSI
                and now - self.started > 3 and now - self.last_lid_open > LID_COOLDOWN):
            self.last_lid_open = now
            self._on_lid_opened()

        self._check_low_battery()
        self._changed()

    def _on_lid_opened(self) -> None:
        log("lid opened")
        self.lid_open = True
        self.lid_closed_seen = False
        self.on_event("lid_opened")
        if self.auto_connect and not self.connected:
            self.connect()

    def _check_lid_closed(self) -> None:
        """The lid-closed bit appears in the very next broadcast. Fallback: no open-lid broadcast
        for a while (both buds were in the case)."""
        if not self.lid_open:
            return
        if self.lid_closed_seen:
            log("lid closed")
        elif self.both_in_case and time.monotonic() - self.case_seen_at > LID_CLOSED_AFTER:
            log("no broadcasts from the open case - treating the lid as closed")
        else:
            return
        self.lid_open = False
        self.on_event("lid_closed")

    # ---- connection ----

    def _output_check(self) -> bool:
        """Are the AirPods Windows' default output right now?"""
        if self._endpoint_id is None:
            self._endpoint_id = airpods_endpoint_id(self.airpods.mac)
        return self._endpoint_id is not None and default_output_id() == self._endpoint_id

    async def _watch_connection(self) -> None:
        # "Connected" = the AirPods are an active audio output (a cheap registry read), checked
        # every 0.25 s so a disconnect pauses media quickly. The Bluetooth link's own status/event
        # is no use here: it often stays up after the audio has gone.
        while not self.quitting:
            if not self.connecting:
                try:
                    connected = await self.airpods.is_connected()
                    was_output = self.is_output
                    self.is_output = connected and self._output_check()
                    self._set_connected(connected, was_output)
                except OSError as e:
                    log("connection check failed:", e)
            await asyncio.sleep(0.25)

    def _set_connected(self, connected: bool, was_output: bool) -> None:
        if connected == self.connected:
            return
        was_connected, self.connected = self.connected, connected
        self.disconnecting = False
        log("connected" if connected else "disconnected")
        if was_connected and not connected and was_output:
            # Stop YouTube & co. carrying on through the PC speakers. Only when the AirPods were
            # the output: if a HomePod was playing, it keeps playing.
            self.paused_by_us = False
            self.loop.create_task(self._pause_after_disconnect())
        self._changed()

    async def _pause_after_disconnect(self) -> None:
        log("AirPods disconnected - pausing media")
        await pause_media()

    def connect(self, make_output: bool = False) -> None:
        """make_output: also switch Windows' output to the AirPods (a click in the list means
        "play here", even if a HomePod is playing)."""
        if self.connecting or not self.airpods:
            return
        self.connecting, self.connect_failed = True, False
        self._changed()
        self.loop.create_task(self._connect(make_output))

    async def _connect(self, make_output: bool) -> None:
        log("connecting...")
        try:
            ok = await self.airpods.connect()
        except OSError:
            ok = False
        log("connect", "ok" if ok else "failed")
        self.connecting, self.connected, self.connect_failed = False, ok, not ok
        if ok and make_output:
            self._endpoint_id = self._endpoint_id or airpods_endpoint_id(self.airpods.mac)
            if self._endpoint_id and default_output_id() != self._endpoint_id:
                try:
                    set_default_output(self._endpoint_id)
                    log("made the AirPods the sound output")
                except Exception as e:
                    log("couldn't switch the output to the AirPods:", repr(e))
        self._changed()

    def disconnect(self) -> None:
        if self.airpods:
            try:
                self.airpods.disconnect()
            except OSError as e:
                log("disconnect failed:", e)
                return
            log("disconnect sent")
            if self.connected:
                self.disconnecting = True
                self._changed()

    # ---- auto-pause ----

    def _check_ears(self) -> None:
        count = self.ears_seen
        if count is None or count == self.ears_stable:
            self.ears_pending = None
            return
        now = time.monotonic()
        if self.ears_pending != count:
            self.ears_pending, self.ears_pending_since = count, now
            return
        if now - self.ears_pending_since < EAR_DEBOUNCE:
            return
        previous, self.ears_stable, self.ears_pending = self.ears_stable, count, None
        log(f"buds in ears: {previous} -> {count}  ({self.ears_rssi} dBm, connected={self.connected}, "
            f"output={self.is_output})")
        if previous is None:
            return
        # Connect the moment a bud goes in, disconnect the moment the last one comes out, instead of
        # waiting for the AirPods/Windows to switch on their own (which takes until the lid closes).
        if count > previous and not self.connected and self.auto_connect:
            log("  bud put in -> connecting")
            self.connect()
            return
        if count == 0 and self.connected:
            log("  both buds out -> disconnecting")
            self.disconnect()  # pause-on-disconnect takes care of the media
            return
        if not self.connected or not self.is_output:
            log("  (not acting: the AirPods aren't this PC's sound output)")
            return
        if count < previous and not self.paused_by_us:
            self.loop.create_task(self._pause(previous))
        elif count > previous and self.paused_by_us and count >= self.ears_before_pause:
            self.loop.create_task(self._resume())

    async def _pause(self, ears_before: int) -> None:
        # Include apps the AirPods already paused themselves in the last few seconds.
        recently_playing = self.playing.playing_within(EAR_DEBOUNCE + 3)
        paused_now = await pause_media()
        self.paused_apps = sorted(set(recently_playing) | set(paused_now))
        if self.paused_apps:
            self.paused_by_us, self.ears_before_pause = True, ears_before
            log("will resume when the bud goes back in:", self.paused_apps)

    async def _resume(self) -> None:
        self.paused_by_us = False
        await resume_media(self.paused_apps)

