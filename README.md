# HomePod Bridge

Your Apple audio devices on Windows, from one tray icon:

- **HomePod and AirPlay speakers:** play your PC's sound on a HomePod (or any
  AirPlay 2 speaker, including a Mac with AirPlay Receiver turned on). Windows
  can't speak AirPlay, so HomePod Bridge streams everything your PC plays to the
  speaker: lossless (ALAC), with about half a second of delay.
- **AirPods** (formerly the separate AirPodsTray app): battery for each bud and
  the case, connect / disconnect, the list pops up when you open the case near
  the PC, media pauses when you take a bud out and resumes when it goes back in,
  and low-battery warnings.

## Install

1. Download `HomePodBridge.exe` from the [latest release](../../releases/latest)
   and put it in a folder of your choice.
2. Optional but recommended: install the free
   [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) and restart.
   With it, your speaker shows up in Windows' own sound menu and your laptop
   stays silent while you play on the HomePod. Without it, the PC's speakers
   play too.
3. Run `HomePodBridge.exe`. When Windows asks, allow it on **private networks**.

## Use

- **Click the tray icon** to see the AirPlay speakers on your network and
  **Connect** to one. The refresh icon searches again.
- **Or pick the speaker in Windows' sound menu** (taskbar speaker icon > output
  list). It's listed under the speaker's name; picking it connects, picking
  your laptop speakers disconnects.
- **Volume keys** control the HomePod's volume while it's playing.
- **Right-click the tray icon** for *Check for updates*, *Start with Windows*
  and *Quit*.

If someone else AirPlays to the speaker, HomePod Bridge steps aside and gives
your laptop its sound back.

**AirPods** appear at the top of the same list once they're paired with
Windows (*Settings > Bluetooth & devices > Add device*, with the case button held
until the light flashes white). Open the case near the PC to see the battery.
Connecting them makes them the sound output; taking both buds out disconnects
them and pauses what was playing. Auto-pause only acts while the AirPods are the
output, so a HomePod that's playing is left alone. *Auto-connect AirPods* (in
the right-click menu) connects them as soon as you open the case or put a bud in.

If you used AirPodsTray, this app replaces it: on first start it closes
AirPodsTray and takes over its *Start with Windows* and auto-connect settings.

## Requirements

- Windows 10/11, on the same network as the speaker (not a guest network).
- The speaker must accept this PC: in the Home app, *Home Settings > Allow
  Speaker & TV Access* set to *Everyone* or *Anyone on the same network*.
  On a Mac: *System Settings > General > AirDrop & Handoff > AirPlay Receiver*,
  allow *Anyone on the same network*.

## Building from source

```
pip install -r requirements.txt
python build_exe.py
```

The exe lands in `dist\`.

## Publishing an update

1. Bump `APP_VERSION` in `homepod_bridge.py`.
2. `python build_exe.py`
3. `gh release create v<version> dist/HomePodBridge.exe --notes "What changed"`

The app's *Check for updates* installs the newest release that has
`HomePodBridge.exe` attached.

## How it works

**AirPods** battery, lid and in-ear state come from the Bluetooth LE
"proximity pairing" broadcasts AirPods send (no connection needed); decoding is
in `airpods_scanner.py`. Connecting uses the Windows Bluetooth audio driver's
one-shot reconnect, like the Connect button in Windows settings
(`auto_connect.py`). `airpods.py` ties it together on its own thread.

**AirPlay** audio is captured from VB-Cable (or the default speakers' loopback), resampled
to 44.1 kHz and sent with [pyatv](https://pyatv.dev) as uncompressed ALAC
frames, which HomePods handle more reliably than pyatv's raw PCM mode. pyatv's
default 1.5 s playback delay is lowered to 0.5 s, the measured minimum that
stays glitch-free on a HomePod mini.
