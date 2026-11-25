"""Menu-bar recorder: Start/Stop a meeting recording or a voice note (microphone only), then
transcribe it into inbox/.

Audio capture is done by the native audiorec tool (a subprocess); transcription runs in a
separate Python process so the Whisper model's memory is released when it finishes.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps

from app import config
from app.transcript import DATE

log = logging.getLogger("menubar")

IDLE, RECORDING, TRANSCRIBING = "🎙", "🔴", "⏳"
VOICE_NOTE = "🎤"
PERMISSION_HELP = {
    "microphone": "System Settings → Privacy & Security → Microphone → turn on AudioRec.",
    "system_audio": ("System Settings → Privacy & Security → Screen & System Audio Recording → "
                     "System Audio Recording Only → turn on AudioRec."),
}


def notify(title: str, message: str) -> None:
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                   check=False, capture_output=True)


def unfinished_recordings() -> list[Path]:
    if not config.RECORDINGS.exists():
        return []
    return sorted((p for p in config.RECORDINGS.iterdir() if p.is_dir() and any(p.glob("*.wav"))),
                  key=lambda p: p.stat().st_mtime)


class MeetingApp(rumps.App):
    def __init__(self):
        super().__init__("Meeting notes", title=IDLE, quit_button=None)
        self.cfg = config.load()
        self.audiorec = str(self.cfg["recorder"]["audiorec"])
        self.state = IDLE
        self.proc: subprocess.Popen | None = None
        self.folder: Path | None = None
        self.started = 0.0
        self.voice_note = False
        self.busy = 0  # transcriptions running
        self.quitting = False

        self.start_item = rumps.MenuItem("Start recording", callback=self.start)
        self.voice_item = rumps.MenuItem("Start voice note", callback=self.start_voice_note)
        self.stop_item = rumps.MenuItem("Stop recording", callback=None)
        self.recover_item = rumps.MenuItem("Recover last recording", callback=None)
        self.menu = [self.start_item, self.voice_item, self.stop_item, None, self.recover_item, None,
                     rumps.MenuItem("Open inbox", callback=lambda _: subprocess.run(["open", config.INBOX])),
                     rumps.MenuItem("Open logs", callback=lambda _: subprocess.run(["open", config.LOGS])),
                     None, rumps.MenuItem("Quit", callback=self.quit)]
        self.timer = rumps.Timer(self.tick, 1)
        self.timer.start()
        threading.Thread(target=self.check_permissions, daemon=True).start()

    # ------------------------------------------------------------- UI (main thread only)

    def tick(self, _):
        """Refresh the title and menu from the current state; background threads only set state."""
        if self.state == RECORDING:
            elapsed = int(time.time() - self.started)
            icon = VOICE_NOTE if self.voice_note else RECORDING
            self.title = f"{icon} {elapsed // 3600}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"
            if elapsed > self.cfg["recorder"]["max_hours"] * 3600:
                log.warning("maximum duration reached, stopping")
                notify("Meeting recorder", "Recording stopped automatically after the maximum duration.")
                self.stop(None)
        elif self.busy:
            self.title = f"{TRANSCRIBING} Transcribing…"
        else:
            self.title = IDLE
        recording = self.state == RECORDING
        self.start_item.set_callback(None if recording else self.start)
        self.voice_item.set_callback(None if recording else self.start_voice_note)
        self.stop_item.set_callback(self.stop if recording else None)
        recoverable = not recording and not self.busy and bool(unfinished_recordings())
        self.recover_item.set_callback(self.recover if recoverable else None)

    # ------------------------------------------------------------- recording

    def start_voice_note(self, _):
        self.start(None, voice_note=True)

    def start(self, _, voice_note: bool = False):
        if self.state == RECORDING:
            return
        stamp = datetime.now().strftime(f"{DATE} %H%M%S")
        self.folder = config.RECORDINGS / stamp
        cmd = [self.audiorec, "start", "--out", str(self.folder),
               "--max-seconds", str(int(self.cfg["recorder"]["max_hours"] * 3600) + 60)]
        if voice_note:
            cmd.append("--mic-only")
        elif self.cfg["recorder"].get("capture") == "all":
            cmd.append("--all-audio")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            log.error("cannot start audiorec: %s", e)
            notify("Meeting recorder", f"Cannot start audiorec: {e}. Run `make audiorec`.")
            return
        self.state, self.started, self.voice_note = RECORDING, time.time(), voice_note
        log.info("recording %s to %s", "voice note" if voice_note else "meeting", self.folder)
        threading.Thread(target=self.read_events, args=(self.proc, self.folder), daemon=True).start()
        threading.Thread(target=self.log_stderr, args=(self.proc,), daemon=True).start()

    def read_events(self, proc: subprocess.Popen, folder: Path):
        """Follow audiorec's JSON events; when it exits, hand the folder to the transcriber."""
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            log.info("audiorec: %s", event)
            if event.get("event") == "started":
                self.warn_about(event)
            elif event.get("event") == "error":
                notify("Meeting recorder", f"Recording problem: {event.get('message', '')}"[:200])
        code = proc.wait()
        log.info("audiorec exited with %s", code)
        if self.proc is proc:
            self.state = IDLE
            self.proc = None
        if any(folder.glob("*.wav")) and not self.quitting:
            self.transcribe(folder)

    def log_stderr(self, proc: subprocess.Popen):
        for line in proc.stderr:
            log.warning("audiorec stderr: %s", line.rstrip())

    def warn_about(self, event: dict):
        problems = []
        if event.get("me") != "recording":
            problems.append(f"Microphone not recorded ({event.get('me')}). {PERMISSION_HELP['microphone']}")
        others = str(event.get("others", ""))
        if others == "off":  # voice note
            pass
        elif others.startswith("error") or others == "none":
            problems.append(f"Call audio not recorded. {PERMISSION_HELP['system_audio']}")
        elif event.get("system_audio_permission") == "denied" and others.startswith("tap"):
            problems.append(f"Call audio will be silent. {PERMISSION_HELP['system_audio']}")
        for p in problems:
            log.warning(p)
            notify("Meeting recorder", p)

    def stop(self, _):
        proc = self.proc
        if proc and proc.poll() is None:
            log.info("stopping recording")
            proc.send_signal(signal.SIGINT)  # audiorec finalizes the WAV files
        # read_events() notices the exit and starts the transcription.

    # ------------------------------------------------------------- transcription

    def transcribe(self, folder: Path):
        def run():
            self.busy += 1
            try:
                log.info("transcribing %s", folder.name)
                result = subprocess.run([sys.executable, "-m", "app.transcribe_file", str(folder)],
                                        cwd=config.ROOT, capture_output=True, text=True)
                if result.returncode == 0:
                    out = result.stdout.strip().splitlines()[-1]
                    log.info("transcript ready: %s", out)
                    notify("Meeting recorder", f"Transcript ready: {Path(out).name}. Filing it now.")
                else:
                    log.error("transcription failed: %s", result.stderr[-2000:])
                    notify("Meeting recorder",
                           "Transcription failed; the audio was kept. Use 'Recover last recording' to retry.")
            finally:
                self.busy -= 1
        threading.Thread(target=run, daemon=True).start()

    def recover(self, _):
        folders = unfinished_recordings()
        if folders:
            notify("Meeting recorder", f"Recovering recording {folders[-1].name}…")
            self.transcribe(folders[-1])

    # ------------------------------------------------------------- permissions / quit

    def check_permissions(self):
        """On first launch, ask for permissions now rather than at the start of a meeting."""
        try:
            status = json.loads(subprocess.run([self.audiorec, "check"], capture_output=True,
                                               text=True, timeout=10).stdout)
            if "undetermined" in (status.get("microphone"), status.get("system_audio")):
                status = json.loads(subprocess.run([self.audiorec, "check", "--request"],
                                                   capture_output=True, text=True, timeout=300).stdout)
        except Exception as e:  # noqa: BLE001
            log.error("permission check failed: %s", e)
            notify("Meeting recorder", f"audiorec is missing or broken ({e}). Run `make audiorec`.")
            return
        log.info("permissions: %s", status)
        for key in ("microphone", "system_audio"):
            if status.get(key) == "denied":
                notify("Meeting recorder", f"Permission needed. {PERMISSION_HELP[key]}")

    def quit(self, _):
        self.quitting = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            notify("Meeting recorder", "Recording stopped. Use 'Recover last recording' after relaunching.")
        rumps.quit_application()


def main():
    config.LOGS.mkdir(exist_ok=True)
    config.RECORDINGS.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(config.LOGS / "menubar.log"), logging.StreamHandler()])
    os.chdir(config.ROOT)
    MeetingApp().run()


if __name__ == "__main__":
    main()
