# -*- coding: utf-8 -*-
"""页面 3：文件传输 & 预览"""
import os, re, sys, shlex, subprocess, threading, time       
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
from core.widgets import FileCard, AnimatedProgressBar, set_state
from core.utils import (
    sh, sh_stream, adb_list, shq, ascii_work_dir, move_into_place,
    has_nonascii_dirs, adb_pull_tree,
)


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
        self._adb_mode = None       # 当前 adb 状态（device / recovery），由 _require_adb 刷新
        self.progress2 = AnimatedProgressBar()
        self.progress2.setFixedHeight(18)
        self.progress2.setFixedWidth(320)
        self.progress2.setFormat("空闲")
        bot.addWidget(self.progress2)
        v.addLayout(bot)

        return w

    def _set_prog2(self, val):
        """文件传输进度：-1 = 不确定进度 → 滚动动画"""
        try:
            if val is None or val < 0:
                self.progress2.set_busy(True, "传输中…")
            else:
                v = max(0, min(100, int(val)))
                self.progress2.set_smooth_value(v, f"{v}%")
        except Exception:
            pass

    def _require_adb(self, show_warning=True):
        """返回可用的 adb 序列号 —— 系统模式和 Recovery 模式都能读写文件

        （以前只认 adb 状态是 device，所以手机在 TWRP/Recovery 里整页都显示“未检测到设备”；
          Recovery 下 adb shell / pull / push 其实都能用）
        """
        mode = None
        serial = None
        try:
            rows, _ = adb_list()
        except Exception:
            rows = []
        for st in ("device", "recovery"):
            for sn, s in rows:
                if s == st:
                    mode, serial = st, sn
                    break
            if serial:
                break
        self._adb_mode = mode
        if serial:
            return serial

        if show_warning:
            other = rows[0][1] if rows else ""
            if other == "unauthorized":
                msg = ("设备已连接但还没授权：请在手机屏幕上点“允许 USB 调试”"
                       "（Recovery 下也会弹）。")
            elif other == "sideload":
                msg = ("设备在 Sideload 模式，这个模式没有文件服务。"
                       "请回到系统或 Recovery 模式后再用文件传输。")
            elif other == "offline":
                msg = "设备处于离线状态：重新插拔数据线，或在 CMD 里执行 adb kill-server 后重试。"
            else:
                msg = ("未检测到 ADB 设备（系统模式和 Recovery 模式都可以），"
                       "请连接手机并授权 USB 调试。")
            QMessageBox.warning(self, "无设备", msg)
        return None

    def _file_icon(self, name, is_dir):
        if is_dir:
            return FILE_KINDS["folder"]
        n = name.lower()
        if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
                       ".heic", ".svg", ".ico")):
            return FILE_KINDS["image"]
        if n.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".3gp",
                       ".flv", ".wmv", ".m4v")):
            return FILE_KINDS["video"]
        if n.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac",
                       ".opus", ".wma")):
            return FILE_KINDS["audio"]
        if n.endswith(".apk"):
            return FILE_KINDS["apk"]
        if n.endswith((".txt", ".log", ".md", ".json", ".xml", ".ini",
                       ".conf", ".yml", ".yaml", ".csv", ".html", ".htm")):
            return FILE_KINDS["text"]
        if n.endswith((".zip", ".rar", ".7z", ".tar", ".gz", ".xz",
                       ".bz2", ".zst")):
            return FILE_KINDS["zip"]
        if n.endswith((".img", ".iso", ".bin", ".raw")):
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
            self.fileStatus.setText("⚠ 未检测到设备，请连接手机并授权 USB 调试"
                                    "（系统 / Recovery 模式都能读文件）")
            set_state(self.fileStatus, "warn")
            return

        self.lg(f"浏览目录：{path}", "info")
        tip = "🛡 Recovery 模式 · " if getattr(self, "_adb_mode", None) == "recovery" else ""
        self.fileStatus.setText(f"{tip}⏳ 正在读取 {path} …")
        set_state(self.fileStatus, "dim")

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
            set_state(self.fileStatus, "err")
            return
        if not rows:
            self._clear_grid()
            self.fileStatus.setText(f"（空目录）{path}")
            set_state(self.fileStatus, "dim")
            return

        self._relayout_files_grid()

        self.fileStatus.setText(f"✓ {path}    共 {len(rows)} 项")
        set_state(self.fileStatus, "ok")
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
        # 中文/特殊名：adb 自己推导远端文件名会算成 '.'（推失败或把文件夹内容散落），
        # 所以先推成 ASCII 临时名，推完在设备端 mv 成真名
        ascii_name = name.isascii()
        push_to = target if ascii_name else (
            f"{target_dir}.tb_tmp_{int(time.time() * 1000) % 100000000}")
        self.lg(f"推送：{f} → {target}", "info")
        self.sig.prog2.emit(0)

        def on_text(line):
            # adb push -p 输出样式：
            #   [ 50%] /sdcard/xxx.bin
            #   45% /sdcard/xxx.bin
            if not line:
                return
            m = re.search(r"\[\s*(\d{1,3})\s*%\]", line)
            if not m:
                m = re.search(r"(?:^|\s)(\d{1,3})\s*%", line)
            if m:
                try:
                    v = int(m.group(1))
                    if 0 <= v <= 100:
                        self.sig.prog2.emit(v)
                except Exception:
                    pass

        def w():
            rc, out = sh_stream(
                ["adb", "-s", dev, "push", "-p", f, push_to],
                on_text=on_text, timeout=1800)
            if rc == 0 and not ascii_name:
                # 设备端改名成真名，并确认真的写进去了
                rc2, out2 = sh(["adb", "-s", dev, "shell",
                                f"mv {shq(push_to)} {shq(target)}"], timeout=30)
                if out2:
                    out = (out or "") + "\n" + out2
                rc = rc2
                rc3, chk = sh(["adb", "-s", dev, "shell",
                               f"test -e {shq(target)} && echo TB_OK"], timeout=15)
                if rc == 0 and not (rc3 == 0 and "TB_OK" in (chk or "")):
                    rc = 1
            elif rc == 0:
                rc2, chk = sh(["adb", "-s", dev, "shell",
                               f"test -e {shq(target)} && echo TB_OK"], timeout=15)
                if not (rc2 == 0 and "TB_OK" in (chk or "")):
                    rc = 1
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
            final = Path(save_path)
        else:
            local_dir = QFileDialog.getExistingDirectory(self, "选择保存到电脑的目录")
            if not local_dir:
                return
            rname = os.path.basename(phone_path.rstrip("/")) or "phone"
            final = Path(local_dir) / rname

        # adb 在本地路径含中文时会建不出文件（cannot create '…': Not a directory），
        # 所以先拉到「同盘、纯 ASCII」的临时名，再由本程序改名到最终位置
        work = ascii_work_dir(final)
        pull_to = str(work / f".tb_pull_{int(time.time() * 1000) % 100000000}")

        # 先取远端总大小（仅对文件；目录留空 → indeterminate）
        total_bytes = 0
        if is_file:
            rc, out = sh(
                ["adb", "-s", dev, "shell",
                 f"stat -c %s {shlex.quote(phone_path)}"],
                timeout=10)
            if rc == 0 and out.strip().isdigit():
                total_bytes = int(out.strip())

        self.lg(f"拉取：{phone_path} → {final}", "info")
        self.sig.prog2.emit(0 if total_bytes > 0 else -1)

        def w():
            stop_flag = threading.Event()

            def poll():
                last = -1
                target_path = Path(pull_to)
                while not stop_flag.is_set():
                    try:
                        if is_file:
                            size = target_path.stat().st_size if target_path.is_file() else 0
                        else:
                            size = sum(
                                f.stat().st_size
                                for f in target_path.rglob("*")
                                if f.is_file())
                    except Exception:
                        size = 0
                    if total_bytes > 0:
                        pct = min(99, int(size * 100 / total_bytes))
                        if pct != last:
                            self.sig.prog2.emit(pct)
                            last = pct
                    time.sleep(0.15)

            # 远端树里有非 ASCII 目录名？→ adb 会中途失败（还留下半成品），直接走混合通道
            need_tree = (not is_file) and has_nonascii_dirs(dev, phone_path) is True
            if need_tree:
                self.sig.log.emit("手机上有非 ASCII 目录名（adb 建不出同名本地目录），"
                                  "按子树导出：ASCII 子树一次拉、含中文目录名的子树逐文件…",
                                  "info")
            elif total_bytes > 0:
                threading.Thread(target=poll, daemon=True).start()

            if need_tree:
                rc2, okc, failc = adb_pull_tree(
                    dev, phone_path, final, str(work),
                    on_progress=lambda p: self.sig.prog2.emit(p),
                    log=lambda s: self.sig.log.emit(s, "plain"))
                stop_flag.set()
                if rc2 in (0, 1) and (okc or failc):
                    self.sig.prog2.emit(100)
                    self.sig.log.emit(
                        f"✅ 下载完成：{final}（成功 {okc} 项"
                        + (f"，失败 {failc} 项" if failc else "") + "）",
                        "ok" if failc == 0 else "warn")
                elif rc2 == 0:
                    self.sig.prog2.emit(100)
                    self.sig.log.emit(f"（空目录）{final}", "dim")
                else:
                    self.sig.prog2.emit(0)
                    self.sig.log.emit("❌ 下载失败（无可取文件）", "error")
                QTimer.singleShot(1200, lambda: self.sig.prog2.emit(0))
                return

            rc, out = sh(
                ["adb", "-s", dev, "pull", phone_path, pull_to],
                timeout=1800)
            stop_flag.set()
            if out:
                for line in out.splitlines():
                    self.sig.log.emit(line, "plain")
            moved = False
            if rc == 0 or os.path.isdir(pull_to):
                moved = move_into_place(pull_to, final)
            if rc == 0 and moved:
                self.lg(f"  ↻ 已落地：{final}", "plain")
                self.sig.prog2.emit(100)
                self.sig.log.emit(f"✅ 下载完成：{final}", "ok")
            elif rc == 0:
                self.sig.prog2.emit(100)
                self.sig.log.emit(f"✅ 下载完成（未改名）：{pull_to}", "ok")
            else:
                if "cannot create" in (out or "").lower():
                    self.sig.log.emit(
                        "提示：电脑上的保存路径尽量别用中文/特殊字符，"
                        "程序已用临时目录中转，若仍失败请换英文目录", "warn")
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