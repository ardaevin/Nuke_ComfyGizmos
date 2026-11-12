"""ComfyGizmos installer window. install.bat / install.command start it with the Python inside Nuke (which has
Qt), so nobody needs their own Python. Run by hand: <Nuke python> installer/installer_gui.py"""
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_core as core  # noqa: E402

try:
    from PySide6 import QtCore, QtGui, QtWidgets                   # Nuke 16+
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets                   # Nuke 14 / 15

CHECKED, UNCHECKED = QtCore.Qt.Checked, QtCore.Qt.Unchecked
TIER_TEXT = {
    "full": "Full - the models as the workflows were built (for 32 GB cards, e.g. RTX 5090)",
    "medium": "Medium - smaller official quantisations of the big models (for about 24 GB cards, e.g. RTX 4090 / 3090)",
}


def gb(n):
    return "{:.1f} GB".format(n / 1e9) if n else "-"


class Worker(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(str, float, float)
    finished = QtCore.Signal(int)

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.cancel = False

    def run(self):
        j, errors = self.job, 0
        say = self.log.emit

        def step(what, fn, *args):
            nonlocal errors
            try:
                fn(*args)
            except (Exception, SystemExit) as e:                       # one failure doesn't stop the rest
                errors += 1
                say("FAILED - {}: {}".format(what, e))
                if not isinstance(e, (RuntimeError, SystemExit)):
                    say(traceback.format_exc(limit=2))

        for name in j["bundled"]:
            step(name, core.copy_bundled_pack, name, j["root"], say)
        for name in j["packs"]:
            if self.cancel:
                break
            step(name, core.install_pack, name, j["root"], j["python"], say)
        last = [0.0]

        def progress(name, done, total):
            if time.time() - last[0] > 0.25 or done >= total:
                last[0] = time.time()
                self.progress.emit(name, float(done), float(total))

        for name in j["downloads"]:
            if self.cancel:
                break
            step(name, core.download, name, j["root"], j["token"], say, progress, lambda: self.cancel)
        if j["nuke"] and not self.cancel:
            step("Nuke", core.install_nuke, j["nuke"], say)
        for mod in j["extras"]:
            if not self.cancel:
                step(mod.LABEL, core.install_extra, mod, say)
        if not self.cancel:
            step("settings", core.write_prefs, j["gizmos"], j["tier"], j["fp4"], say)
        self.finished.emit(errors)


class Installer(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ComfyGizmos installer")
        self.resize(980, 860)
        self.wfs = core.workflows()
        self.stats, self.folder_paths = core.server_info()
        self.prefs = core.read_prefs()
        self.have_files, self.have_packs = set(), set()
        self.worker = None
        self._build()
        self._set_root(core.find_comfyui(self.folder_paths))
        self._refresh()

    # ------------------------------------------------------------------ layout
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        box = QtWidgets.QGroupBox("ComfyUI")
        form = QtWidgets.QFormLayout(box)
        self.root_edit, root_row = self._path_row(folder=True)
        self.root_edit.editingFinished.connect(lambda: self._set_root(self.root_edit.text()))
        form.addRow("ComfyUI folder", root_row)
        self.py_edit, py_row = self._path_row(folder=False)
        form.addRow("ComfyUI's Python", py_row)
        self.comfy_label = QtWidgets.QLabel()
        self.comfy_label.setWordWrap(True)
        form.addRow("", self.comfy_label)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Model size")
        v = QtWidgets.QVBoxLayout(box)
        name, vram = core.gpu(self.stats)
        adv_tier, adv_fp4 = core.models.gpu_advice(name, vram)
        v.addWidget(QtWidgets.QLabel("Graphics card: {}{}".format(
            name or "not found", " ({:.0f} GB) - {} suggested".format(vram / 1024 ** 3, adv_tier.capitalize()) if vram else "")))
        tier, fp4 = core.models.settings(self.prefs) if "tier" in self.prefs else (adv_tier, adv_fp4)
        if "fp4" in self.prefs:
            fp4 = bool(self.prefs["fp4"])
        self.tier_buttons = {}
        for t in core.models.TIERS:
            b = QtWidgets.QRadioButton(TIER_TEXT[t])
            b.setChecked(t == tier)
            b.toggled.connect(self._refresh)
            v.addWidget(b)
            self.tier_buttons[t] = b
        self.fp4_box = QtWidgets.QCheckBox("Use NVFP4 models where there are some (RTX 50 series only; "
                                           "older cards get the int8 versions instead)")
        self.fp4_box.setChecked(fp4)
        self.fp4_box.toggled.connect(self._refresh)
        v.addWidget(self.fp4_box)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Gizmos - untick what you don't want; only the ticked ones appear in Nuke")
        v = QtWidgets.QVBoxLayout(box)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Gizmo", "To download", "Missing"])
        self.tree.header().setStretchLastSection(True)
        for fam, ids in core.families():
            parent = QtWidgets.QTreeWidgetItem(self.tree, [fam])
            parent.setFlags(parent.flags() | QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsAutoTristate)
            for wf_id in ids:
                new = core.models.is_new(self.prefs, wf_id)                # added since the last install
                item = QtWidgets.QTreeWidgetItem(parent, [self.wfs[wf_id][0] + ("   (new)" if new else "")])
                item.setData(0, QtCore.Qt.UserRole, wf_id)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(0, CHECKED if core.models.is_picked(self.prefs, wf_id) else UNCHECKED)
            parent.setExpanded(True)
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 100)
        self.tree.itemChanged.connect(lambda *a: self._refresh())
        v.addWidget(self.tree)
        self.total_label = QtWidgets.QLabel()
        self.total_label.setWordWrap(True)
        v.addWidget(self.total_label)
        lay.addWidget(box, 1)

        box = QtWidgets.QGroupBox("Install")
        form = QtWidgets.QFormLayout(box)
        self.packs_box = QtWidgets.QCheckBox("Install the missing node packs (git clone + their requirements)")
        self.packs_box.setChecked(True)
        form.addRow("", self.packs_box)
        self.dl_box = QtWidgets.QCheckBox("Download the missing models from Hugging Face")
        self.dl_box.setChecked(True)
        form.addRow("", self.dl_box)
        self.token = QtWidgets.QLineEdit()
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText("hf_... - only for gated models (LTX, Flux 3 Klein); not saved anywhere")
        self.token.setText(os.environ.get("HF_TOKEN", ""))
        form.addRow("Hugging Face token", self.token)
        self.nuke_edit, nuke_row = self._path_row(folder=True)
        self.nuke_edit.setText(os.path.join(core.NUKE_DIR, "python"))
        form.addRow("Nuke gizmos folder", nuke_row)
        self.extra_boxes = []                                          # add-ons for other apps, if this repo has any
        for mod in core.extras():
            box_ = QtWidgets.QCheckBox(mod.LABEL)
            form.addRow("", box_)
            self.extra_boxes.append((box_, mod))
        lay.addWidget(box)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(160)
        lay.addWidget(self.log_view)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.go = QtWidgets.QPushButton("Install")
        self.go.clicked.connect(self._install)
        self.stop = QtWidgets.QPushButton("Cancel")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._cancel)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        for b in (self.go, self.stop, close):
            row.addWidget(b)
        lay.addLayout(row)

    def _path_row(self, folder):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit()
        btn = QtWidgets.QPushButton("Browse...")

        def browse():
            if folder:
                path = QtWidgets.QFileDialog.getExistingDirectory(self, "Pick a folder", edit.text())
            else:
                path = QtWidgets.QFileDialog.getOpenFileName(self, "Pick ComfyUI's python", edit.text())[0]
            if path:
                edit.setText(os.path.normpath(path))
                edit.editingFinished.emit()
        btn.clicked.connect(browse)
        h.addWidget(edit, 1)
        h.addWidget(btn)
        return edit, w

    # ------------------------------------------------------------------ state
    def _set_root(self, root):
        root = os.path.normpath(root) if root else ""
        self.root_edit.setText(root)
        if not core.is_comfyui(root):
            self.comfy_label.setText("<b>Pick the ComfyUI folder</b> (the one with custom_nodes and models in it)."
                                     if root else "<b>ComfyUI not found - pick its folder.</b>")
            self.have_files, self.have_packs = set(), set()
            self._refresh()
            return
        if not self.py_edit.text():
            self.py_edit.setText(core.find_python(root))
        version = core.comfy_version(root, self.stats)
        ok = core.models.version_ok(version)
        self.comfy_label.setText("Version {} - {}. {}".format(
            version or "unknown",
            "ok" if ok else "<b>update ComfyUI to {} or newer</b>".format(core.models.catalog()["min_comfyui"]),
            "Running." if self.stats else "Not running (that's fine)."))
        self.have_files = core.present(root, self.folder_paths)
        self.have_packs = core.packs_present(root)
        self._refresh()

    def _tier(self):
        return next(t for t, b in self.tier_buttons.items() if b.isChecked())

    def _picked(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for k in range(parent.childCount()):
                item = parent.child(k)
                if item.checkState(0) == CHECKED:
                    out.append(item.data(0, QtCore.Qt.UserRole))
        return out

    def _plan(self, wf_ids):
        need = core.requirements(wf_ids, self._tier(), self.fp4_box.isChecked())
        packs = [p for p in need["packs"] if p not in self.have_packs]
        files = [f for f in need["models"] if f not in self.have_files]
        return need, packs, files

    def _refresh(self, *args):
        if not hasattr(self, "total_label"):
            return
        self.tree.blockSignals(True)
        cat = core.models.catalog()
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for k in range(parent.childCount()):
                item = parent.child(k)
                _, packs, files = self._plan([item.data(0, QtCore.Qt.UserRole)])
                size = sum(core.models.file_size(f) for f in files)
                missing = [p for p in packs if not cat["packs"][p].get("bundled")] + files
                item.setText(1, gb(size))
                item.setText(2, ", ".join(missing) if missing else "nothing - ready")
                item.setToolTip(2, "\n".join(missing))
        self.tree.blockSignals(False)
        picked = self._picked()
        _, packs, files = self._plan(picked)
        auto = [f for f in files if cat["models"].get(f, {}).get("auto")]
        manual = [f for f in files if f not in auto and not cat["models"].get(f, {}).get("url")]
        gated = [f for f in files if cat["models"].get(f, {}).get("gated")]
        size = sum(core.models.file_size(f) for f in files)
        text = ["{} gizmos ticked. Missing: {} node packs, {} model files, <b>{} to download</b> (shared files counted once)."
                .format(len(picked), len([p for p in packs if not cat["packs"][p].get("bundled")]),
                        len(files) - len(auto), gb(size))]
        if gated:
            text.append("{} of them are gated: accept their licence on Hugging Face and paste a token below.".format(len(gated)))
        if manual:
            text.append("No public download known for: {} - copy them into ComfyUI's models/loras yourself.".format(
                ", ".join(manual)))
        self.total_label.setText(" ".join(text))

    # ------------------------------------------------------------------ install
    def _say(self, text):
        self.log_view.appendPlainText(text)

    def _install(self):
        root = self.root_edit.text()
        if not core.is_comfyui(root):
            QtWidgets.QMessageBox.warning(self, "ComfyGizmos", "Pick the ComfyUI folder first.")
            return
        picked = self._picked()
        if not picked:
            QtWidgets.QMessageBox.warning(self, "ComfyGizmos", "Tick at least one gizmo.")
            return
        need, packs, files = self._plan(picked)
        cat = core.models.catalog()
        job = {
            "root": root, "python": self.py_edit.text(), "token": self.token.text().strip(),
            "bundled": [p for p in need["packs"] if cat["packs"][p].get("bundled")],
            "packs": [p for p in packs if not cat["packs"][p].get("bundled")] if self.packs_box.isChecked() else [],
            "downloads": [f for f in files if cat["models"].get(f, {}).get("url")] if self.dl_box.isChecked() else [],
            "nuke": self.nuke_edit.text(), "extras": [m for b, m in self.extra_boxes if b.isChecked()],
            "gizmos": picked, "tier": self._tier(), "fp4": self.fp4_box.isChecked(),
        }
        size = sum(core.models.file_size(f) for f in job["downloads"])
        msg = "Install {} gizmos{}{}?".format(
            len(picked), ", {} node packs".format(len(job["packs"])) if job["packs"] else "",
            ", and download {} of models".format(gb(size)) if size else "")
        if QtWidgets.QMessageBox.question(self, "ComfyGizmos", msg) != QtWidgets.QMessageBox.Yes:
            return
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.bar.setVisible(bool(job["downloads"]))
        self.worker = Worker(job)
        self.worker.log.connect(self._say)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._done)
        threading.Thread(target=self.worker.run, daemon=True).start()

    def _progress(self, name, done, total):
        self.bar.setMaximum(1000)
        self.bar.setValue(int(1000 * done / total) if total else 0)
        self.bar.setFormat("{}  {:.1f} / {:.1f} GB".format(name, done / 1e9, total / 1e9))

    def _cancel(self):
        if self.worker:
            self.worker.cancel = True
            self._say("Cancelling after the current step...")

    def _done(self, errors):
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        self.bar.setVisible(False)
        if errors:
            self._say("Finished with {} problem(s) - see above. Run the installer again to retry.".format(errors))
        else:
            self._say("Done. Restart ComfyUI and Nuke, then use Nodes > ComfyUI > Check setup.")
        self._set_root(self.root_edit.text())                          # re-scan what is there now


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = Installer()
    win.show()
    if __name__ == "__main__":
        sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
