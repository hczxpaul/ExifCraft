"""Map picker: single window, and closing must stop the tile threads.

tkintermapview starts ~13 worker threads per map. Two bugs used to combine:
the threads only stop when TkinterMapView.destroy() sets running=False, but
the window was closed with Toplevel.destroy() — which does not call that
override — so every open leaked a batch of threads that spun forever retrying
failed tile requests. Once enough had piled up the CPU was saturated and the
app stopped responding.
"""
import tkinter as tk
from tkinter import ttk

import pytest

pytest.importorskip("tkintermapview")

from tkintermapview import TkinterMapView


def _toplevels(root):
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def _map_widget(root):
    win = _toplevels(root)[0]
    return win, [w for w in win.winfo_children()
                 if isinstance(w, TkinterMapView)][0]


@pytest.fixture
def map_app(app):
    app._open_map()
    app.root.update()
    yield app
    for win in _toplevels(app.root):
        win.destroy()
    app.root.update()
    app._map_win = None


class TestMapWindow:
    def test_opens(self, map_app):
        assert len(_toplevels(map_app.root)) == 1

    def test_second_call_reuses_the_window(self, map_app):
        before = len(_toplevels(map_app.root))
        map_app._open_map()
        map_app.root.update()
        assert len(_toplevels(map_app.root)) == before      # no second window

    def test_close_stops_the_worker_threads(self, map_app):
        win, m = _map_widget(map_app.root)
        assert m.running is True
        # the Close button must go through the wrapped handler
        btn = [w for w in win.winfo_children() if isinstance(w, ttk.Button)][-1]
        btn.invoke()
        map_app.root.update()
        assert m.running is False                            # threads can exit
        assert _toplevels(map_app.root) == []
        assert map_app._map_win is None

    def test_reopens_after_close(self, map_app):
        win, _m = _map_widget(map_app.root)
        win.destroy()
        map_app.root.update()
        map_app._map_win = None
        map_app._open_map()
        map_app.root.update()
        assert len(_toplevels(map_app.root)) == 1

    def test_refused_while_writing(self, app, no_dialogs):
        class FakeWorker:
            def is_alive(self):
                return True
        app.worker = FakeWorker()
        app._open_map()
        app.root.update()
        assert _toplevels(app.root) == []
        app.worker = None
