"""Application wiring: tray, panel, pill, controller, and the 33ms UI tick.

Startup order matters. DPI awareness is declared before ``QApplication`` exists;
the tray icon appears immediately while the ~7s model load runs on a worker
thread (an app that launches at logon must not block on torch before showing any
sign of life); and the global mouse hook is installed only *after* that load,
because loading torch holds the GIL in long bursts and Windows silently drops a
hook procedure that can't answer within 300ms.

The UI thread never blocks. It polls the player's plain integer counters on a
timer rather than being called back from the audio thread, which is what keeps
the audio callback free of signals and locks.
"""

from __future__ import annotations

import logging
import sys
import threading
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import APP_NAME
from . import config as configmod
from . import log as logmod
from . import paths
from .core.controller import ReaderController
from .text.splitter import SentenceSplitter
from .tts.kokoro_engine import KokoroEngine
from .ui.icons import app_icon
from .ui.pill import SelectionPill
from .ui.reader_panel import ReaderPanel
from .ui.settings_dialog import SettingsDialog
from .ui.settings_window import SettingsWindow
from .ui.tray import Tray
from .win import dpi, shell, singleton
from .win import capture as capmod
from .win import window as winwin
from .win.hotkey import StopHotkey
from .win.selection import SelectionWatcher

log = logging.getLogger(__name__)

TICK_MS = 33
TOPMOST_REASSERT_MS = 3000
MAX_CHARS = 400_000

WELCOME_TEXT = """Lysdexic TTS is running.

Select any text, anywhere on your computer, and a small pill will appear next
to it with a Read button. Press it and the text is read aloud here, one
sentence at a time, with the sentence being spoken highlighted.

The buttons at the top of this panel control playback while it reads. Previous
restarts the current sentence if you are more than two seconds into it, and
steps back otherwise. You can also click any sentence to jump straight to it.

The app lives in your system tray. If you cannot see its icon, it is hidden
under the small upward arrow next to the clock, and you can drag it out to pin
it there permanently.

Press play to hear this out loud.
"""


class ReaderApp(QObject):
    engine_loaded = Signal(bool, str)

    def __init__(self, app: QApplication, cfg: configmod.Config | None = None) -> None:
        super().__init__()
        self.app = app
        self.cfg = cfg or configmod.load()

        self.engine = KokoroEngine(
            lang_code=self.cfg.get("engine", "lang_code"),
            repo_id=self.cfg.get("engine", "repo_id"),
            voice=self.cfg.get("engine", "voice"),
            speed=float(self.cfg.get("engine", "speed")),
            torch_threads=int(self.cfg.get("engine", "torch_threads")),
            trim_lead=bool(self.cfg.get("audio", "trim_lead")),
            prefer_offline=bool(self.cfg.get("engine", "prefer_offline")),
        )
        self.splitter = SentenceSplitter(
            trailing_pause_s=float(self.cfg.get("audio", "trailing_pause_s")),
            paragraph_pause_s=float(self.cfg.get("audio", "paragraph_pause_s")),
            max_sentences=int(self.cfg.get("ui", "max_sentences")),
        )
        self.ctl = ReaderController(
            self.engine,
            splitter=self.splitter,
            voice=self.cfg.get("engine", "voice"),
            speed=float(self.cfg.get("engine", "speed")),
            lookahead_sentences=int(self.cfg.get("playback", "lookahead_sentences")),
            prev_restart_threshold_s=float(
                self.cfg.get("playback", "prev_restart_threshold_s")
            ),
            blocksize=int(self.cfg.get("audio", "blocksize")),
            device=(self.cfg.get("audio", "device") or None),
        )
        self.ctl.player.volume = float(self.cfg.get("audio", "volume"))

        self.panel = ReaderPanel()
        self.pill = SelectionPill(
            auto_hide_ms=int(self.cfg.get("selection", "pill_auto_hide_ms"))
        )
        self.tray = Tray()
        self.tray.setIcon(app_icon())
        self.tray.set_state("loading", f"{APP_NAME} — loading model...")
        self.tray.show()

        self._ready = False
        # Two distinct states that were previously conflated. `_reading` means
        # "audio is actively playing"; `_has_document` means "something is
        # loaded and can be played". Transport must be gated on the latter,
        # otherwise finishing or stopping a read leaves the panel inert.
        self._reading = False
        self._has_document = False
        self._pending_text: str | None = None
        self._candidate = None
        self._settings_dialog: SettingsDialog | None = None
        self._settings_window: SettingsWindow | None = None
        # Session-scoped: the settings checkbox stays the persistent master
        # switch, this is just "quiet for a moment".
        self._noise_paused = False

        self.watcher = self._make_watcher()

        self.hotkey = StopHotkey(self._on_panic_stop)
        self.app.installNativeEventFilter(self.hotkey)

        self._wire()
        self._register_own_windows()

        self._apply_settings()

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._topmost = QTimer(self)
        self._topmost.setInterval(TOPMOST_REASSERT_MS)
        self._topmost.timeout.connect(self._reassert_topmost)
        self._topmost.start()

        threading.Thread(
            target=self._load_engine, name="engine-load", daemon=True
        ).start()

    # -------------------------------------------------------------- wiring

    def _make_watcher(self) -> SelectionWatcher:
        sel = self.cfg.section("selection")
        return SelectionWatcher(
            mode=sel.get("mode", "aggressive"),
            probe_delay_ms=int(sel.get("probe_delay_ms", 120)),
            min_chars=int(sel.get("min_chars", 1)),
            ignore_classes=sel.get("ignore_classes", ()),
            ignore_processes=sel.get("ignore_processes", ()),
            enable_double_click=bool(sel.get("enable_double_click", True)),
            enable_triple_click=bool(sel.get("enable_triple_click", True)),
            ignore_injected=bool(sel.get("ignore_injected", True)),
        )

    def _wire(self) -> None:
        self.panel.play_pause_clicked.connect(self._on_play_pause)
        self.panel.stop_clicked.connect(self._on_stop)
        self.panel.next_clicked.connect(self.ctl.next_sentence)
        self.panel.prev_clicked.connect(self.ctl.prev_sentence)
        self.panel.sentence_clicked.connect(self._on_sentence_clicked)
        self.panel.settings_clicked.connect(self._on_open_settings)
        self.panel.noise_toggled.connect(self._on_toggle_noise)

        self.tray.read_clipboard.connect(self._on_read_clipboard)
        self.tray.show_panel.connect(self._on_show_panel)
        self.tray.stop_reading.connect(self._on_stop)
        self.tray.quit_requested.connect(self.quit)
        self.tray.open_settings.connect(self._on_open_settings)
        self.tray.open_settings_folder.connect(self._on_open_settings_folder)
        self.tray.reload_settings.connect(self._on_manual_reload)
        self.tray.watcher_toggled.connect(self._on_watcher_toggled)

        self.pill.read_clicked.connect(self._on_pill_read)
        self.pill.copy_clicked.connect(self._on_pill_copy)

        self.watcher.candidate.connect(self._on_candidate)
        self.watcher.hook_state_changed.connect(self._on_hook_state)

        self.engine_loaded.connect(self._on_engine_loaded)

    def _register_own_windows(self) -> None:
        """Our own windows must never be mistaken for a text selection."""
        hwnds = []
        for w in (self.panel, self.pill):
            try:
                hwnds.append(winwin.hwnd_of(w))
            except Exception:
                pass
        self.watcher.set_own_windows(hwnds)

    # ------------------------------------------------------------- startup

    def _load_engine(self) -> None:
        """Runs on a worker thread; the signal marshals back to the GUI thread."""
        try:
            self.engine.load()
            self.splitter.warm()
            if self.cfg.get("engine", "warm_on_start"):
                self.engine.warm()
            self.ctl.start()
            # Pull down the starter voices the same way the model and af_heart
            # arrive. Best-effort and after warm-up, so it never delays the
            # first read and offline simply means fewer voices.
            if self.cfg.get("engine", "fetch_default_voices"):
                try:
                    self.engine.ensure_default_voices()
                except Exception:
                    log.debug("could not fetch the starter voices", exc_info=True)
            warning = (
                ""
                if self.engine.espeak_fallback_ok
                else "espeak fallback missing: some words will be skipped"
            )
            self.engine_loaded.emit(True, warning)
        except Exception as exc:
            log.exception("engine failed to load")
            self.engine_loaded.emit(False, str(exc))

    def _on_engine_loaded(self, ok: bool, message: str) -> None:
        self._ready = ok
        if not ok:
            self.tray.set_state("error", f"{APP_NAME} — failed to load: {message}")
            self.panel.set_status("engine failed to load")
            return

        # Only now install the hook: the load window is the one time this
        # process predictably starves the GIL, and Windows drops slow hooks.
        if self.cfg.get("selection", "mode") != "off":
            self.watcher.start()
        else:
            self.tray.set_watcher_checked(False)

        if not self.watcher.hook.installed and self.cfg.get("selection", "mode") != "off":
            self.tray.set_state(
                "warning", f"{APP_NAME} — ready (select-to-read unavailable)"
            )
        elif message:
            self.tray.set_state("warning", f"{APP_NAME} — {message}")
        else:
            self.tray.set_state("ready", f"{APP_NAME} — ready")
        self.panel.set_status("ready")

        if self._pending_text is not None:
            text, self._pending_text = self._pending_text, None
            self.read(text)
            return

        if self.cfg.get("app", "notify_on_ready"):
            self.tray.showMessage(
                f"{APP_NAME} is running",
                "Select text in any app and press Read on the pill.\n"
                "The tray icon may be under the ^ arrow — drag it out to pin it.",
                QSystemTrayIcon.MessageIcon.Information,
                6000,
            )
        if self.cfg.get("app", "first_run"):
            self._show_welcome()
            self.cfg.set("app", "first_run", False)
            configmod.save(self.cfg)

    def _show_welcome(self) -> None:
        """First-run proof of life: something visible, and readable out loud."""
        self.read(WELCOME_TEXT)
        self.ctl.pause()  # don't start talking unprompted
        self.panel.set_status("ready — press play, or select text anywhere")

    # ------------------------------------------------------------- reading

    def read(self, raw_text: str) -> None:
        if not raw_text or not raw_text.strip():
            self.tray.showMessage(
                APP_NAME, "Nothing to read.",
                QSystemTrayIcon.MessageIcon.Information, 2000,
            )
            return
        if len(raw_text) > MAX_CHARS:
            raw_text = raw_text[:MAX_CHARS]
        if not self._ready:
            # Show the text right away so the click feels acknowledged, and
            # queue the read for when the model finishes loading.
            self._pending_text = raw_text
            self.panel.set_document(raw_text, [])
            self.panel.set_status("loading model...")
            self.panel.show_floating()
            return

        n = self.ctl.read(raw_text)
        self.panel.set_document(self.ctl.raw_text, self.ctl.sentences)
        self.panel.set_enabled_transport(True)
        self.panel.set_status(
            f"0 / {n}" + ("  ·  truncated" if self.ctl.truncated else "")
        )
        if self.cfg.get("ui", "show_panel_on_read"):
            self.panel.show_floating()
        self._has_document = n > 0
        self._reading = True
        self.tray.set_reading(True)
        self.tray.set_state("reading", f"{APP_NAME} — reading {n} sentences")

        if self.cfg.get("app", "stop_hotkey_enabled"):
            self.hotkey.register(str(self.cfg.get("app", "stop_hotkey")))

    def _end_playback(self) -> None:
        """Playback stopped or ran out. The document stays loaded and fully
        replayable -- only the 'audio is running' state is cleared."""
        self._reading = False
        self.hotkey.unregister()
        self.tray.set_reading(False)
        self.tray.set_state("ready", f"{APP_NAME} — ready")
        self.panel.set_playing(False)

    # ------------------------------------------------------- select-to-read

    def _on_candidate(self, candidate) -> None:
        self._candidate = candidate
        log.debug(
            "selection candidate: source=%s text=%s process=%s",
            candidate.source, logmod.redact(candidate.text), candidate.process,
        )
        self.pill.show_for(candidate.rect, (candidate.x, candidate.y))

    def _on_pill_read(self) -> None:
        clip = self.cfg.section("clipboard")
        cap = capmod.capture_text(
            self._candidate,
            allow_clipboard=bool(clip.get("fallback_enabled", True)),
            restore_clipboard=bool(clip.get("restore", True)),
            blocklist=clip.get("blocklist", capmod.DEFAULT_CLIPBOARD_BLOCKLIST),
            copy_timeout_ms=int(clip.get("copy_timeout_ms", 400)),
        )
        if not cap.ok:
            self.pill.flash("Couldn't read that")
            if cap.error:
                log.info("capture failed: %s", cap.error)
            return
        self.pill.hide()
        self.read(cap.text)

    def _on_pill_copy(self) -> None:
        cap = capmod.copy_to_clipboard(self._candidate)
        self.pill.flash("Copied" if cap.ok else "Couldn't copy")
        if not cap.ok and cap.error:
            log.info("copy failed: %s", cap.error)

    def _on_hook_state(self, alive: bool) -> None:
        if not alive:
            log.warning("selection watcher hook is not active")
            self.tray.set_state(
                "warning", f"{APP_NAME} — select-to-read is not active"
            )

    # ------------------------------------------------------------ commands

    def _on_read_clipboard(self) -> None:
        self.read(QGuiApplication.clipboard().text() or "")

    def _on_show_panel(self) -> None:
        self.panel.show_floating()

    def _on_play_pause(self) -> None:
        if not self._has_document:
            return
        # play() restarts from the top when the document has finished, so this
        # works as a replay button too.
        if self.ctl.toggle():
            self._on_playback_started()

    def _on_stop(self) -> None:
        self.ctl.stop()
        self._end_playback()

    def _on_panic_stop(self) -> None:
        log.info("panic stop hotkey pressed")
        self._on_stop()

    def _on_sentence_clicked(self, index: int) -> None:
        if not self._has_document:
            return
        self.ctl.jump_to_sentence(index)
        self.ctl.play()
        self._on_playback_started()

    def _on_toggle_noise(self) -> None:
        """Silence or restore the background sound without touching settings."""
        self._noise_paused = not self._noise_paused
        self._apply_settings()
        log.debug("background sound %s", "paused" if self._noise_paused else "resumed")

    def _on_playback_started(self) -> None:
        if self._reading:
            return
        self._reading = True
        self.tray.set_reading(True)
        self.tray.set_state(
            "reading", f"{APP_NAME} — reading {self.ctl.total_sentences} sentences"
        )
        if self.cfg.get("app", "stop_hotkey_enabled"):
            self.hotkey.register(str(self.cfg.get("app", "stop_hotkey")))

    def _on_watcher_toggled(self, enabled: bool) -> None:
        if enabled and not self.watcher.isRunning():
            self.watcher = self._make_watcher()
            self.watcher.candidate.connect(self._on_candidate)
            self.watcher.hook_state_changed.connect(self._on_hook_state)
            self._register_own_windows()
            self.watcher.start()
        elif not enabled and self.watcher.isRunning():
            self.watcher.stop()
            self.pill.hide()

    def _on_open_settings(self) -> None:
        """Open the visual settings window.

        Settings are edited in-app rather than handed to an external editor:
        .toml has no registered handler on a default Windows install, and
        Windows 11's tabbed Notepad swallows the file when launched from a
        background process.
        """
        if self._settings_window is None:
            self._settings_window = SettingsWindow(engine=self.engine)
            self._settings_window.applied.connect(self._on_reload_settings)
            self._settings_window.open_raw_editor.connect(self._on_open_raw_settings)
        self._settings_window.show_focused()

    def _on_open_raw_settings(self) -> None:
        """The plain-text TOML editor, reached from Advanced."""
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog()
            self._settings_dialog.saved.connect(self._on_raw_settings_saved)
        self._settings_dialog.show_focused()

    def _on_raw_settings_saved(self) -> None:
        self._on_reload_settings()
        if self._settings_window is not None:
            # Keep the visual controls in step with a hand edit.
            self._settings_window.show_focused()

    def _on_open_settings_folder(self) -> None:
        paths.ensure_dirs()
        shell.reveal_in_explorer(paths.config_dir())

    def _apply_settings(self) -> None:
        """Push the current config onto the live objects.

        Everything reachable from the settings window takes effect here without
        a restart; only the audio device and thread count need one.
        """
        cfg = self.cfg
        self.ctl.player.volume = float(cfg.get("audio", "volume"))

        keep_alive = bool(cfg.get("audio", "keep_audio_alive"))
        # The panel button pauses the background sound for the session without
        # turning the feature off, so it survives a settings save.
        self.ctl.player.set_keepalive(
            keep_alive and not self._noise_paused,
            float(cfg.get("audio", "keep_alive_db")),
            str(cfg.get("audio", "keep_alive_color")),
        )
        self.panel.set_noise_control(keep_alive, not self._noise_paused)
        if keep_alive and self._ready:
            # Open the device now rather than at the first read, so the audio
            # path is already engaged before anything is spoken.
            try:
                self.ctl.player.ensure_ready()
            except Exception:
                log.debug("could not open the audio device early", exc_info=True)

        # Re-renders the loaded document if the voice or speed actually
        # changed, so the choice takes effect on what is on screen right now.
        voice = str(cfg.get("engine", "voice"))
        speed = float(cfg.get("engine", "speed"))
        self.engine.voice = voice
        self.engine.speed = speed
        if self.ctl.set_voice_and_speed(voice, speed) and self._has_document:
            self.panel.set_status("changing voice…")
        self.ctl.prev_restart_threshold_s = float(
            cfg.get("playback", "prev_restart_threshold_s")
        )
        self.ctl.scheduler.lookahead_sentences = int(
            cfg.get("playback", "lookahead_sentences")
        )
        self.splitter.trailing_pause_s = float(cfg.get("audio", "trailing_pause_s"))
        self.splitter.paragraph_pause_s = float(cfg.get("audio", "paragraph_pause_s"))
        self.splitter.max_sentences = int(cfg.get("ui", "max_sentences"))

        self.panel.set_typography(
            int(cfg.get("ui", "panel_font_pt")),
            float(cfg.get("ui", "panel_line_spacing")),
            str(cfg.get("ui", "panel_font_family") or ""),
        )

        self.pill.auto_hide_ms = int(cfg.get("selection", "pill_auto_hide_ms"))
        self.watcher.set_mode(cfg.get("selection", "mode"))
        self.watcher.enable_double_click = bool(
            cfg.get("selection", "enable_double_click")
        )
        self.watcher.enable_triple_click = bool(
            cfg.get("selection", "enable_triple_click")
        )
        watch_on = cfg.get("selection", "mode") != "off"
        self.tray.set_watcher_checked(watch_on)

        if not cfg.get("app", "stop_hotkey_enabled"):
            self.hotkey.unregister()

    def _on_reload_settings(self) -> None:
        """Re-read from disk and apply. Silent: the settings window saves on
        every change and showing a toast each time would be noise."""
        self.cfg = configmod.load()
        self._apply_settings()

    def _on_manual_reload(self) -> None:
        self._on_reload_settings()
        self.tray.showMessage(
            APP_NAME, "Settings reloaded.",
            QSystemTrayIcon.MessageIcon.Information, 1500,
        )
        log.info("settings reloaded from %s", paths.settings_file())

    # ---------------------------------------------------------------- tick

    def _tick(self) -> None:
        # Gated on having a document, not on playing: after a read finishes or
        # is stopped, the panel must still track next/back and show state.
        if not self._has_document:
            return
        state = self.ctl.tick()
        if state.sentence_changed:
            self.panel.set_sentence(state.sentence_index)
        self.panel.set_playing(state.playing)

        total = state.total_sentences
        if state.finished:
            status = f"finished — {total} sentences  ·  press play to replay"
        else:
            shown = min(state.sentence_index + 1, total)
            status = f"{shown} / {total}"
            if state.starved:
                status += "  ·  buffering"
            elif not state.playing:
                status += "  ·  paused"
        self.panel.set_status(status)

        if state.finished and self._reading:
            self._end_playback()

    def _reassert_topmost(self) -> None:
        if self.panel.isVisible():
            winwin.raise_topmost(self.panel)

    # ---------------------------------------------------------------- exit

    def quit(self) -> None:
        try:
            self._timer.stop()
            self._topmost.stop()
            self.hotkey.unregister()
            if self.watcher.isRunning():
                self.watcher.stop()
            self.ctl.shutdown()
        finally:
            self.pill.hide()
            self.tray.hide()
            singleton.release()
            self.app.quit()


def main(argv=None) -> int:
    argv = list(argv or sys.argv)

    # Must come first: under pythonw.exe there is no stdout/stderr, and kokoro's
    # import-time loguru setup raises on a None sink.
    logmod.ensure_std_streams()

    if not singleton.acquire():
        # Two instances would mean two mouse hooks, two pills, and two copies
        # of a 300MB model in memory.
        singleton.signal_existing_instance()
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 0

    cfg = configmod.load()
    logmod.setup(str(cfg.get("app", "log_level")), to_console=sys.stderr is not None)
    log.info("starting %s (settings: %s)", APP_NAME, paths.settings_file())

    # Both of these must happen before QApplication is constructed. PassThrough
    # stops a 150% monitor being rounded to 100%, which would put every
    # natively-positioned window slightly off.
    dpi.set_process_dpi_aware()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # closing the panel must not exit

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("no system tray available on this desktop")

    reader = ReaderApp(app, cfg)

    text_args = [a for a in argv[1:] if not a.startswith("-")]
    if text_args:
        reader.read(" ".join(text_args))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
