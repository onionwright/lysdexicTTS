"""Headless harness for the streaming pipeline.

This is the Phase 1 proof: normalize -> split -> expand to playback units ->
synthesize the first unit -> start playing -> render the rest during playback,
with working sentence-level transport. No Qt, no Windows hooks. If this is
solid, the rest of the app is plumbing around it.

    python -m tools.stream_cli "Some text to read."
    python -m tools.stream_cli --file tools\\sample_article.txt --stats
    python -m tools.stream_cli --file tools\\sample_article.txt --script

Interactive keys: [space] play/pause  [n] next  [b] back  [s] stop  [q] quit
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.audio.player import StreamPlayer  # noqa: E402
from reader.core.cache import AudioCache  # noqa: E402
from reader.core.controller import ReaderController  # noqa: E402
from reader.text.splitter import SentenceSplitter  # noqa: E402
from reader.tts.kokoro_engine import KokoroEngine  # noqa: E402

try:
    import msvcrt
except ImportError:  # non-Windows
    msvcrt = None

POLL_S = 0.033  # matches the 33ms UI timer the Qt app will use


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Lysdexic TTS streaming harness")
    p.add_argument("text", nargs="?", help="text to read")
    p.add_argument("--file", "-f", help="read text from a file instead")
    p.add_argument("--voice", "-v", default="af_heart")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--device", default=None, help="output device name or index")
    p.add_argument("--blocksize", type=int, default=2048)
    p.add_argument("--volume", type=float, default=1.0)
    p.add_argument("--lookahead", type=int, default=3)
    p.add_argument("--stats", action="store_true", help="print a timing summary at exit")
    p.add_argument("--script", action="store_true",
                   help="run an automated transport exercise instead of reading through")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def load_text(args) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if args.text:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide text, --file, or pipe to stdin.")


def poll_key():
    # kbhit() touches the console buffer, which does not exist when this is run
    # non-interactively; failing here must not take the read down.
    try:
        if msvcrt is None or not msvcrt.kbhit():
            return None
        return msvcrt.getch().decode("ascii", "replace").lower()
    except Exception:
        return None


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    raw = load_text(args)

    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    engine = KokoroEngine(voice=args.voice, speed=args.speed)
    splitter = SentenceSplitter()

    t0 = time.perf_counter()
    engine.load()
    splitter.warm()
    load_s = time.perf_counter() - t0
    # The tray app keeps the model resident and warm, so a cold first inference
    # here would misrepresent the latency a real read will see.
    t0 = time.perf_counter()
    engine.warm()
    print(f"engine + splitter ready in {load_s:.2f}s, warmed in "
          f"{time.perf_counter() - t0:.2f}s "
          f"(espeak fallback: {engine.espeak_fallback_ok})")

    player = StreamPlayer(engine.sample_rate, blocksize=args.blocksize, device=device)
    player.volume = args.volume
    cache = AudioCache(max_seconds=300.0)
    ctl = ReaderController(
        engine,
        splitter=splitter,
        cache=cache,
        player=player,
        voice=args.voice,
        speed=args.speed,
        lookahead_sentences=args.lookahead,
    )
    ctl.start()

    t0 = time.perf_counter()
    n = ctl.read(raw)
    print(f"split {len(raw)} chars into {n} sentences "
          f"({len(ctl._units)} playback units) in "
          f"{(time.perf_counter() - t0) * 1000:.0f}ms"
          + ("  [TRUNCATED]" if ctl.truncated else ""))
    if not n:
        ctl.shutdown()
        print("nothing to read")
        return 1

    play_start = None
    script_step = 0
    interrupted = False

    print("\nkeys: [space] play/pause  [n] next  [b] back  [s] stop  [q] quit\n")

    try:
        while True:
            state = ctl.tick()

            if play_start is None and ctl.first_audio_latency is not None:
                play_start = time.perf_counter()
                print(f">>> time-to-first-audio: {ctl.first_audio_latency:.2f}s")

            if state.sentence_changed and 0 <= state.sentence_index < n:
                s = ctl.sentences[state.sentence_index]
                preview = s.text if len(s.text) <= 70 else s.text[:67] + "..."
                print(f"[{state.sentence_index + 1:>3}/{n}] {preview}")

            if player.callback_error:
                print(f"!!! callback error: {player.callback_error}")
                break

            if state.finished:
                print("\n--- finished ---")
                break

            if args.script and play_start is not None:
                script_step = run_script(
                    ctl, time.perf_counter() - play_start, script_step
                )
                if script_step < 0:
                    break

            key = poll_key()
            if key == "q":
                interrupted = True
                break
            elif key == " ":
                print("  [play]" if ctl.toggle() else "  [pause]")
            elif key == "n":
                ctl.next_sentence()
            elif key == "b":
                ctl.prev_sentence()
            elif key == "s":
                ctl.stop()
                print("  [stop]")

            time.sleep(POLL_S)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        ctl.shutdown()

    if args.stats:
        print_stats(ctl, n)
    return 130 if interrupted else 0


def run_script(ctl, elapsed, step):
    """Automated transport exercise, for checking behaviour without a human."""
    schedule = [
        (3.0, "pause", ctl.pause),
        (4.5, "resume", ctl.play),
        (7.0, "next sentence", ctl.next_sentence),
        (9.0, "next sentence", ctl.next_sentence),
        (12.0, "prev (should restart the current sentence)", ctl.prev_sentence),
        (14.0, "prev again (should also restart)", ctl.prev_sentence),
        (16.0, "stop", ctl.stop),
        (17.0, "done", None),
    ]
    if step >= len(schedule):
        return step
    when, label, action = schedule[step]
    if elapsed >= when:
        print(f"  ~~ script t={when:.1f}s: {label}")
        if action is None:
            return -1
        action()
        return step + 1
    return step


def print_stats(ctl, n_sentences):
    sched, cache, player = ctl.scheduler, ctl.cache, ctl.player
    print("\n================ stats ================")
    if ctl.first_audio_latency:
        print(f"time-to-first-audio : {ctl.first_audio_latency:.2f}s")
    print(f"sentences / units   : {n_sentences} / {len(ctl._units)}")
    print(f"synthesized         : {sched.synth_count}")
    if sched.synth_seconds > 0:
        print(f"synth wall time     : {sched.synth_seconds:.2f}s")
        print(f"audio produced      : {sched.audio_seconds:.2f}s")
        print(f"aggregate RTF       : "
              f"{sched.audio_seconds / sched.synth_seconds:.2f}x")
    print(f"cache               : {len(cache)} entries, "
          f"{cache.seconds:.1f}s, {cache.hits} hits / {cache.misses} misses")
    print(f"underruns (xruns)   : {player.xruns}")
    gap_s = player.starved_frames / float(player.sample_rate)
    print(f"pipeline stalls     : {player.starve_events} events, "
          f"{gap_s:.2f}s of silence total")
    print("=======================================")


if __name__ == "__main__":
    raise SystemExit(main())
