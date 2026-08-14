# Lysdexic TTS — text-to-speech for dyslexic readers

Select-anywhere streaming text-to-speech for Windows, built for dyslexic
readers on top of [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M).

Highlight text in **any** application and a small pill appears next to it with
**Read** and **Copy**. Press Read and it starts speaking in about a second,
synthesizing each sentence in the background while the previous one plays. A
floating panel shows the captured text with the sentence currently being spoken
highlighted, and transport controls stay visible the whole time.

No copying into a separate window. No right-click menu to hunt for.

That focus shapes the whole interface: adjustable text size and line spacing
in the reading window, a sentence highlight that tracks the voice so you never
lose your place, and settings written as plain sentences with ordinary
controls rather than configuration syntax.

---

## What it does, and does not do, with your input

To work at all, this app installs a global mouse hook, can synthesize a Ctrl+C
keystroke, and reads the clipboard. That combination deserves a straight answer
about what it actually does, so here it is.

**What it does**

- Watches for the *shape* of a mouse gesture — press, drag, release — to decide
  whether you just selected something. It records button positions only.
- Reads the selected text through **UI Automation**, the public accessibility
  API that screen readers use. This is the normal path and it has no side
  effects at all.
- Falls back to a synthetic **Ctrl+C** only when UI Automation is blind (some
  Electron apps, some PDF readers), and only *after you click Read or Copy* —
  never during the passive check that decides whether to show the pill. When it
  does run, your clipboard is saved and restored around it.

**What it does not do**

- **No keyboard hook.** It is a *mouse* hook. It never observes a keystroke.
- **No text is ever written to disk.** Logging redacts captured text to a
  character count (`log_text` is off and there is no code path that writes it).
- **No network.** Once the model is cached it sets `HF_HUB_OFFLINE=1` and makes
  no outbound connections. Nothing you read is sent anywhere.
- **No process injection, no reading other processes' memory.**
- **No elevation.** It deliberately refuses to run elevated — an elevated
  process could not hook or send input to normal windows anyway.
- **Terminals are excluded** from the Ctrl+C path entirely, because there
  Ctrl+C means "interrupt", not "copy".

Everything here is documented Win32 and UI Automation API usage, the same as in
`pynput`, `pywinauto`, AutoHotkey, PowerToys, and any clipboard manager.

**One practical consequence:** antivirus behavioural heuristics sometimes flag
the hook + input-injection + clipboard combination on sight. It ships as plain
Python source rather than a packed executable for exactly this reason — packers
are what actually trip those heuristics — but you may still need a Defender
exclusion for the folder.

You can turn the watcher off entirely (tray → *Select-to-read*, or
`selection.mode = "off"`) and use the tray's *Read clipboard* instead; the hook
is never installed in that mode.

---

## Quick start

Double-click **`LysdexicTTS.cmd`**, or:

```bash
venv\Scripts\pythonw.exe run_reader.pyw
```

It runs in the **system tray**. Windows 11 files new tray icons into the hidden
`^` overflow flyout, so on first run the app also shows a notification and opens
the reader panel — drag the icon out of the flyout to pin it next to the clock.

If it ever seems not to start, run **`LysdexicTTS-debug.cmd`**, which is the same
thing with a console window, and check
`%LOCALAPPDATA%\KokoroReader\logs\reader.log`.

Try the engine from the command line without any UI:

```bash
venv\Scripts\python.exe -m tools.stream_cli --file tools\sample_article.txt --stats
```

Run the tests:

```bash
venv\Scripts\python.exe -m pytest tests -q
```

Turn on **Start with Windows** from the tray menu once you're happy with it.

### Using it

| | |
|---|---|
| Select text anywhere | The pill appears — **Read** or **Copy** |
| Transport | Buttons on the panel: prev · play/pause · next · stop |
| Previous | Restarts the current sentence if you're >2s in, else steps back |
| Jump | Click any sentence in the panel |
| Panic stop | `Ctrl+Alt+Esc`, registered only while audio is playing |
| Background sound | Button beside the gear, when keep-alive is on — silences it without stopping the reading |
| Tray | Read clipboard, show panel, edit settings, autostart, quit |

Settings live under tray → *Settings…* — a plain visual window with real
controls, grouped as Voice, Reading, Selecting text, Text size, Starting up and
Advanced. Every option has a short plain-language description; nothing requires
reading configuration syntax. Changes apply and save as you make them.

**Text size and line spacing are first-class settings**, not theme details.
Generous type and loose line spacing are among the few interventions with real
evidence behind them for dyslexic readers, so they get their own page with a
live preview.

**Keep the sound connection open** (Voice page) is for hearing aids, Bluetooth
headphones, and anything with noise cancelling. The pauses between sentences are
exact digital zeros, and some devices read a run of zeros as "no signal" and gate
their processing off and on — audible as the noise cancelling switching around
every sentence. Enabling this mixes a near-inaudible noise floor into the output so
it is never digitally silent. Measured on a 5.4-second read: 0.51 s of digital
silence across 6 blocks becomes zero, with the speech itself unchanged.

The level is in RMS dBFS and −70 is a good starting point; some devices need
more signal than others to stay awake. Three sounds are offered, defaulting to
**brown** (−6 dB/octave) because at a given level it is the hardest of the three
to notice, which is exactly what you want from a signal whose only job is to
stop the audio path going to sleep. Pink (−3 dB/octave) is what people usually
mean by "white noise" — rain, a fan. Actual white noise is flat, so most of its
energy sits in the top octaves and it sounds like an electrical buzz; it is
there if you want it. The tables are built in the frequency domain, so they loop
seamlessly and land on exactly the requested RMS level.

It stays on while the reading is paused and stopped, deliberately — switching it
off at those moments would put the gap back exactly where you notice it most.
When you genuinely need silence, the reader panel grows a **background-sound
button** next to the settings gear that silences it in one click without
stopping what is being read. That button only appears when the feature is
switched on.

Under *Advanced* there's still a raw TOML editor for anyone who prefers it, and
it validates before saving so a typo can't stop the app from starting. The file
lives at `%APPDATA%\KokoroReader\settings.toml`. It is edited in-app rather than
shelled out to an external editor because `.toml` has no file association on a
default Windows install, and Windows 11's tabbed Notepad silently swallows files
opened from a background process. Logs are at
`%LOCALAPPDATA%\KokoroReader\logs\`. Those storage paths, the single-instance
mutex and the autostart registry value deliberately keep the original
`KokoroReader` name so an existing install keeps its settings and can't leave a
stale Run entry behind; only the user-facing name changed.

---

## Why it is built this way

Measurements on this machine (i7-1165G7, 4 cores, **no CUDA**) drove nearly
every design decision:

| Measurement | Value | Consequence |
|---|---|---|
| Synthesis real-time factor | **2.4–3.2×** | Three sentences of lookahead never starves |
| Fixed overhead per synthesis call | **~0.4 s** regardless of length | Don't over-split; a 3-word sentence costs nearly as much as a 30-word one |
| Silence padding per chunk | **0.31–0.38 s** leading, **0.49 s** trailing | Naive concatenation leaves ~0.85 s of dead air between *every* sentence |
| Waveform edge samples | **exactly ±0.0** | Concatenation is inherently click-free; no crossfade needed |
| Cold start | **~7 s** | The app must stay resident with the model warm |

### Results

| | Before | After |
|---|---|---|
| Time to first audio, typical document | 1.07 s | **0.87–0.94 s** |
| Time to first audio, long opening sentence | 5.67 s | **1.71 s** |
| Gaps between sentences (35-sentence article) | 1.03 s across 1 stall | **0 stalls** |
| Audio underruns | 0 | **0** |

The long-opening-sentence case matters most: it is what happens when you
highlight a paragraph and press Read. It was fixed by expanding sentences into
smaller **playback units** while the buffer is still cold.

---

## Architecture

```
raw text
   │
   ├─ text/normalize.py   PDF de-wrap & de-hyphenation, bullets, headings,
   │                      NBSP/zero-width cleanup — one pass, keeping an
   │                      offset map back to the original string
   ├─ text/splitter.py    spaCy tok2vec+senter → sentences, tiny-fragment merge
   ├─ text/units.py       sentences → playback units (long sentences cut at
   │                      clause boundaries *only while the buffer is cold*)
   ├─ core/scheduler.py   synthesis thread, bounded lookahead, cancellation
   │   └─ tts/kokoro_engine.py → tts/postproc.py (trim silence, shape pauses)
   │        └─ core/cache.py    text-keyed LRU, bounded by total seconds
   └─ audio/player.py     sounddevice OutputStream callback + transport

win/  hook · selection · uia · clipboard · keys · capture · window · dpi
      hotkey · autostart · singleton
ui/   pill · reader_panel · tray · icons · theme
```

`core/controller.py` ties it together and is the only thing the UI talks to. It
translates *sentence*-level intent ("next sentence") into *unit*-level playlist
jumps.

### Threading

| Thread | Owns | Rules |
|---|---|---|
| UI / main | all widgets | Never blocks. Polls player state every 33 ms. |
| `win-hook` | the mouse hook | Its procedure appends to a deque and returns. Nothing else. |
| selection watcher | UI Automation (STA COM) | UIA calls are cross-process and can block for seconds. |
| `tts-synth` | `KPipeline`, both spaCy instances | Sole caller of torch. |
| PortAudio callback | — | Bounded work only: slice copies and integer bookkeeping. No allocation, no locks, no logging, and **no exception may escape** (it would abort the stream). |

Control threads write plain attributes (atomic under the GIL); the callback
publishes progress as plain integers the UI polls. **No lock is reachable from
the audio callback.** Headroom against GIL contention with torch comes from
buffer depth — `blocksize=2048` is 85 ms at 24 kHz — not from lock-freedom,
which is unattainable in a Python callback. `torch_threads` defaults to 4, not
8: 8 benchmarks ~20 % faster but oversubscribes a 4-core part and competes
directly with the audio thread.

### Capture, and why it doesn't touch your clipboard

1. **UI Automation first.** The watcher reads the selection directly — no
   clipboard, no keystrokes. Measured at ~16 ms in Notepad, and it also returns
   a bounding rectangle used to place the pill.
2. **Synthetic Ctrl+C only as a fallback,** and only once you actually click
   Read or Copy. The passive probe that decides whether to show the pill never
   touches the clipboard, so merely selecting text can never clobber what you
   had copied. When the fallback does run, the clipboard is snapshotted and
   restored around it, and terminals are excluded entirely because Ctrl+C means
   "interrupt" there.

The pill is a `WS_EX_NOACTIVATE` window: clicking it never moves focus, so the
source application keeps your selection alive.

### Transport

Every discontinuity — pause, resume, stop, next, previous, seek — routes through
a single `_pending` slot in the player so it lands on an 8 ms raised-cosine fade
and cannot click. The fade is shorter than one block, so fade-out, transition
and fade-in all complete inside one callback.

- **Pause never stops the device.** WASAPI stop/start is audible and costs
  50–150 ms to resume, so pausing writes silence with the stream running.
- **Starvation degrades to silence, not to a stopped stream.**
- **Previous** restarts the current sentence if you're more than 2 s in — the
  rule every music player uses, because reaching for "back" usually means "say
  that again".

### Cancellation

A torch forward pass cannot be interrupted, so this doesn't pretend otherwise.
Cancellation is expressed in terms of *audio*, not compute: every request
carries a generation number checked immediately before inference starts, so
stale work costs nothing; a pass already in flight finishes and its result is
cached anyway (the cache is keyed by text). Audio silences in 8 ms, so Stop
feels instant.

---

## Things worth knowing

- **Antivirus.** A global mouse hook plus synthetic keystrokes plus clipboard
  reads is, behaviourally, the signature of a keylogger. It ships as a plain
  script rather than a packed executable (packers are what actually trip
  heuristics) and **never writes captured text to disk**, but you may still need
  a Defender exclusion.
- **Voices download themselves.** On first run the app fetches a starter set of
  five — Heart, Bella, Michael (American) and Emma, George (British) — the same
  way the model itself arrives, about 500 KB each. 23 more English voices are
  one click away under *Settings → Voice → Get another voice*. Nothing is
  vendored into this repository; set `engine.fetch_default_voices = false` to
  stay strictly offline and keep only `af_heart`.
- **`HF_HUB_OFFLINE` is set automatically** when the model snapshot is already
  cached, because kokoro otherwise makes a network round trip on every startup
  to revalidate — which would stall a tray app at logon.
- **espeak fallback is verified at startup.** Without it kokoro *silently drops*
  out-of-dictionary words, which is the worst failure mode for a reader because
  nothing looks wrong.
- **`pythonw.exe` has no stdout/stderr**, and kokoro's import-time loguru setup
  raises on a `None` sink. `reader.log.ensure_std_streams()` must run before
  anything imports kokoro — otherwise the engine fails to load on every
  autostart while working perfectly from a console.
- **Aggressive selection mode misfires sometimes** — that is the accepted cost
  of the pill also working in Electron apps and PDF readers where UI Automation
  is blind. Switch `selection.mode` to `uia_only` or `modifier` in settings if
  it gets noisy.
- **The hook can be silently dropped** by Windows if its procedure ever exceeds
  300 ms. A watchdog detects that (two consecutive confirmations, to avoid
  racing `SetCursorPos`) and reinstalls it.
- **PDF de-hyphenation is a heuristic.** It correctly rejoins `meas-\nure`, but
  it will also rejoin a genuinely hyphenated word that falls at a line break.
- **Elevated windows are invisible to it.** A non-elevated hook receives no
  events while an admin window has focus, and this app deliberately stays
  non-elevated — an elevated process could not send input to normal windows.

---

## Status

- [x] **Phase 0** — repo hygiene, dependency manifest
- [x] **Phase 1** — streaming pipeline, verified headless (0 underruns, 0 stalls)
- [x] **Phase 2** — tray app, floating panel, live sentence highlighting, transport
- [x] **Phase 3** — global selection capture: mouse hook, UI Automation,
      clipboard fallback, the Read/Copy pill
- [x] **Phase 4** — TOML settings, autostart, single instance, logging,
      per-monitor DPI, device-loss recovery, panic hotkey

Not built yet: word-level highlighting — the per-word timings are already
computed and kept on every chunk, so it is mostly UI work.

Kokoro-82M is Apache-2.0 licensed. The model and its voice packs are downloaded
from [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) at runtime
rather than redistributed here.
