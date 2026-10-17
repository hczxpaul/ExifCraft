"""Tests for feature 3: BackupManager core — journal, recovery, orphan scan."""
import json
import os
import sys

import pytest

from conftest import ExifCraft


@pytest.fixture
def bm_factory(tmp_path):
    """BackupManager instances rooted in a per-test tmp dir."""
    made = []

    def make(sid=None):
        bm = ExifCraft.BackupManager(str(tmp_path), session_id=sid)
        made.append(bm)
        return bm

    yield make
    for bm in made:
        bm.cleanup()


class TestJournal:
    def test_begin_writes_manifest(self, bm_factory, jpeg_factory):
        bm = bm_factory()
        src = jpeg_factory("j.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        assert os.path.exists(bm.path)
        data = json.load(open(bm.path, encoding="utf-8"))
        assert data["records"][0]["src"] == str(src)
        assert data["records"][0]["state"] == "pending"
        assert data["records"][0]["mtime"] > 0

    def test_state_transitions(self, bm_factory, jpeg_factory):
        bm = bm_factory()
        src = jpeg_factory("s.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        bm.commit(rec)
        assert json.load(open(bm.path, encoding="utf-8"))["records"][0]["state"] == "done"
        bm.fail(rec)
        assert json.load(open(bm.path, encoding="utf-8"))["records"][0]["state"] == "failed"

    def test_copy_mode_has_no_backup(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("c.jpg")
        dst = tmp_path / "c_exif.jpg"
        rec = bm.begin(str(src), str(dst), "copy")
        assert rec.backup is None

    def test_backup_name_format(self, bm_factory, jpeg_factory):
        bm = bm_factory(sid="abcd1234")
        src = jpeg_factory("photo.jpg")
        name = os.path.basename(bm.backup_name(str(src)))
        assert name.startswith("._ExifCraft_bak_abcd1234_photo.jpg")
        assert name.endswith(".exifbak")

    def test_session_ids_differ(self, bm_factory):
        a, b = bm_factory(), bm_factory()
        assert a.sid != b.sid
        assert a.path != b.path

    def test_register_dirs_index(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("d.jpg")
        bm.begin(str(src), str(src), "overwrite")
        index = tmp_path / ".exifcraft" / "sessions" / "_dirs.json"
        dirs = json.load(open(index, encoding="utf-8"))
        assert str(tmp_path) in [os.path.abspath(d) for d in dirs]


class TestRecovery:
    def _seed(self, tmp_path, records):
        """Write a fake journal directly (simulates a crashed session)."""
        d = tmp_path / ".exifcraft" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "deadbeef.json").write_text(
            json.dumps({"session": "deadbeef", "records": records}), encoding="utf-8")
        return d

    def test_pending_with_missing_dst_restores(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("r.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        # simulate: rename done (src gone), save never happened. No cleanup()
        # here — that would delete the very backup the recovery needs.
        os.rename(str(src), rec.backup)
        self._seed(tmp_path, [rec.as_dict()])

        restored, orphans = ExifCraft.BackupManager.recover_sessions(str(tmp_path))
        assert restored == 1
        assert os.path.exists(str(src))             # original back in place
        assert not os.path.exists(rec.backup)

    def test_pending_with_existing_dst_keeps_disk(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("k.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        # simulate: save completed, commit missed — both files exist
        with open(rec.backup, "w") as fp:
            fp.write("backup-content")
        self._seed(tmp_path, [rec.as_dict()])

        restored, _ = ExifCraft.BackupManager.recover_sessions(str(tmp_path))
        assert restored == 0
        assert os.path.exists(str(src))             # disk wins, untouched
        assert os.path.exists(rec.backup)

    def test_journal_deleted_after_replay(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("x.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        bm.commit(rec)
        bm.cleanup()
        self._seed(tmp_path, [rec.as_dict()])

        ExifCraft.BackupManager.recover_sessions(str(tmp_path))
        sessions = tmp_path / ".exifcraft" / "sessions"
        assert not any(f.endswith(".json") and not f.startswith("_")
                       for f in os.listdir(sessions))

    def test_no_journals_zero_cost(self, tmp_path):
        assert ExifCraft.BackupManager.recover_sessions(str(tmp_path)) == (0, 0)

    def test_orphan_detection(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory(sid="orphan01")
        src = jpeg_factory("o.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")   # registers the dir
        with open(rec.backup, "w") as fp:                 # backup on disk…
            fp.write("backup")
        os.remove(bm.path)                                # …journal lost

        restored, orphans = ExifCraft.BackupManager.recover_sessions(str(tmp_path))
        assert restored == 0
        assert orphans == 1                         # unreferenced backup reported

    def test_dirs_index_survives_journal_cleanup(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("keep.jpg")
        bm.begin(str(src), str(src), "overwrite")
        bm.cleanup()
        index = tmp_path / ".exifcraft" / "sessions" / "_dirs.json"
        assert index.exists()                       # _ prefix: not treated as a journal


class TestSafeWrite:
    def _overwrite_rec(self, bm, src):
        return bm.begin(str(src), str(src), "overwrite")

    def test_overwrite_renames_and_writes(self, bm_factory, jpeg_factory):
        bm = bm_factory()
        src = jpeg_factory("w.jpg")
        original = src.read_bytes()
        rec = self._overwrite_rec(bm, src)

        bm.safe_write(rec, lambda dst: open(dst, "wb").write(b"NEW-CONTENT"))

        assert rec.backup and os.path.exists(rec.backup)
        assert open(rec.backup, "rb").read() == original   # old content preserved
        assert open(rec.dst, "rb").read() == b"NEW-CONTENT"
        bm.commit(rec)
        assert rec.state == "done"

    def test_copy_mode_writes_directly(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory()
        src = jpeg_factory("c.jpg")
        dst = tmp_path / "c_exif.jpg"
        rec = bm.begin(str(src), str(dst), "copy")

        bm.safe_write(rec, lambda d: open(d, "wb").write(b"copy-out"))

        assert rec.backup is None
        assert dst.read_bytes() == b"copy-out"
        assert src.read_bytes() != b"copy-out"              # original untouched

    def test_save_failure_marks_failed_and_raises(self, bm_factory, jpeg_factory):
        bm = bm_factory()
        src = jpeg_factory("f.jpg")
        rec = self._overwrite_rec(bm, src)

        def boom(_dst):
            raise RuntimeError("disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            bm.safe_write(rec, boom)
        assert rec.state == "failed"                        # journal knows

    def test_rename_failure_falls_back_to_copy(self, bm_factory, jpeg_factory,
                                               monkeypatch):
        import os as _os
        bm = bm_factory()
        src = jpeg_factory("slow.jpg")
        original = src.read_bytes()
        rec = self._overwrite_rec(bm, src)

        def no_rename(a, b):
            raise OSError("cross-device or locked")
        monkeypatch.setattr(_os, "rename", no_rename)

        bm.safe_write(rec, lambda dst: open(dst, "wb").write(b"NEW"))

        assert open(rec.backup, "rb").read() == original    # copy2 preserved it
        assert open(rec.dst, "rb").read() == b"NEW"

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only attribute")
    def test_backup_is_hidden_on_windows(self, bm_factory, jpeg_factory):
        import ctypes
        bm = bm_factory()
        src = jpeg_factory("h.jpg")
        rec = self._overwrite_rec(bm, src)
        bm.safe_write(rec, lambda dst: open(dst, "wb").write(b"x"))
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(rec.backup))
        assert attrs & 0x2                                  # FILE_ATTRIBUTE_HIDDEN


PARAMS = dict(
    longitude="114.05", latitude="22.54", altitude="12",
    focal_length="35", aperture="2.8", iso="200",
    make="NIKON", model="Z6 II", datetime="2024:05:06 07:08:09",
)


class TestWorkerIntegration:
    """The write worker must journal every file it touches."""

    def test_overwrite_batch_creates_backups(self, app, no_dialogs, jpeg_factory):
        paths = [str(jpeg_factory(f"b{i}.jpg", gps=True)) for i in range(5)]
        originals = [open(p, "rb").read() for p in paths]

        app._write_worker(paths, "", PARAMS | {"altitude": "12"}, overwrite=True)
        app.root.update()

        assert app.bm.backup_count() == 5
        assert all(r.state == "done" for r in app.bm.recs)
        for p, orig in zip(paths, originals):
            backup = app.bm.backup_name(p)
            assert open(backup, "rb").read() == orig      # original preserved
            assert open(p, "rb").read() != orig           # and replaced

    def test_copy_mode_makes_no_backup(self, app, no_dialogs, jpeg_factory, tmp_path):
        src = str(jpeg_factory("cm.jpg"))
        app._write_worker([src], str(tmp_path), PARAMS, overwrite=False)
        app.root.update()

        assert app.bm.backup_count() == 0
        assert (tmp_path / "cm_exif.jpg").exists()
        assert open(src, "rb").read() != b""              # source untouched

    def test_failure_does_not_stop_the_batch(self, app, no_dialogs, jpeg_factory):
        good = [str(jpeg_factory(f"g{i}.jpg")) for i in range(3)]
        broken = str(jpeg_factory("bad.jpg"))
        with open(broken, "wb") as fp:
            fp.write(b"not an image")
        app._write_worker([broken] + good, "", PARAMS, overwrite=True)
        app.root.update()

        done = [r for r in app.bm.recs if r.state == "done"]
        assert len(done) == 3                             # good ones still committed

    def test_backup_text_reported(self, app, no_dialogs, jpeg_factory):
        assert app._backup_text() == ""                   # nothing yet
        paths = [str(jpeg_factory(f"t{i}.jpg")) for i in range(2)]
        app._write_worker(paths, "", PARAMS, overwrite=True)
        app.root.update()
        assert "备份 2 个" in app._backup_text()

    def test_rollback_restores_exactly(self, app, no_dialogs, jpeg_factory):
        paths = [str(jpeg_factory(f"r{i}.jpg", gps=True)) for i in range(3)]
        originals = [open(p, "rb").read() for p in paths]
        app._write_worker(paths, "", PARAMS, overwrite=True)
        app.root.update()

        done, skipped = app.bm.rollback()
        assert (done, len(skipped)) == (3, 0)
        for p, orig in zip(paths, originals):
            assert open(p, "rb").read() == orig           # byte-exact restore
            assert not os.path.exists(app.bm.backup_name(p))

    def test_rollback_skips_externally_changed(self, app, no_dialogs, jpeg_factory):
        src = str(jpeg_factory("ext.jpg"))
        original = open(src, "rb").read()
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        # somebody else edits the file after our write
        with open(src, "wb") as fp:
            fp.write(b"foreign edit")

        done, skipped = app.bm.rollback()
        assert done == 0
        assert skipped == [src]
        assert open(src, "rb").read() == b"foreign edit"   # never overwritten
        assert os.path.exists(app.bm.backup_name(src))     # backup kept, not lost


class TestRollbackUI:
    def test_button_disabled_without_backups(self, app, no_dialogs):
        assert str(app.rollback_btn["state"]) == "disabled"

    def test_button_enabled_after_overwrite(self, app, no_dialogs, jpeg_factory):
        src = str(jpeg_factory("btn.jpg"))
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        assert str(app.rollback_btn["state"]) == "normal"

    def test_button_disabled_again_after_rollback(self, app, no_dialogs, jpeg_factory):
        src = str(jpeg_factory("btn2.jpg"))
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        app._rollback_clicked()                       # dialogs stubbed → confirmed
        assert str(app.rollback_btn["state"]) == "disabled"

    def test_rollback_click_restores_file(self, app, no_dialogs, jpeg_factory):
        src = str(jpeg_factory("btn3.jpg"))
        original = open(src, "rb").read()
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        app._rollback_clicked()
        assert open(src, "rb").read() == original


class TestExitFlow:
    """_on_close must offer rollback and always release backups."""

    @pytest.fixture(autouse=True)
    def _keep_root_alive(self, app, monkeypatch):
        """_on_close destroys the window; keep the shared Tk root usable."""
        monkeypatch.setattr(app.root, "destroy", lambda: None)

    def _prep(self, app, jpeg_factory):
        src = str(jpeg_factory("exit.jpg"))
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        return src

    def test_cancel_keeps_program_running(self, app, mod, monkeypatch,
                                          no_dialogs, jpeg_factory):
        src = self._prep(app, jpeg_factory)
        monkeypatch.setattr(mod.messagebox, "askyesnocancel", lambda *a, **k: None)
        app._on_close()
        assert app.bm.recs                            # nothing rolled back
        assert os.path.exists(app.bm.backup_name(src))  # backup still there

    def test_no_keeps_changes_and_cleans(self, app, mod, monkeypatch,
                                         no_dialogs, jpeg_factory):
        src = self._prep(app, jpeg_factory)
        original_after_write = open(src, "rb").read()
        monkeypatch.setattr(mod.messagebox, "askyesnocancel", lambda *a, **k: False)
        app._on_close()
        assert open(src, "rb").read() == original_after_write   # changes kept
        assert not os.path.exists(app.bm.backup_name(src))      # backups released

    def test_yes_rolls_back_and_cleans(self, app, mod, monkeypatch,
                                       no_dialogs, jpeg_factory):
        src = str(jpeg_factory("exit3.jpg"))
        original = open(src, "rb").read()
        app._write_worker([src], "", PARAMS, overwrite=True)
        app.root.update()
        monkeypatch.setattr(mod.messagebox, "askyesnocancel", lambda *a, **k: True)
        app._on_close()
        assert open(src, "rb").read() == original     # restored
        assert not os.path.exists(app.bm.backup_name(src))
        assert app.bm.recs == []

    def test_atexit_cleanup_releases_backups(self, app, no_dialogs, jpeg_factory):
        src = self._prep(app, jpeg_factory)
        app._atexit_cleanup()
        assert not os.path.exists(app.bm.backup_name(src))
        assert app.bm.recs == []


class TestPurgeOrphans:
    def test_purge_removes_unreferenced_backups(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory(sid="purge01")
        src = jpeg_factory("p.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        with open(rec.backup, "w") as fp:
            fp.write("orphan")
        os.remove(bm.path)                             # journal gone → orphan

        assert ExifCraft.BackupManager.recover_sessions(str(tmp_path))[1] == 1
        removed = ExifCraft.BackupManager.purge_orphans(str(tmp_path))
        assert removed == 1
        assert not os.path.exists(rec.backup)

    def test_purge_keeps_referenced_backups(self, bm_factory, jpeg_factory, tmp_path):
        bm = bm_factory(sid="keep01")
        src = jpeg_factory("keep.jpg")
        rec = bm.begin(str(src), str(src), "overwrite")
        with open(rec.backup, "w") as fp:
            fp.write("mine")
        assert ExifCraft.BackupManager.purge_orphans(str(tmp_path)) == 0
        assert os.path.exists(rec.backup)              # still referenced
