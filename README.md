# HomePod Bridge

Play your Windows PC's sound on a HomePod (or any AirPlay 2 speaker, including
a Mac with AirPlay Receiver turned on).

Windows can't speak AirPlay, so HomePod Bridge sits in the system tray and
streams everything your PC plays to the speaker: lossless (ALAC), with about
half a second of delay.

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

Audio is captured from VB-Cable (or the default speakers' loopback), resampled
to 44.1 kHz and sent with [pyatv](https://pyatv.dev) as uncompressed ALAC
frames, which HomePods handle more reliably than pyatv's raw PCM mode. pyatv's
default 1.5 s playback delay is lowered to 0.5 s, the measured minimum that
stays glitch-free on a HomePod mini.
