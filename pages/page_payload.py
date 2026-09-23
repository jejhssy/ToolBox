# -*- coding: utf-8 -*-
"""页面 5：Payload / 本地 OTA 提取（调用外部 payload-dumper-go.exe）

流程：
    ① 选择本地 OTA ZIP / payload.bin
  ② 点「🔍 读取分区表」→ 列表出现分区
  ③ 勾选 → 点「📤 提取选中」
  ④ 任何时刻点「⏹ 停止」立即中断

后端工具（自动查找）：
    · payload-dumper-go.exe   ← 推荐（支持本地 ZIP / payload.bin）

查找顺序：PATH → 项目根目录 → tools/ 子目录
"""
import re, shutil, subprocess, threading, time
import zipfile
from pathlib import Path

try:
    import py7zr
except ImportError:
    py7zr = None

from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QProgressBar, QMessageBox, QFileDialog,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QStyledItemDelegate, QStyleOptionViewItem,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPen, QBrush
from core.config import WIN, OUT_DIR


PROG_RE = re.compile(r"(\d+)\s*%")
NAME_OK = re.compile(r"^[A-Za-z0-9_\-]+$")
_ACTIVE_PROCESSES = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()


class PayloadItemDelegate(QStyledItemDelegate):
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        if checked:
            option.backgroundBrush = QBrush(QColor(self.theme["acc"]).darker(150))
        super().paint(painter, option, index)
        color = QColor(self.theme["acc"]) if checked else QColor(self.theme["line"])
        pen = QPen(color, 2 if checked else 1)
        painter.save()
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(option.rect.adjusted(2, 2, -3, -3), 6, 6)
        painter.restore()


# ===================== 外部工具调用 =====================
def _run_cli(cmd, cancel_event=None, on_text=None):
    """运行外部命令，支持取消。返回 (returncode, text)。
       cancel_event 触发 → kill 子进程，返回 -100。"""
    kw = {}
    if WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    except FileNotFoundError:
        return -1, f"未找到 {cmd[0]}"
    except Exception as e:
        return -3, f"启动失败：{e}"
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(p)

    def terminate():
        if WIN and p.pid:
            subprocess.run(
                ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                p.kill()
            except Exception:
                pass

    # 看门狗：cancel 触发时即使线程卡在 read() 里也能杀进程
    if cancel_event is not None:
        def _watch():
            while p.poll() is None:
                if cancel_event.is_set():
                    terminate()
                    return
                time.sleep(0.15)
        threading.Thread(target=_watch, daemon=True).start()

    output = bytearray()

    def collect_output():
        try:
            for line in iter(p.stdout.readline, b""):
                output.extend(line)
                if on_text:
                    on_text(line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except Exception:
            pass

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    while p.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            terminate()
            break
        time.sleep(0.05)
    try:
        p.wait(timeout=5)
    except Exception:
        terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            pass
    reader.join(timeout=5)

    all_bytes = bytes(output)
    if reader.is_alive():
        terminate()
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.discard(p)

    if cancel_event is not None and cancel_event.is_set():
        return -100, all_bytes.decode("utf-8", errors="replace")
    rc = p.returncode if p.returncode is not None else 0
    return rc, all_bytes.decode("utf-8", errors="replace")


def _find_tool():
    """返回本地 payload-dumper-go 路径。"""
    names = [
        ("payload-dumper-go.exe", "pdg"),
        ("payload-dumper-go", "pdg"),
    ]
    # 1. PATH
    for n, k in names:
        p = shutil.which(n)
        if p:
            return p, k
    # 2. 项目根 / tools
    here = Path(__file__).resolve().parent.parent   # 项目根
    for d in (here, here / "tools", Path.cwd(), Path.cwd() / "tools"):
        for n, k in names:
            f = d / n
            if f.exists():
                return str(f), k
    return None, None


def _find_xz():
    found = shutil.which("xz") or shutil.which("xz.exe")
    if found:
        return found
    here = Path(__file__).resolve().parent.parent
    for directory in (here, here / "tools"):
        for name in ("xz.exe", "xz"):
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _validate_local_source(path):
    """验证本地文件可读取；ZIP 是否包含 payload.bin 由读取阶段分流。"""
    source = Path(path)
    if not source.is_file():
        return "文件不存在"
    try:
        if source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source) as package:
                package.namelist()
        elif source.suffix.lower() == ".7z":
            if py7zr is None:
                return "读取 7z 需要安装 py7zr：pip install py7zr"
            with py7zr.SevenZipFile(source, mode="r") as package:
                package.getnames()
        elif source.suffix.lower() == ".bin":
            with source.open("rb") as stream:
                if stream.read(4) != b"CrAU":
                    return "BIN 文件不是有效的 payload.bin"
        return None
    except zipfile.BadZipFile:
        return "ZIP 文件损坏或格式无效"
    except OSError as exc:
        return f"无法读取文件：{exc}"


def _zip_entries(path):
    """返回普通 ZIP 内的文件条目；目录不加入列表。"""
    entries = []
    with zipfile.ZipFile(path) as package:
        for item in package.infolist():
            if item.is_dir():
                continue
            entries.append((item.filename, f"{item.file_size} bytes"))
    return entries


def _archive_entries(path):
    source = Path(path)
    if source.suffix.lower() == ".7z":
        if py7zr is None:
            raise RuntimeError("读取 7z 需要安装 py7zr")
        with py7zr.SevenZipFile(source, mode="r") as package:
            return [(name, "") for name in package.getnames()]
    return _zip_entries(source)


def _archive_contains_payload(path):
    source = Path(path)
    if source.suffix.lower() == ".7z":
        if py7zr is None:
            return False
        with py7zr.SevenZipFile(source, mode="r") as package:
            names = package.getnames()
    else:
        with zipfile.ZipFile(source) as package:
            names = package.namelist()
    return any(str(name).replace("\\", "/").rstrip("/").split("/")[-1].lower()
               == "payload.bin" for name in names)


def _parse_list_output(text):
    """解析 payload-dumper-go 的普通和机器可读分区列表输出。"""
    parts = []
    seen = set()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # payload-dumper-go -m -l: name:size_in_KB
        machine = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(\d+)\s*$", s)
        if machine:
            entries = [(machine.group(1), f"{machine.group(2)} KB")]
        else:
            # Human output: name (size), possibly several entries on one line.
            entries = [
                (m.group(1), m.group(2))
                for m in re.finditer(
                    r"([A-Za-z0-9_\-]+)\s*\(([^)]+)\)", s)]
            if not entries and "\t" in s:
                cols = [c.strip() for c in s.split("\t") if c.strip()]
                if len(cols) >= 2:
                    entries = [(cols[0], cols[1])]

        for name, size_r in entries:
            if name in seen:
                continue
            seen.add(name)
            parts.append((name, size_r))
    return parts


def _is_payload_like_source(value):
    """判断本地路径是否具有 payload 文件扩展名。"""
    if value is None:
        return False
    s = str(value).strip().lower()
    if not s:
        return False
    if s.startswith(("http://", "https://")):
        return False

    blocked = (".tar.gz", ".tgz", ".tar", ".exe", ".msi")
    if any(s.endswith(ext) for ext in blocked):
        return False

    ok_suffixes = (".zip", ".7z", ".bin", ".img", ".payload")
    if any(s.endswith(ext) for ext in ok_suffixes):
        return True

    return False


def _tool_log_name(tool_path):
    return "[pdg]"


# ===================== 页面 =====================
class PayloadPageMixin:

    def _page_payload(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        t = QLabel("📦  Payload / 链接提取")
        t.setObjectName("cardTitle")
        v.addWidget(t)
        d = QLabel("① 选择本地卡刷包或 payload 文件  ② 读取分区表  ③ 勾选分区后提取")
        d.setObjectName("desc")
        v.addWidget(d)

        # ---- 源输入 ----
        srcRow = QHBoxLayout()
        srcRow.setSpacing(8)
        self.payloadUrlE = QLineEdit()
        self.payloadUrlE.setPlaceholderText("输入本地 payload.bin 或 OTA ZIP 路径…")
        self.payloadUrlE.setFixedHeight(40)
        self.payloadUrlE.returnPressed.connect(self.do_payload_read)
        srcRow.addWidget(self.payloadUrlE, 1)

        self.payloadLocalBtn = QPushButton("📂  本地文件")
        self.payloadLocalBtn.setObjectName("secondary")
        self.payloadLocalBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.payloadLocalBtn.setFixedHeight(44)
        self.payloadLocalBtn.setFixedWidth(150)
        self.payloadLocalBtn.clicked.connect(self.do_payload_pick_local)
        srcRow.addWidget(self.payloadLocalBtn)

        self.payloadReadBtn = QPushButton("🔍  读取分区表")
        self.payloadReadBtn.setObjectName("primary")
        self.payloadReadBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.payloadReadBtn.setFixedHeight(44)
        self.payloadReadBtn.setFixedWidth(160)
        self.payloadReadBtn.clicked.connect(self.do_payload_read)
        srcRow.addWidget(self.payloadReadBtn)

        self.payloadStopBtn = QPushButton("⏹  停止")
        self.payloadStopBtn.setObjectName("secondary")
        self.payloadStopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.payloadStopBtn.setFixedHeight(44)
        self.payloadStopBtn.setFixedWidth(96)
        self.payloadStopBtn.setEnabled(False)
        self.payloadStopBtn.clicked.connect(self.do_payload_stop)
        srcRow.addWidget(self.payloadStopBtn)
        v.addLayout(srcRow)

        self.payloadInfo = QLabel("尚未载入任何内容")
        self.payloadInfo.setObjectName("info")
        self.payloadInfo.setWordWrap(True)
        v.addWidget(self.payloadInfo)

        # ---- 列表头 ----
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self._label("分区列表"))
        self.payloadSelectionInfo = QLabel("已选 0 项")
        self.payloadSelectionInfo.setObjectName("payloadSelectionInfo")
        head.addWidget(self.payloadSelectionInfo)
        self.payloadSearchE = QLineEdit()
        self.payloadSearchE.setObjectName("payloadSearch")
        self.payloadSearchE.setPlaceholderText("搜索分区…")
        self.payloadSearchE.setClearButtonEnabled(True)
        self.payloadSearchE.setFixedHeight(30)
        self.payloadSearchE.setFixedWidth(190)
        self.payloadSearchE.textChanged.connect(self._payload_filter_items)
        head.addWidget(self.payloadSearchE)
        head.addStretch()
        for text, cb in (
            ("全选", lambda: self._payload_check_all(True)),
            ("取消", lambda: self._payload_check_all(False)),
            ("反选", self._payload_invert),
        ):
            b = QPushButton(text)
            b.setObjectName("ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(32)
            b.setFixedWidth(64)
            b.clicked.connect(cb)
            head.addWidget(b)
        v.addLayout(head)

        self.payloadList = QListWidget()
        self.payloadList.setObjectName("payloadList")
        self.payloadList.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.payloadList.setMinimumHeight(300)
        self.payloadList.setSpacing(3)
        self.payloadList.setAlternatingRowColors(False)
        self.payloadList.setItemDelegate(PayloadItemDelegate(self.theme, self.payloadList))
        self.payloadList.itemPressed.connect(self._payload_on_item_pressed)
        self.payloadList.itemClicked.connect(self._payload_on_item_clicked)
        self.payloadList.itemChanged.connect(self._payload_on_item_changed)
        v.addWidget(self.payloadList, 1)

        # ---- 输出 ----
        outRow = QHBoxLayout()
        outRow.setSpacing(8)
        outRow.addWidget(self._label("输出目录"))
        self.payloadOutE = QLineEdit(str(Path(OUT_DIR) / "extracted"))
        self.payloadOutE.setFixedHeight(36)
        outRow.addWidget(self.payloadOutE, 1)

        self.payloadOutBtn = QPushButton("📁  选择")
        self.payloadOutBtn.setObjectName("secondary")
        self.payloadOutBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.payloadOutBtn.setFixedHeight(36)
        self.payloadOutBtn.setFixedWidth(90)
        self.payloadOutBtn.clicked.connect(self._payload_pick_outdir)
        outRow.addWidget(self.payloadOutBtn)

        self.payloadExtractBtn = QPushButton("📤  提取选中")
        self.payloadExtractBtn.setObjectName("primary")
        self.payloadExtractBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.payloadExtractBtn.setFixedHeight(36)
        self.payloadExtractBtn.setFixedWidth(140)
        self.payloadExtractBtn.setEnabled(False)
        self.payloadExtractBtn.clicked.connect(self.do_payload_extract)
        outRow.addWidget(self.payloadExtractBtn)
        v.addLayout(outRow)

        # ---- 状态 / 进度 ----
        progRow = QHBoxLayout()
        progRow.setSpacing(10)
        self.payloadStatus = QLabel("等待操作…")
        self.payloadStatus.setObjectName("info")
        progRow.addWidget(self.payloadStatus, 1)

        self.payloadProg = QProgressBar()
        self.payloadProg.setRange(0, 100)
        self.payloadProg.setValue(0)
        self.payloadProg.setFixedHeight(10)
        self.payloadProg.setTextVisible(False)
        self.payloadProg.setFixedWidth(220)
        progRow.addWidget(self.payloadProg)
        v.addLayout(progRow)

        # 内部状态
        self._payload_src_kind = None       # "local"
        self._payload_src_value = None
        self._payload_cancel = threading.Event()
        self._payload_token = 0
        self._payload_busy = False          # 读取 或 提取 中
        self._payload_partitions = []       # [(name, size_str), ...]
        self._payload_archive_mode = False  # 普通 ZIP 只展示内容
        return w

    # ====================== 状态工具 ======================
    def _payload_set_prog(self, val):
        try:
            if val < 0:
                self.payloadProg.setRange(0, 0)
            else:
                self.payloadProg.setRange(0, 100)
                self.payloadProg.setValue(max(0, min(100, int(val))))
        except Exception:
            pass

    def _payload_set_status(self, text, color=None):
        try:
            self.payloadStatus.setText(text)
            if color is None:
                color = self.theme["dim"]
            self.payloadStatus.setStyleSheet(
                f"color: {color}; background: transparent;")
        except Exception:
            pass

    def _payload_pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.payloadOutE.setText(d)

    def _ui_if_current(self, token, fn):
        def _wrapped():
            if self._payload_token == token:
                fn()
        self.sig.ui.emit(_wrapped)

    # ====================== 勾选视觉 ======================
    def _payload_apply_item_style(self, it):
        try:
            checked = it.checkState() == Qt.CheckState.Checked
            f = it.font()
            f.setBold(checked)
            it.setFont(f)
            if checked:
                bg = QColor(self.theme["acc"]).darker(150)
                it.setBackground(bg)
                it.setForeground(QColor("#ffffff"))
            else:
                it.setBackground(QColor(self.theme["log"]))
                it.setForeground(QColor(self.theme["dim"]))
        except Exception:
            pass

    def _payload_check_all(self, checked):
        self.payloadList.blockSignals(True)
        for i in range(self.payloadList.count()):
            it = self.payloadList.item(i)
            if it.isHidden():
                continue
            it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.payloadList.blockSignals(False)
        for i in range(self.payloadList.count()):
            self._payload_apply_item_style(self.payloadList.item(i))
        self._payload_update_extract_btn()

    def _payload_invert(self):
        self.payloadList.blockSignals(True)
        for i in range(self.payloadList.count()):
            it = self.payloadList.item(i)
            if it.isHidden():
                continue
            cur = it.checkState()
            it.setCheckState(Qt.CheckState.Unchecked
                             if cur == Qt.CheckState.Checked else Qt.CheckState.Checked)
        self.payloadList.blockSignals(False)
        for i in range(self.payloadList.count()):
            self._payload_apply_item_style(self.payloadList.item(i))
        self._payload_update_extract_btn()

    def _payload_checked_names(self):
        out = []
        for i in range(self.payloadList.count()):
            it = self.payloadList.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _payload_update_extract_btn(self, *_):
        checked_count = sum(
            self.payloadList.item(i).checkState() == Qt.CheckState.Checked
            for i in range(self.payloadList.count()))
        if self._payload_archive_mode:
            self.payloadSelectionInfo.setText(
                f"普通压缩包 · {self.payloadList.count()} 个文件")
        else:
            self.payloadSelectionInfo.setText(f"已选 {checked_count} 项")
        self.payloadExtractBtn.setText(
            f"📤  提取选中 ({checked_count})")
        self.payloadExtractBtn.setEnabled(self.payloadList.count() > 0)

    def _payload_on_item_changed(self, it):
        self._payload_apply_item_style(it)
        self._payload_update_extract_btn()

    def _payload_on_item_pressed(self, it):
        self._payload_press_state = it.checkState()

    def _payload_on_item_clicked(self, it):
        if getattr(self, "_payload_press_state", None) == it.checkState():
            it.setCheckState(
                Qt.CheckState.Unchecked
                if it.checkState() == Qt.CheckState.Checked
                else Qt.CheckState.Checked)
        self._payload_press_state = None

    def _payload_filter_items(self, text):
        query = (text or "").strip().casefold()
        for i in range(self.payloadList.count()):
            item = self.payloadList.item(i)
            item.setHidden(bool(query) and query not in item.text().casefold())

    def _payload_clear_list(self):
        self._payload_archive_mode = False
        self.payloadSearchE.clear()
        self.payloadList.blockSignals(True)
        self.payloadList.clear()
        self.payloadList.blockSignals(False)
        self.payloadExtractBtn.setEnabled(False)
        self.payloadSelectionInfo.setText("已选 0 项")
        self.payloadExtractBtn.setText("📤  提取选中 (0)")

    def _payload_add_item(self, display, payload, checked=False):
        it = QListWidgetItem(display)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, payload)
        self.payloadList.addItem(it)
        return it

    def _payload_end_batch(self):
        for i in range(self.payloadList.count()):
            self._payload_apply_item_style(self.payloadList.item(i))
        self._payload_update_extract_btn()

    # ====================== 忙碌状态 ======================
    def _payload_start_busy(self):
        self._payload_token += 1
        self._payload_cancel = threading.Event()
        self._payload_busy = True
        self.payloadReadBtn.setEnabled(False)
        self.payloadStopBtn.setEnabled(True)
        self.payloadLocalBtn.setEnabled(False)
        self.payloadExtractBtn.setEnabled(False)
        self._payload_set_prog(-1)
        return self._payload_token, self._payload_cancel

    def _payload_end_busy(self, token):
        if self._payload_token != token:
            return
        self._payload_busy = False
        self.payloadReadBtn.setEnabled(True)
        self.payloadStopBtn.setEnabled(False)
        self.payloadLocalBtn.setEnabled(True)
        self._payload_cancel = threading.Event()
        self._payload_update_extract_btn()
        self._payload_set_prog(0)

    # ====================== 停止 ======================
    def do_payload_stop(self):
        if not self._payload_busy:
            return
        self.sig.log.emit("⏹ 正在停止 …", "warn")
        self._payload_cancel.set()
        with _ACTIVE_PROCESSES_LOCK:
            processes = list(_ACTIVE_PROCESSES)
        for process in processes:
            try:
                if WIN and process.pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    process.kill()
            except Exception:
                pass
        self.payloadStopBtn.setEnabled(False)
        self._payload_set_status("⏹ 正在停止 …", self.theme["warn"])

    # ====================== 选择本地 ======================
    def do_payload_pick_local(self):
        if self._payload_busy:
            self._payload_cancel.set()
            self.sig.log.emit("⏹ 当前任务正在取消，不能切换到本地文件", "warn")
            self._payload_set_status("⏹ 当前任务正在取消…", self.theme["warn"])
            return

        f, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "",
            "Payload / 压缩包 (*.zip *.7z *.bin *.img *.payload);;所有文件 (*)")
        if not f:
            return

        if not _is_payload_like_source(f):
            QMessageBox.warning(
                self, "不是 payload 文件",
                "当前选择的不是 payload.bin / zip / .img 这类有效载荷文件。\n\n"
                "请重新选择 payload.bin、*.zip 或对应分区包文件；\n"
                "不要选 tar.gz / .tar / .exe 这类工具包。")
            return

        error = _validate_local_source(f)
        if error:
            QMessageBox.warning(self, "无法读取文件", error)
            return

        self.payloadUrlE.clear()
        self._payload_src_kind = "local"
        self._payload_src_value = f
        QTimer.singleShot(50, self.do_payload_read)

    # ====================== 读取入口 ======================
    def do_payload_read(self):
        if self._payload_busy:
            self._payload_cancel.set()
            self.sig.log.emit("⏹ 正在读取中，忽略重复触发", "warn")
            self._payload_set_status("⏹ 当前任务正在取消…", self.theme["warn"])
            return

        source_text = self.payloadUrlE.text().strip()

        if source_text:
            if source_text.startswith(("http://", "https://")):
                QMessageBox.information(
                    self, "不支持远程读取",
                    "当前页面只读取本地文件。请先把 OTA ZIP 下载到电脑，再点击「本地文件」选择。")
                return
            if not _is_payload_like_source(source_text):
                QMessageBox.warning(
                    self, "不是 payload 文件",
                    "请选择本地 payload.bin、OTA ZIP 或对应的本地载荷文件。")
                return
            error = _validate_local_source(source_text)
            if error:
                QMessageBox.warning(self, "无法读取文件", error)
                return
            self._payload_src_kind = "local"
            self._payload_src_value = source_text
        elif self._payload_src_value:
            self._payload_src_kind = self._payload_src_kind or "local"
        else:
            QMessageBox.information(
                self, "未提供来源",
                "请输入本地文件路径，或点击「📂 本地文件」选择 OTA ZIP / payload.bin。")
            return

        # 找工具
        tool, kind = _find_tool()
        if not tool:
            self.sig.log.emit("❌ 未找到 payload-dumper-go.exe", "error")
            self.sig.log.emit(
                "   请把 payload-dumper-go.exe 放到项目根目录或 tools/ 子目录", "info")
            QMessageBox.critical(
                self, "缺少工具",
                "未找到 payload-dumper-go.exe。\n\n"
                "请把 payload-dumper-go.exe 放到：\n"
                "  · 项目根目录\n"
                "  · tools/ 子目录\n"
                "  · 或加入系统 PATH")
            return

        self._payload_clear_list()
        self._payload_partitions = []

        token, cancel = self._payload_start_busy()
        self.lg(f"读取分区表：{self._payload_src_value}", "info")
        self._payload_set_status("🔍 正在读取分区表 …", self.theme["warn"])

        threading.Thread(
            target=self._read_worker_safe,
            args=(token, cancel, tool, self._payload_src_value),
            daemon=True).start()

    def _read_worker_safe(self, token, cancel, tool, src):
        try:
            self._read_worker(token, cancel, tool, src)
        except Exception as exc:
            self.sig.log.emit(f"❌ 读取异常：{exc}", "error")
            self._ui_if_current(token,
                lambda: self._read_finish(token, False, "读取异常"))

    def _read_worker(self, token, cancel, tool, src):
        # 本地文件先检查存在
        if not Path(src).exists():
            self.sig.log.emit(f"❌ 文件不存在：{src}", "error")
            self._ui_if_current(token,
                lambda: self._read_finish(token, False, "文件不存在"))
            return

        if Path(src).suffix.lower() in (".zip", ".7z") \
            and not _archive_contains_payload(src):
            try:
                entries = _archive_entries(src)
            except Exception as exc:
                self.sig.log.emit(f"❌ 压缩包读取失败：{exc}", "error")
                self._ui_if_current(token,
                    lambda: self._read_finish(token, False, "压缩包读取失败"))
                return

            self._payload_partitions = entries
            self._payload_archive_mode = True

            def fill_archive():
                self.payloadInfo.setText(
                    f"📦 {src}\n   普通压缩包，共 {len(entries)} 个文件（没有 payload.bin）")
                self.payloadInfo.setStyleSheet(
                    f"color: {self.theme['text']}; background: transparent;")
                self.payloadList.blockSignals(True)
                for name, size_r in entries:
                    self._payload_add_item(name, name)
                self.payloadList.blockSignals(False)
                self._payload_end_batch()
                self._read_finish(token, True,
                    f"✓ ZIP 读取成功：{len(entries)} 个文件")

            self._ui_if_current(token, fill_archive)
            return

        cmd = [tool, "-m", "-l", src]
        self.sig.log.emit(f"执行：{' '.join(cmd)}", "plain")

        def on_text(line):
            s = (line or "").rstrip()
            if s:
                self.sig.log.emit(f"{_tool_log_name(tool)} {s}", "plain")

        rc, out = _run_cli(cmd, cancel_event=cancel, on_text=on_text)

        if rc == -100:
            self.sig.log.emit("⏹ 读取已停止", "warn")
            self._ui_if_current(token,
                lambda: self._read_cancelled(token))
            return

        if rc != 0:
            self.sig.log.emit(f"❌ 工具返回码 {rc}", "error")
            self._ui_if_current(token,
                lambda: self._read_finish(token, False, f"工具返回码 {rc}"))
            return

        parts = _parse_list_output(out)
        if not parts:
            self.sig.log.emit("⚠ 未从输出中解析出任何分区", "warn")
            self.sig.log.emit(f"原始输出：{out[:1000]}", "plain")
            self._ui_if_current(token,
                lambda: self._read_finish(token, False, "未解析出分区"))
            return

        self._payload_partitions = parts

        def fill():
            self.payloadInfo.setText(
                f"📦 {src}\n   共 {len(parts)} 个分区（payload）")
            self.payloadInfo.setStyleSheet(
                f"color: {self.theme['text']}; background: transparent;")
            self.payloadList.blockSignals(True)
            for name, size_r in parts:
                display = f"{name}.img    （{size_r}）" if size_r else f"{name}.img"
                self._payload_add_item(display, name)
            self.payloadList.blockSignals(False)
            self._payload_end_batch()
            self._read_finish(token, True,
                f"✓ 读取成功：{len(parts)} 个分区")
        self._ui_if_current(token, fill)

    def _read_finish(self, token, ok, msg):
        self._payload_end_busy(token)
        if ok:
            self._payload_set_status(msg, self.theme["ok"])
            self.lg(msg, "ok")
        else:
            self._payload_set_status(f"❌ {msg}", self.theme["err"])
            self.lg(f"❌ {msg}", "error")

    def _read_cancelled(self, token):
        self._payload_end_busy(token)
        self._payload_set_status("⏹ 已停止", self.theme["warn"])

    # ====================== 提取 ======================
    def do_payload_extract(self):
        if not self._payload_partitions:
            QMessageBox.warning(self, "无内容", "请先读取分区表。")
            return
        names = self._payload_checked_names()
        if not names:
            QMessageBox.information(self, "未选择", "请勾选要提取的分区。")
            return

        outdir = self.payloadOutE.text().strip()
        if not outdir:
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return
        Path(outdir).mkdir(parents=True, exist_ok=True)

        if self._payload_archive_mode:
            self._start_archive_extract(names, outdir)
            return

        tool, kind = _find_tool()
        if not tool:
            QMessageBox.critical(self, "缺少工具",
                "未找到 payload-dumper-go.exe。")
            return
        if not _find_xz():
            QMessageBox.critical(
                self, "缺少解压依赖",
                "提取 payload 分区需要 xz.exe。\n\n"
                "请安装 xz 并加入 PATH，或把 xz.exe 放到 tools 子目录。")
            self.sig.log.emit("❌ 未找到 xz.exe，无法提取压缩分区", "error")
            return

        token, cancel = self._payload_start_busy()
        self.lg(f"开始提取 {len(names)} 个分区 → {outdir}", "info")
        self._payload_set_status(
            f"⏳ 正在提取 {len(names)} 个分区 …", self.theme["warn"])

        threading.Thread(
            target=self._extract_worker_safe,
            args=(token, cancel, tool, self._payload_src_value, names, outdir),
            daemon=True).start()

    def _start_archive_extract(self, names, outdir):
        token, cancel = self._payload_start_busy()
        self.lg(f"开始提取 {len(names)} 个压缩包文件 → {outdir}", "info")
        self._payload_set_status(
            f"⏳ 正在提取 {len(names)} 个文件 …", self.theme["warn"])
        threading.Thread(
            target=self._archive_extract_worker_safe,
            args=(token, cancel, self._payload_src_value, names, outdir),
            daemon=True).start()

    def _archive_extract_worker_safe(self, token, cancel, src, names, outdir):
        try:
            source = Path(src)
            if source.suffix.lower() == ".7z":
                if py7zr is None:
                    raise RuntimeError("读取 7z 需要安装 py7zr")
                with py7zr.SevenZipFile(source, mode="r") as package:
                    package.extract(path=outdir, targets=names)
            else:
                root = Path(outdir).resolve()
                with zipfile.ZipFile(source) as package:
                    for name in names:
                        if cancel.is_set():
                            self._ui_if_current(token,
                                lambda: self._extract_finish(token, False, "已停止"))
                            return
                        target = (root / name).resolve()
                        if root != target and root not in target.parents:
                            raise ValueError(f"非法压缩包路径：{name}")
                        package.extract(name, root)
            self._ui_if_current(token,
                lambda: self._extract_finish(token, True, "✅ 压缩包文件提取完成"))
        except Exception as exc:
            self.sig.log.emit(f"❌ 压缩包提取异常：{exc}", "error")
            self._ui_if_current(token,
                lambda: self._extract_finish(token, False, "压缩包提取失败"))

    def _extract_worker_safe(self, token, cancel, tool, src, names, outdir):
        try:
            self._extract_worker(token, cancel, tool, src, names, outdir)
        except Exception as exc:
            self.sig.log.emit(f"❌ 提取异常：{exc}", "error")
            self._ui_if_current(token,
                lambda: self._extract_finish(token, False, "提取异常"))

    def _extract_worker(self, token, cancel, tool, src, names, outdir):
        cmd = [tool,
               "-p", ",".join(names),
               "-o", outdir,
               src]
        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")

        def on_text(line):
            s = (line or "").rstrip()
            if not s:
                return
            self.sig.log.emit(f"[tool] {s}", "plain")
            m = PROG_RE.search(s)
            if m:
                try:
                    v = int(m.group(1))
                    self._ui_if_current(
                        token, lambda v=v: self._payload_set_prog(v))
                except Exception:
                    pass

        rc, output = _run_cli(cmd, cancel_event=cancel, on_text=on_text)

        if rc == -100:
            self.sig.log.emit("⏹ 提取已停止", "warn")
            self._ui_if_current(token,
                lambda: self._extract_finish(token, False, "已停止"))
            return

        if rc == 0:
            self._ui_if_current(token,
                lambda: self._extract_finish(token, True, "✅ 提取完成"))
        else:
            if output.strip():
                self.sig.log.emit(f"工具输出：{output[:3000]}", "error")
            self._ui_if_current(token,
                lambda: self._extract_finish(token, False, f"工具返回码 {rc}"))

    def _extract_finish(self, token, ok, msg):
        self._payload_end_busy(token)
        if ok:
            self._payload_set_status(msg, self.theme["ok"])
            self.lg(msg, "ok")
        else:
            self._payload_set_status(f"❌ {msg}", self.theme["err"])
            self.lg(f"❌ {msg}", "error")