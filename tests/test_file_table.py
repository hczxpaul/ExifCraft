"""Tests for the file table: columns, drag sweep, sorting, status, zero IO."""
import os

import pytest

from conftest import ExifCraft as ec

# conftest exposes the *module*; the app class lives inside it
HINT = ec.ExifCraft.HINT_IID


@pytest.fixture
def table_app(app):
    """The shared app, sized so the tree rows have real bbox geometry."""
    app.root.geometry("1100x800+50+50")
    app.root.update()
    return app


def _y(tv, iid: str) -> int:
    """Mid-row y coordinate (widget space) for a visible row."""
    tv.update_idletasks()
    box = tv.bbox(iid)
    if not box:
        pytest.skip(f"row {iid} has no bbox in this geometry")
    return box[1] + box[3] // 2


def _load(app, jpeg_factory, names):
    app.input_files = [str(jpeg_factory(n)) for n in names]
    app._refresh_list()
    app.root.update_idletasks()
    return app


class TestEmptyState:
    def test_hint_row_when_no_files(self, table_app):
        table_app.input_files = []
        table_app._refresh_list()
        assert table_app.file_list.get_children() == (HINT,)
        assert "File" in table_app.file_list.item(HINT, "values")[0]

    def test_hint_row_is_not_a_file(self, table_app, no_dialogs):
        table_app.input_files = []
        table_app._refresh_list()
        table_app.file_list.selection_set(HINT)
        assert table_app._selected_index() is None
        table_app._toggle_file_tick(0)          # out of range: no-op
        assert table_app.input_files == []

    def test_footer_says_nothing_selected(self, table_app):
        table_app.input_files = []
        table_app._refresh_list()
        assert table_app.list_count_lbl.cget("text") == "尚未选择文件"


class TestColumns:
    def test_row_carries_name_and_status(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["one.jpg"])
        values = table_app.file_list.item("0", "values")
        assert values[0] == "one.jpg"            # filename column
        assert values[1] == "就绪"                # default status

    def test_only_two_data_columns(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg"])
        assert table_app.file_list["columns"] == ("name", "status")

    def test_checkbox_owns_the_tree_column(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["pretty_name.jpg"])
        # the tree column carries the tick icon and no text
        assert table_app.file_list.item("0", "text") == ""
        assert table_app.file_list.item("0", "image")

    def test_checkbox_icon_follows_flag(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["z.jpg"])
        on = table_app.file_list.item("0", "image")
        table_app.file_checked[0] = False
        table_app._set_row_state(0)
        off = table_app.file_list.item("0", "image")
        assert str(on) != str(off)
        assert "off" in table_app.file_list.item("0", "tags")


class TestClickAndDrag:
    def test_click_toggles_single_row(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg", "b.jpg", "c.jpg"])
        tv = table_app.file_list
        y = _y(tv, "1")
        tv.event_generate("<ButtonPress-1>", x=12, y=y)
        tv.event_generate("<ButtonRelease-1>", x=12, y=y)
        table_app.root.update_idletasks()
        assert table_app.file_checked == [True, False, True]

    def test_drag_down_untick_block(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg", "b.jpg", "c.jpg"])
        tv = table_app.file_list
        y0, y2 = _y(tv, "0"), _y(tv, "2")
        tv.event_generate("<ButtonPress-1>", x=12, y=y0)
        for step in (1, 2):
            tv.event_generate("<B1-Motion>", x=12, y=_y(tv, str(step)))
        tv.event_generate("<ButtonRelease-1>", x=12, y=y2)
        table_app.root.update_idletasks()
        assert table_app.file_checked == [False, False, False]

    def test_drag_back_and_forth_stays_uniform(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg", "b.jpg", "c.jpg"])
        tv = table_app.file_list
        y0, y2 = _y(tv, "0"), _y(tv, "2")
        tv.event_generate("<ButtonPress-1>", x=12, y=y0)
        tv.event_generate("<B1-Motion>", x=12, y=y2)
        tv.event_generate("<B1-Motion>", x=12, y=y0)     # sweep back
        tv.event_generate("<ButtonRelease-1>", x=12, y=y0)
        table_app.root.update_idletasks()
        # uniform block — a toggle-based implementation would leave
        # [False, True, False] here instead
        assert table_app.file_checked == [False, False, False]

    def test_drag_up_from_bottom(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg", "b.jpg", "c.jpg"])
        tv = table_app.file_list
        y0, y2 = _y(tv, "0"), _y(tv, "2")
        tv.event_generate("<ButtonPress-1>", x=12, y=y2)
        tv.event_generate("<B1-Motion>", x=12, y=_y(tv, "1"))
        tv.event_generate("<B1-Motion>", x=12, y=y0)
        tv.event_generate("<ButtonRelease-1>", x=12, y=y0)
        table_app.root.update_idletasks()
        assert table_app.file_checked == [False, False, False]

    def test_space_toggles_selection(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg", "b.jpg"])
        table_app.file_list.selection_set("0")
        table_app._on_list_space()
        assert table_app.file_checked == [False, True]


class TestSorting:
    def test_name_asc_then_desc(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["c.jpg", "a.jpg", "b.jpg"])
        names = lambda: [os.path.basename(p) for p in table_app.input_files]
        table_app._sort_by_name()                   # first click: ascending
        assert names() == ["a.jpg", "b.jpg", "c.jpg"]
        table_app._sort_by_name()                   # second: descending
        assert names() == ["c.jpg", "b.jpg", "a.jpg"]
        table_app._sort_by_name()                   # third: import order
        assert names() == ["c.jpg", "a.jpg", "b.jpg"]

    def test_ticks_travel_with_their_file(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["c.jpg", "a.jpg", "b.jpg"])
        # c=on, a=off, b=on
        table_app.file_checked = [True, False, True]
        table_app._sort_by_name()                   # a, b, c
        names = [os.path.basename(p) for p in table_app.input_files]
        flags = dict(zip(names, table_app.file_checked))
        assert flags == {"a.jpg": False, "b.jpg": True, "c.jpg": True}
        assert table_app.checked_files() == [
            table_app.input_files[1], table_app.input_files[2]]

    def test_restore_import_order(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["c.jpg", "a.jpg", "b.jpg"])
        import_order = list(table_app.input_files)
        table_app._sort_by("name")
        table_app._sort_by(None)
        assert table_app.input_files == import_order

    def test_header_shows_direction(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["a.jpg"])
        table_app._sort_by_name()
        assert "▲" in table_app.file_list.heading("name", "text")
        table_app._sort_by_name()
        assert "▼" in table_app.file_list.heading("name", "text")

    def test_selection_follows_the_file(self, table_app, jpeg_factory):
        _load(table_app, jpeg_factory, ["c.jpg", "a.jpg", "b.jpg"])
        table_app.file_list.selection_set("0")       # c.jpg
        table_app._sort_by("name")
        idx = table_app._selected_index()
        assert table_app.input_files[idx].endswith("c.jpg")


class TestStatusColumn:
    def test_done_row_shows_written(self, table_app, jpeg_factory, mod):
        _load(table_app, jpeg_factory, ["done.jpg", "other.jpg"])
        src = table_app.input_files[0]
        table_app.bm.recs = [mod.Rec(src=src, dst=src, backup=None,
                                     mode="overwrite", state="done")]
        table_app._refresh_status_column()
        assert table_app.file_list.item("0", "values")[1] == "已写入"
        assert table_app.file_list.item("1", "values")[1] == "就绪"

    def test_failed_row_gets_failed_tag(self, table_app, jpeg_factory, mod):
        _load(table_app, jpeg_factory, ["bad.jpg"])
        src = table_app.input_files[0]
        table_app.bm.recs = [mod.Rec(src=src, dst=src, backup=None,
                                     mode="overwrite", state="failed")]
        table_app._refresh_status_column()
        assert table_app.file_list.item("0", "values")[1] == "失败"
        assert "failed" in table_app.file_list.item("0", "tags")

    def test_status_resets_after_rollback_clears_recs(self, table_app, jpeg_factory, mod):
        _load(table_app, jpeg_factory, ["x.jpg"])
        src = table_app.input_files[0]
        table_app.bm.recs = [mod.Rec(src=src, dst=src, backup=None,
                                     mode="overwrite", state="done")]
        table_app._refresh_status_column()
        assert table_app.file_list.item("0", "values")[1] == "已写入"
        table_app.bm.recs = []
        table_app._refresh_status_column()
        assert table_app.file_list.item("0", "values")[1] == "就绪"


class TestZeroFileIO:
    """The table must render from memory alone — this is the whole point of
    the design, so it gets a dedicated guard test."""

    def test_render_never_stats_or_opens(self, table_app, monkeypatch, tmp_path):
        # deliberately non-existent paths: rendering must not care
        table_app.input_files = [str(tmp_path / f"ghost{i}.jpg") for i in range(50)]

        def boom(*_a, **_k):
            raise AssertionError("file table must not touch the filesystem")

        monkeypatch.setattr(os, "stat", boom)
        monkeypatch.setattr(os.path, "getsize", boom)
        monkeypatch.setattr(os.path, "exists", boom)

        table_app._refresh_list()
        table_app._sort_by("name")
        table_app._sort_by("fmt")
        table_app._refresh_status_column()
        table_app._tick_all_files(False)
        table_app._tick_all_files(True)

        assert len(table_app.file_list.get_children()) == 50
        assert table_app.checked_files() == table_app.input_files


def _clear_flags(app):
    """All field flags off — the presets are defined relative to 'nothing on'."""
    for v in app.write_flags.values():
        v.set(False)
    app.date_flag.set(False)


class TestQuickGroups:
    def test_gps_preset_selects_only_gps(self, table_app):
        _clear_flags(table_app)
        table_app._quick_group("gps")
        flags = {n: v.get() for n, v in table_app.write_flags.items()}
        assert flags["longitude"] and flags["latitude"] and flags["altitude"]
        assert not flags["make"] and not flags["model"]
        assert not flags["iso"] and not flags["focal_length"]
        assert table_app.date_flag.get() is False

    def test_second_click_clears_the_same_group(self, table_app):
        _clear_flags(table_app)
        table_app._quick_group("gps")
        table_app._quick_group("gps")            # click again = undo
        assert not any(v.get() for v in table_app.write_flags.values())

    def test_camera_preset_is_exclusive(self, table_app):
        _clear_flags(table_app)
        table_app.write_flags["iso"].set(True)   # pre-existing selection
        table_app._quick_group("camera")
        flags = {n: v.get() for n, v in table_app.write_flags.items()}
        assert flags["make"] and flags["model"]
        assert not flags["iso"]                  # other groups are left off

    def test_date_preset_only_touches_date(self, table_app):
        _clear_flags(table_app)
        table_app._quick_group("date")
        assert table_app.date_flag.get() is True
        assert not any(v.get() for v in table_app.write_flags.values())
        table_app._quick_group("date")
        assert table_app.date_flag.get() is False

    def test_all_toggles_both_ways(self, table_app):
        _clear_flags(table_app)
        table_app._quick_group("all")
        assert all(v.get() for v in table_app.write_flags.values())
        assert table_app.date_flag.get() is True
        # the reported bug: a second click must undo, not be a no-op
        table_app._quick_group("all")
        assert not any(v.get() for v in table_app.write_flags.values())
        assert table_app.date_flag.get() is False

    def test_preset_drops_pending_delete_marks(self, table_app):
        _clear_flags(table_app)
        table_app.gps_clear.set(True)
        table_app.date_clear.set(True)
        table_app._quick_group("gps")
        assert table_app.gps_clear.get() is False
        assert table_app.date_clear.get() is False
