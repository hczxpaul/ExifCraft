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


def _button(win, text):
    """Find a button by label, wherever it sits in the window tree."""
    def walk(w):
        for child in w.winfo_children():
            if isinstance(child, ttk.Button) and str(child.cget("text")) == text:
                return child
            found = walk(child)
            if found is not None:
                return found
        return None
    return walk(win)


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

    def test_confirm_stops_the_worker_threads(self, map_app):
        win, m = _map_widget(map_app.root)
        assert m.running is True
        # the 确认 button must go through the wrapped handler
        _button(win, "确认").invoke()
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


class TestPickNeedsConfirmation:
    """A click previews; only 确认 writes the coordinates into the form."""

    def test_click_does_not_touch_the_form(self, map_app):
        win, m = _map_widget(map_app.root)
        map_app.field_vars["latitude"].set("")
        map_app.field_vars["longitude"].set("")
        m.map_click_callback((22.5, 114.0))
        assert map_app.field_vars["latitude"].get() == ""
        assert map_app.field_vars["longitude"].get() == ""

    def test_confirm_writes_and_closes(self, map_app):
        win, m = _map_widget(map_app.root)
        m.map_click_callback((22.5, 114.0))
        _button(win, "确认").invoke()
        map_app.root.update()
        assert map_app.field_vars["latitude"].get() == "22.500000"
        assert map_app.field_vars["longitude"].get() == "114.000000"
        assert _toplevels(map_app.root) == []

    def test_close_without_confirm_keeps_old_values(self, map_app):
        win, m = _map_widget(map_app.root)
        map_app.field_vars["latitude"].set("1.0")
        map_app.field_vars["longitude"].set("2.0")
        m.map_click_callback((22.5, 114.0))
        win.destroy()                      # the X button path
        map_app.root.update()
        map_app._map_win = None
        assert map_app.field_vars["latitude"].get() == "1.0"
        assert map_app.field_vars["longitude"].get() == "2.0"
