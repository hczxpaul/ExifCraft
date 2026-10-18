"""Closing the window means "keep my work" — no dialog, no surprise.

Every write is already on disk when the worker finishes; the Rollback button
is the explicit way to undo. The old exit prompt asked "roll back everything?"
as its default answer, which made people undo work they meant to keep.
"""


class FakeBackupManager:
    def __init__(self):
        self.recs = [object()] * 3
        self.cleaned = False
        self.rolled_back = 0

    def cleanup(self):
        self.cleaned = True

    # the close path must not reach for these any more
    def rollback(self):
        self.rolled_back += 1
        return 0, []

    def backup_count(self):
        return 0


def test_close_never_asks(app, mod, monkeypatch):
    bm = FakeBackupManager()
    app.bm = bm
    asked = []
    for name in ("askyesnocancel", "askyesno", "showinfo", "showwarning"):
        monkeypatch.setattr(mod.messagebox, name,
                            lambda *a, **k: asked.append(name) or True)

    shutdown = []
    monkeypatch.setattr(app, "_shutdown", lambda: shutdown.append(1))

    app._on_close()

    assert asked == []                  # not a single dialog
    assert shutdown == [1]              # and it really exits


def test_close_releases_backups(app, monkeypatch):
    """Leaving the tmp files behind would fill the photo folder over time."""
    bm = FakeBackupManager()
    app.bm = bm
    monkeypatch.setattr(app.root, "destroy", lambda: None)

    app._on_close()

    assert bm.cleaned is True
    assert bm.rolled_back == 0


def test_close_does_not_roll_back(app, monkeypatch):
    bm = FakeBackupManager()
    app.bm = bm
    monkeypatch.setattr(app.root, "destroy", lambda: None)

    app._on_close()

    assert bm.rolled_back == 0          # the user's work survives
