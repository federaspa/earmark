import json
from types import SimpleNamespace

from app import menubar


class FakeProc:
    def __init__(self):
        self.stdout = iter([json.dumps({"event": "stopped"}) + "\n"])

    def wait(self):
        return 0


def finished(folder, discarded):
    """Run MeetingApp.read_events for an audiorec that has just exited."""
    proc, transcribed = FakeProc(), []
    app = SimpleNamespace(proc=proc, state=menubar.RECORDING, quitting=False,
                          discarded={folder} if discarded else set(),
                          warn_about=lambda e: None, transcribe=transcribed.append)
    menubar.MeetingApp.read_events(app, proc, folder)
    return app, transcribed


def test_stopped_recording_is_transcribed(tmp_path, monkeypatch):
    (tmp_path / "me.wav").write_bytes(b"x")
    app, transcribed = finished(tmp_path, discarded=False)
    assert transcribed == [tmp_path] and app.state == menubar.IDLE and tmp_path.exists()


def test_discarded_recording_is_deleted_not_transcribed(tmp_path, monkeypatch):
    monkeypatch.setattr(menubar, "notify", lambda *a: None)
    folder = tmp_path / "rec"
    folder.mkdir()
    (folder / "me.wav").write_bytes(b"x")
    app, transcribed = finished(folder, discarded=True)
    assert transcribed == [] and not folder.exists() and app.discarded == set()
