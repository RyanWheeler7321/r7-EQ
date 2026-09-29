![r7-EQ](media/r7-eq.png)

<img src="r7eq/icon.svg" alt="r7-EQ icon" width="96">

# r7-EQ

r7-EQ is a small EQ app for Equalizer APO. I have two outputs, computer speakers and headphones, and I made it mostly to bring down the dynamic range on the speakers and shape the sound the way I like.

The EQ curve takes up to 64 points and sits on top of a live spectrum analyzer. Below it are dials for tone (tilt, warmth, presence and air), stereo width, 3D, room reverb and the compressor. The compressor is based on Chrome's Web Audio compressor code (see `THIRD_PARTY_NOTICES.md`), and the T button sets it to a reasonable default for TV, dialogue and other non-music stuff. A limiter at the end keeps it from clipping.

Every playback device gets its own profile and presets, and r7-EQ switches to whichever device Windows is using. The processing runs inside Equalizer APO, so it keeps working with the window closed.

It's pretty barebones and mostly made for my own setup, but it should work with anything Equalizer APO supports. I run it on Windows 11 with Python 3.12.

## Setup

1. Install [Equalizer APO](https://sourceforge.net/projects/equalizerapo/) and tick your playback devices in its Device Selector.
2. Install the Python packages: `python -m pip install PySide6 numpy SoundCard pycaw`
3. Put the three DLLs from the release zip in a `plugins` folder next to `r7-EQ.pyw`, or build them with `native\build.cmd` (needs Visual Studio 2022 Build Tools).
4. Give the Windows audio service read access to that folder: `icacls plugins /grant "*S-1-5-19:(OI)(CI)RX"`. Without it only the EQ curve and tone dials work, and r7-EQ shows a warning.
5. Run `pythonw r7-EQ.pyw`.

The first time it runs, it adds an include block to Equalizer APO's `config.txt` and keeps the original as `config.before-r7-eq.txt`. Closing the window only hides it. Run it again to bring it back, or with `--stop` to quit.

## Controls

- Double-click the graph to add a point
- Right-click a point, or click it again once it's selected, to reset, edit or remove it
- Double-click a point or press `Enter` to type exact values
- `Delete` removes the selected point
- Drag a dial up or down, holding `Shift` for finer steps, and double-click a dial to reset it
- The All switch turns r7-EQ on or off for every device, and the EQ and Comp switches only affect the current device

## Headsets with their own surround

If your headset software does its own surround (G HUB and similar), open the Device Selector's troubleshooting options, untick "Install APO" for pre-mix, and untick automatic mode adjustment. The surround keeps working and r7-EQ runs after it.
