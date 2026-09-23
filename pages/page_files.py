# -*- coding: utf-8 -*-
"""页面 3：文件传输 & 预览"""
import os, re, sys, shlex, subprocess, threading
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QProgressBar, QMessageBox, QFileDialog,
    QScrollArea, QGridLayout,
)
from PyQt6.QtCore import Qt, QTimer
from core.config import (
    WIN, FILE_KINDS,
    CARD_W, CARD_GAP_X, CARD_GAP_Y, GRID_PADDING,
)
from core.widgets import FileCard
from core.utils import sh, sh_stream, adb_dev


class FilesPageMixin:

    def _page_files(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.pushBtn = QPushButton("📤  传入到手机")
        self.pushBtn.setObjectName("primary")
        self.pushBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pushBtn.setFixedHeight(40)
        self.pushBtn.setFixedWidth(140)
        self.pushBtn.clicked.connect(self.do_push)
        top.addWidget(self.pushBtn)

        self.pullBtn = QPushButton("📥  传出到电脑")
        self.pullBtn.setObjectName("primary")
        self.pullBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pullBtn.setFixedHeight(40)
        self.pullBtn.setFixedWidth(140)
        self.pullBtn.clicked.connect(self.do_pull)
        top.addWidget(self.pullBtn)

        self.refreshBtn = QPushButton("🔄  刷新目录")
        self.refreshBtn.setObjectName("secondary")
        self.refreshBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refreshBtn.setFixedHeight(40)
        self.refreshBtn.setFixedWidth(130)
        self.refreshBtn.clicked.connect(self.do_browse_phone)
        top.addWidget(self.refreshBtn)

        self.upBtn = QPushButton("⬆  返回上级")
        self.upBtn.setObjectName("secondary")
        self.upBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upBtn.setFixedHeight(40)
        self.upBtn.setFixedWidth(130)
        self.upBtn.clicked.connect(self.do_phone_up)
        top.addWidget(self.upBtn)

        self.delBtn2 = QPushButton("🗑  删除")
        self.delBtn2.setObjectName("secondary")
        self.delBtn2.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delBtn2.setFixedHeight(40)
        self.delBtn2.setFixedWidth(100)
        self.delBtn2.clicked.connect(self.do_delete_selected)
        top.addWidget(self.delBtn2)
        top.addStretch()
        v.addLayout(top)

        pathRow = QHBoxLayout()
        pathRow.setSpacing(8)
        self.phonePathE = QLineEdit("/sdcard/")
        self.phonePathE.setFixedHeight(36)
        self.phonePathE.returnPressed.connect(self.do_browse_phone)
        pathRow.addWidget(self.phonePathE, 1)
        self.goBtn = QPushButton("➡  前往")
        self.goBtn.setObjectName("secondary")
        self.goBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.goBtn.setFixedHeight(36)
        self.goBtn.setFixedWidth(90)
        self.goBtn.clicked.connect(self.do_browse_phone)
        pathRow.addWidget(self.goBtn)
        v.addLayout(pathRow)

        self.fileScroll = QScrollArea()
        self.fileScroll.setObjectName("fileScroll")
        self.fileScroll.setWidgetResizable(True)
        self.fileScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.fileGrid = QWidget()
        self.fileGrid.setObjectName("fileGrid")
        self.grid = QGridLayout(self.fileGrid)
        self.grid.setContentsMargins(GRID_PADDING, GRID_PADDING, GRID_PADDING, GRID_PADDING)
        self.grid.setHorizontalSpacing(CARD_GAP_X)
        self.grid.setVerticalSpacing(CARD_GAP_Y)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.fileScroll.setWidget(self.fileGrid)
        v.addWidget(self.fileScroll, 1)

        bot = QHBoxLayout()
        bot.setSpacing(10)
        self.fileStatus = QLabel("等待设备连接…")
        self.fileStatus.setObjectName("info")
        bot.addWidget(self.fileStatus, 1)
        self.progress2 = QProgressBar()
        self.progress2.setRange(0, 100)
        self.progress2.setValue(0)
        self.progress2.setFixedHeight(10)
        self.progress2.setTextVisible(False)
        self.progress2.setFixedWidth(200)
        bot.addWidget(self.progress2)
        v.addLayout(bot)

        return w

    def _set_prog2(self, val):
        try:
            if val < 0:
                self.progress2.setRange(0, 0)
            else:
                self.progress2.setRange(0, 100)
                self.progress2.setValue(max(0, min(100, int(val))))
        except Exception:
            pass

    def _require_adb(self, show_warning=True):
        ds, _ = adb_dev()
        if not ds:
            if show_warning:
                QMessageBox.warning(self, "无设备",
                                    "未检测到 ADB 设备，请连接手机并授权 USB 调试。")
            return None
        return ds[0]

    def _file_icon(self, name, is_dir):
        if is_dir:
            return FILE_KINDS["folder"]
        n = name.lower()
        if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
            return FILE_KINDS["image"]
        if n.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".3gp")):
            return FILE_KINDS["video"]
        if n.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")):
            return FILE_KINDS["audio"]
        if n.endswith(".apk"):
            return FILE_KINDS["apk"]
        if n.endswith((".txt", ".log", ".md", ".json", ".xml", ".ini", ".conf")):
            return FILE_KINDS["text"]
        if n.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
            return FILE_KINDS["zip"]
        if n.endswith(".img"):
            return FILE_KINDS["img"]
        return FILE_KINDS["file"]

    def _phone_path_of(self, name):
        base = self.phonePathE.text().strip()
        if not base.endswith("/"):
            base += "/"
        return base + name

    def do_browse_phone(self):
        dev = self._require_adb(show_warning=False)
        path = self.phonePathE.text().strip() or "/sdcard/"
        if not path.endswith("/"):
            path += "/"

        self._clear_grid()
        self._sel_name = None
        self._sel_is_dir = False

        if not dev:
            self.fileStatus.setText("⚠ 未检测到设备，请连接手机并授权 USB 调试")
            self.fileStatus.setStyleSheet(f"color: {self.theme['warn']}; background: transparent;")
            return

        self.lg(f"浏览目录：{path}", "info")
        self.fileStatus.setText(f"⏳ 正在读取 {path} …")
        self.fileStatus.setStyleSheet(f"color: {self.theme['dim']}; background: transparent;")

        def w():
            quoted = shlex.quote(path)
            rc, out = sh(["adb", "-s", dev, "shell", f"ls -1Ap {quoted}"], timeout=20)
            self.sig.log.emit(f"[1] ls -1Ap 返回码 = {rc}", "plain")
            if rc != 0:
                if out:
                    self.sig.log.emit(out, "plain")
                self.sig.files.emit(path, [], False)
                return

            rows = []
            for line in out.splitlines():
                n = line.strip().replace("\r", "")
                if not n:
                    continue
                if n.startswith("ls:") or n.startswith("adb:"):
                    continue
                is_dir = n.endswith("/")
                if is_dir:
                    n = n[:-1]
                if not n or n in (".", ".."):
                    continue
                rows.append([n, "0", "d" if is_dir else "-", is_dir])

            self.sig.log.emit(f"[2] 拿到 {len(rows)} 条", "plain")
            rows.sort(key=lambda x: (not x[3], x[0].lower()))
            self.sig.files.emit(path, rows, True)

        threading.Thread(target=w, daemon=True).start()

    def _calc_cols(self):
        try:
            vw = self.fileScroll.viewport().width()
        except Exception:
            vw = 0
        if vw <= 0:
            vw = 800
        usable = vw - GRID_PADDING * 2
        if usable < CARD_W:
            return 1
        n = (usable + CARD_GAP_X) // (CARD_W + CARD_GAP_X)
        return max(1, min(int(n), 20))

    def _relayout_files_grid(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.file_cards = []

        for c in range(self._cur_cols + 2):
            self.grid.setColumnStretch(c, 0)

        if not self._cur_ok or not self._cur_rows:
            return

        cols = self._calc_cols()
        self._cur_cols = cols

        for i, (name, size, perm, is_dir) in enumerate(self._cur_rows):
            symbol, color = self._file_icon(name, is_dir)
            info = "文件夹" if is_dir else ""
            card = FileCard(name, is_dir, symbol, color, info,
                            self._on_file_clicked, self._on_file_double,
                            parent=self.fileGrid)
            self.file_cards.append(card)
            r, c = divmod(i, cols)
            self.grid.addWidget(card, r, c)

        self.grid.setColumnStretch(cols, 1)

    def _on_files_ready(self, path, rows, ok):
        self._cur_path = path
        self._cur_ok = bool(ok)
        self._cur_rows = rows if ok else []
        self._sel_name = None
        self._sel_is_dir = False

        if not ok:
            self._clear_grid()
            self.fileStatus.setText(f"❌ 无法读取：{path}")
            self.fileStatus.setStyleSheet(f"color: {self.theme['err']}; background: transparent;")
            return
        if not rows:
            self._clear_grid()
            self.fileStatus.setText(f"（空目录）{path}")
            self.fileStatus.setStyleSheet(f"color: {self.theme['dim']}; background: transparent;")
            return

        self._relayout_files_grid()

        self.fileStatus.setText(f"✓ {path}    共 {len(rows)} 项")
        self.fileStatus.setStyleSheet(f"color: {self.theme['ok']}; background: transparent;")
        self.sig.log.emit(f"[3] ✓ 已显示 {len(rows)} 项（{self._cur_cols} 列）", "ok")

    def _clear_grid(self):
        self.file_cards = []
        self._cur_rows = []
        self._cur_ok = False
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def _on_file_clicked(self, card):
        for c in self.file_cards:
            c.set_selected(c is card)
        self._sel_name = card._name
        self._sel_is_dir = card._is_dir

    def _on_file_double(self, card):
        name, is_dir = card._name, card._is_dir
        if is_dir:
            base = self.phonePathE.text().strip()
            if not base.endswith("/"):
                base += "/"
            self.phonePathE.setText(base + name + "/")
            self.do_browse_phone()
        else:
            phone_path = self._phone_path_of(name)
            self._preview_file(phone_path, name)

    def do_phone_up(self):
        path = self.phonePathE.text().strip().rstrip("/")
        if not path:
            return
        idx = path.rfind("/")
        if idx <= 0:
            self.phonePathE.setText("/")
        else:
            self.phonePathE.setText(path[:idx] + "/")
        self.do_browse_phone()

    def _preview_file(self, phone_path, name):
        dev = self._require_adb()
        if not dev:
            return
        tmp_dir = Path(os.environ.get("TEMP", ".")) / "unlock_toolbox_preview"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        local = tmp_dir / name
        self.lg(f"预览：正在拉取 {phone_path} …", "info")

        def w():
            rc, out = sh(["adb", "-s", dev, "pull", phone_path, str(local)], timeout=120)
            if rc != 0:
                self.sig.log.emit(f"❌ 拉取失败（{rc}）", "error")
                if out:
                    self.sig.log.emit(out, "plain")
                return
            self.sig.log.emit(f"✓ 已拉取到：{local}", "ok")
            self._open_file(str(local))
        threading.Thread(target=w, daemon=True).start()

    def _open_file(self, path):
        try:
            if WIN:
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.sig.log.emit(f"✓ 已打开：{path}", "ok")
        except Exception as e:
            self.sig.log.emit(f"❌ 打开失败：{e}", "error")

    def do_push(self):
        dev = self._require_adb()
        if not dev:
            return
        f, _ = QFileDialog.getOpenFileName(self, "选择要上传的文件", "", "所有文件 (*)")
        if not f:
            return
        target_dir = self.phonePathE.text().strip() or "/sdcard/"
        if not target_dir.endswith("/"):
            target_dir += "/"
        name = os.path.basename(f)
        target = target_dir + name
        self.lg(f"推送：{f} → {target}", "info")
        self.sig.prog2.emit(0)

        def on_text(line):
            m = re.search(r"\[\s*(\d+)%\]", line)
            if m:
                self.sig.prog2.emit(int(m.group(1)))

        def w():
            rc, out = sh_stream(
                ["adb", "-s", dev, "push", "-p", f, target],
                on_text=on_text, timeout=1800)
            for line in (out or "").splitlines():
                if line.strip():
                    self.sig.log.emit(line, "plain")
            if rc == 0:
                self.sig.prog2.emit(100)
                self.sig.log.emit(f"✅ 上传成功：{target}", "ok")
                QTimer.singleShot(0, self.do_browse_phone)
            else:
                self.sig.prog2.emit(0)
                self.sig.log.emit(f"❌ 上传失败（{rc}）", "error")
            QTimer.singleShot(1200, lambda: self.sig.prog2.emit(0))

        threading.Thread(target=w, daemon=True).start()

    def do_pull(self):
        dev = self._require_adb()
        if not dev:
            return

        if self._sel_name:
            base = self.phonePathE.text().strip()
            if not base.endswith("/"):
                base += "/"
            phone_path = base + self._sel_name
            is_file = not self._sel_is_dir
            default_name = self._sel_name
        else:
            phone_path = self.phonePathE.text().strip()
            is_file = False
            default_name = ""

        if not phone_path:
            QMessageBox.warning(self, "未填路径", "请填写手机文件/目录路径，或先单击选中一个文件。")
            return

        if is_file:
            save_path, _ = QFileDialog.getSaveFileName(
                self, "保存到电脑", default_name, "所有文件 (*)")
            if not save_path:
                return
            local_target = save_path
        else:
            local_dir = QFileDialog.getExistingDirectory(self, "选择保存到电脑的目录")
            if not local_dir:
                return
            local_target = local_dir

        self.lg(f"拉取：{phone_path} → {local_target}", "info")
        self.sig.prog2.emit(-1)

        def w():
            rc, out = sh(["adb", "-s", dev, "pull", phone_path, local_target], timeout=1800)
            if out:
                for line in out.splitlines():
                    self.sig.log.emit(line, "plain")
            if rc == 0:
                self.sig.prog2.emit(100)
                self.sig.log.emit(f"✅ 下载完成：{local_target}", "ok")
            else:
                self.sig.prog2.emit(0)
                self.sig.log.emit(f"❌ 下载失败（{rc}）", "error")
            QTimer.singleShot(1200, lambda: self.sig.prog2.emit(0))

        threading.Thread(target=w, daemon=True).start()

    def do_delete_selected(self):
        name = self._sel_name
        if not name:
            QMessageBox.information(self, "未选择", "请先单击选中一个文件或文件夹。")
            return
        phone_path = self._phone_path_of(name)
        r = QMessageBox.question(
            self, "确认删除",
            f"确定要删除手机上的：\n{phone_path}\n\n此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        dev = self._require_adb()
        if not dev:
            return

        def w():
            rc, out = sh(["adb", "-s", dev, "shell", f"rm -rf {shlex.quote(phone_path)}"], timeout=30)
            if out:
                self.sig.log.emit(out, "plain")
            self.sig.log.emit("✓ 已删除" if rc == 0 else f"❌ 删除失败（{rc}）",
                              "ok" if rc == 0 else "error")
            self._sel_name = None
            QTimer.singleShot(0, self.do_browse_phone)
        threading.Thread(target=w, daemon=True).start()