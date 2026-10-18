"""Layout tests: three resizable columns, preview split, path field."""
import pytest


@pytest.fixture
def wide_app(app):
    """The shared app on a wide window with the sashes placed."""
    app.root.geometry("1400x900+40+40")
    app.root.update()
    app._setup_column_widths()
    app.root.update()
    return app


def _widths(app):
    paned = app._main_paned
    return [paned.nametowidget(p).winfo_width() for p in paned.panes()]


class TestThreeColumns:
    def test_three_panes(self, wide_app):
        assert len(wide_app._main_paned.panes()) == 3

    def test_file_column_is_the_narrowest(self, wide_app):
        w = _widths(wide_app)
        assert w[0] < w[1] and w[0] < w[2], w

    def test_preview_has_its_own_split(self, wide_app):
        assert len(wide_app._prev_split.panes()) == 2

    def test_log_lives_in_the_middle_column(self, wide_app):
        log_pane = wide_app.log.master.master          # log -> frame -> pane
        panes = [wide_app._mid_paned.nametowidget(p)
                 for p in wide_app._mid_paned.panes()]
        assert any(str(log_pane) == str(p) for p in panes)

    def test_horizontal_sash_moves_the_file_column(self, wide_app):
        paned = wide_app._main_paned
        before = _widths(wide_app)[0]
        paned.sashpos(0, before + 60)
        wide_app.root.update()
        assert _widths(wide_app)[0] > before + 30

    def test_middle_sash_moves_the_log(self, wide_app):
        mid = wide_app._mid_paned
        height_before = mid.nametowidget(mid.panes()[1]).winfo_height()
        total = mid.winfo_height()
        mid.sashpos(0, int(total * 0.55))
        wide_app.root.update()
        assert mid.nametowidget(mid.panes()[1]).winfo_height() > height_before


class TestPathField:
    LONG = (r"D:\HD4300PLUS-916C\personal_folder\Photos\MobileBackup"
            r"\HONOR BVL-AN00 已修复\LivePhoto_20260502_225341_674.JPG")

    def test_long_path_scrolls_to_the_tail(self, wide_app):
        wide_app.src_var.set(self.LONG)
        wide_app.root.update_idletasks()
        wide_app.root.update()
        first, last = wide_app._src_entry.xview()
        assert first > 0.05, (first, last)
        assert last > 0.98, (first, last)

    def test_short_path_fits(self, wide_app):
        wide_app.src_var.set(r"D:\a.jpg")
        wide_app.root.update_idletasks()
        wide_app.root.update()
        first, last = wide_app._src_entry.xview()
        assert first < 0.01 and last > 0.99

    def test_overflow_shows_the_scrollbar(self, wide_app):
        holder = wide_app._src_entry.master
        wide_app.src_var.set(self.LONG)
        wide_app.root.update()
        assert len(holder.grid_slaves(row=1)) == 1
        wide_app.src_var.set(r"D:\a.jpg")
        wide_app.root.update()
        assert len(holder.grid_slaves(row=1)) == 0

    def test_output_field_behaves_the_same(self, wide_app):
        wide_app.out_var.set(self.LONG)
        wide_app.root.update_idletasks()
        wide_app.root.update()
        first, last = wide_app._out_entry.xview()
        assert first > 0.05 and last > 0.98


class TestPreviewFollowsItsPane:
    def test_budget_tracks_the_box(self, wide_app):
        wide_app.root.update_idletasks()
        box = wide_app._preview_box
        w, h = box.winfo_width(), box.winfo_height()
        assert wide_app._preview_budget() == (max(wide_app._px(120), w),
                                              max(wide_app._px(90), h))

    def test_refit_skips_when_size_is_unchanged(self, wide_app, monkeypatch):
        wide_app.PREVIEW_MAX = wide_app._preview_budget()
        called = []
        monkeypatch.setattr(wide_app, "_resize_preview_budget",
                            lambda: called.append(1))
        wide_app._refit_preview()
        assert called == []          # same size -> no decode
