# -*- coding: utf-8 -*-
"""页面：Recovery 工具（ADB Sideload / 导出相册 / 推送文件）"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QComboBox, QTextEdit, QMessageBox, QFileDialog,
)
from PyQt6.QtCore import Qt

from core.config import WIN
from core.widgets import AnimatedProgressBar, Card, set_state
from core.utils import (
    sh, adb_list, shq as _shq, hsize as _hsize,
    ascii_work_dir, move_into_place, has_nonascii_dirs, adb_pull_tree,
)

# ==================== 常量 ====================
# 设备模式 → 显示名（检测在 ui/main_window.py，右上角胶囊也用它）
MODE_NAMES = {
    "none": "未连接", "system": "系统模式", "recovery": "Recovery 模式",
    "sideload": "Sideload 模式", "fastboot": "Fastboot 模式",
    "unauthorized": "未授权", "offline": "离线 / 无响应",
}

# 导出相册预设（右边 None 表示让用户自己改下面的路径）
EXPORT_PRESETS = [
    ("相册 DCIM（相机 + 截图）", "/sdcard/DCIM"),
    ("图片 Pictures", "/sdcard/Pictures"),
    ("截图 Pictures/Screenshots", "/sdcard/Pictures/Screenshots"),
    ("下载 Download", "/sdcard/Download"),
    ("视频 Movies", "/sdcard/Movies"),
    ("整个内部存储 /sdcard（可能很大）", "/sdcard"),
    ("自定义（直接改下面的路径）", None),
]

# 推送目标预设
PUSH_PRESETS = [
    ("下载 Download", "/sdcard/Download"),
    ("相册 DCIM", "/sdcard/DCIM"),
    ("图片 Pictures", "/sdcard/Pictures"),
    ("视频 Movies", "/sdcard/Movies"),
    ("铃声 Ringtones", "/sdcard/Ringtones"),
    ("内部存储根目录 /sdcard", "/sdcard"),
    ("自定义（直接改下面的路径）", None),
]

# 报错关键字 → 人话（sideload / 导出 / 推送各自的常见坑）
SIDELOAD_ERRORS = (
    ("sideload connection failed",
     "设备不在 Sideload 模式：请在 Recovery 里选 “Apply update from ADB”，"
     "或用上面的「⚡ 重启并进入 Sideload」"),
    ("failed to read command",
     "设备端中断了传输：多半是包不完整、机型不匹配，或数据线接触不良"),
    ("closed", "连接被关闭：设备没在 Sideload 模式，或传输过程中数据线掉了"),
    ("protocol fault", "通信协议出错：换一个 USB 口（建议直插主板）或换数据线重试"),
    ("no such file", "本地找不到这个侧载包，检查路径是否被移动/删除"),
    ("device not found", "设备已断开，重新插线后再试"),
)
PULL_ERRORS = (
    ("does not exist",
     "手机上找不到该目录：确认路径是否正确；Recovery 下要先把 /sdcard（data/media）挂载上"),
    ("permission denied", "没有读取权限：系统模式下部分目录需要 root 才能读"),
    ("no space left", "电脑磁盘空间不足，换一个导出目录"),
    ("device not found", "设备已断开，重新插线后再试"),
)
PUSH_ERRORS = (
    ("no space left", "手机存储空间不足"),
    ("permission denied", "手机目标目录没有写权限：/sdcard 之外的目录通常需要 root；"
                          "Recovery 下请先挂载分区"),
    ("remote couldn't create file",
     "手机端没法在目标目录创建文件：确认目标目录存在、有写权限，文件名也别带特殊字符"),
    ("is a directory",
     "远端同名位置已经是个目录：换一个目标目录，或先把手机上那个同名文件夹改名"),
    ("device not found", "设备已断开，重新插线后再试"),
)

_RE_PCT_BRACKET = re.compile(r"\[\s*(\d+)%\]")      # adb push/pull -p 的进度
_RE_PCT_TILDE = re.compile(r"~(\d+)%")              # adb sideload 的进度
_RE_XFER_OK = re.compile(r"Total xfer", re.I)       # sideload 结束标志
_RE_NOBUILD = re.compile(r"No such file or directory|not found", re.I)
# _shq() / _hsize() 来自 core.utils（shq / hsize），两个页面共用同一套实现


class RecoveryPageMixin:

    # ==================== 界面 ====================
    def _page_recovery(self):
        w = QWidget()
        self.recPage = w
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("🛠  Recovery 工具")
        title.setObjectName("cardTitle")
        v.addWidget(title)
        desc = QLabel("侧载刷机、导出相册、往手机推文件。设备模式（系统 / Recovery / "
                      "Sideload / Fastboot）在右上角实时显示；需要重启或切换模式请到「📱 主页」。")
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        v.addWidget(desc)

        # ============ 左右两栏：左 = 三个功能卡片，右 = 任务进度 + 实时输出 ============
        cols = QHBoxLayout()
        cols.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(12)
        right = QVBoxLayout()
        right.setSpacing(12)
        cols.addLayout(left, 5)
        cols.addLayout(right, 4)
        v.addLayout(cols, 1)

        # ---------- ① ADB Sideload ----------
        sdCard = Card()
        sv = QVBoxLayout(sdCard)
        sv.setContentsMargins(18, 14, 18, 16)
        sv.setSpacing(10)
        sdT = QLabel("①  ADB Sideload（侧载刷机）")
        sdT.setObjectName("cardTitle")
        sv.addWidget(sdT)
        sdD = QLabel("侧载刷机要求手机处于 Sideload 模式：重启到 Recovery 后，"
                     "在手机上选 “Apply update from ADB”。"
                     "程序会自动检测，检测到 Sideload 模式后按钮才会亮起。")
        sdD.setObjectName("desc")
        sdD.setWordWrap(True)
        sv.addWidget(sdD)

        self.recSdLb = QLabel("⏳ 正在检测设备模式…")
        self.recSdLb.setObjectName("info")
        self.recSdLb.setWordWrap(True)
        sv.addWidget(self.recSdLb)

        zipRow = QHBoxLayout()
        zipRow.setSpacing(8)
        self.recZipE = QLineEdit()
        self.recZipE.setReadOnly(True)
        self.recZipE.setPlaceholderText("选择 OTA / 侧载刷机包（.zip）…")
        self.recZipE.setFixedHeight(40)
        zipRow.addWidget(self.recZipE, 1)
        self.recZipBtn = QPushButton("📂  选择 zip")
        self.recZipBtn.setObjectName("primary")
        self.recZipBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recZipBtn.setFixedHeight(40)
        self.recZipBtn.setMinimumWidth(120)
        self.recZipBtn.clicked.connect(self.do_rec_pick_zip)
        zipRow.addWidget(self.recZipBtn)
        sv.addLayout(zipRow)

        self.recSideloadBtn = QPushButton("🚀  开始 Sideload")
        self.recSideloadBtn.setObjectName("primary")
        self.recSideloadBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recSideloadBtn.setFixedHeight(46)
        self.recSideloadBtn.setToolTip(
            "只有手机处于 Sideload 模式时才能开始侧载\n"
            "进入方法：重启到 Recovery 后，在手机上选 “Apply update from ADB”")
        self.recSideloadBtn.clicked.connect(self.do_rec_sideload)
        sv.addWidget(self.recSideloadBtn)
        left.addWidget(sdCard)

        # ---------- ② 导出相册 ----------
        exCard = Card()
        ev = QVBoxLayout(exCard)
        ev.setContentsMargins(18, 14, 18, 16)
        ev.setSpacing(10)
        exT = QLabel("②  导出相册 / 目录到电脑")
        exT.setObjectName("cardTitle")
        ev.addWidget(exT)
        exD = QLabel("默认相册 DCIM，也可选其它目录或直接改路径；用 adb pull 拉取，进度实时显示。")
        exD.setObjectName("desc")
        exD.setWordWrap(True)
        ev.addWidget(exD)

        exRow = QHBoxLayout()
        exRow.setSpacing(8)
        self.recExportCb = QComboBox()
        for name, path in EXPORT_PRESETS:
            self.recExportCb.addItem(name, path)
        self.recExportCb.setFixedHeight(40)
        self.recExportCb.setFixedWidth(190)
        self.recExportCb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recExportCb.currentIndexChanged.connect(self._rec_export_preset_changed)
        exRow.addWidget(self.recExportCb)
        self.recExportPathE = QLineEdit("/sdcard/DCIM")
        self.recExportPathE.setFixedHeight(40)
        self.recExportPathE.setPlaceholderText("/sdcard/DCIM")
        self.recExportPathE.setToolTip("手机上的目录，可直接改成别的，例如 /sdcard/DCIM/Camera")
        exRow.addWidget(self.recExportPathE, 1)
        ev.addLayout(exRow)

        exGo = QHBoxLayout()
        exGo.setSpacing(8)
        self.recExportBtn = QPushButton("⬇  导出到电脑…")
        self.recExportBtn.setObjectName("primary")
        self.recExportBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recExportBtn.setFixedHeight(42)
        self.recExportBtn.clicked.connect(self.do_rec_export)
        exGo.addWidget(self.recExportBtn, 1)
        self.recOpenDirBtn = QPushButton("📂  打开目录")
        self.recOpenDirBtn.setObjectName("ghost")
        self.recOpenDirBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recOpenDirBtn.setFixedHeight(42)
        self.recOpenDirBtn.setMinimumWidth(140)
        self.recOpenDirBtn.setEnabled(False)
        self.recOpenDirBtn.setToolTip("在文件管理器里打开上一次的导出位置")
        self.recOpenDirBtn.clicked.connect(self.do_rec_open_dir)
        exGo.addWidget(self.recOpenDirBtn)
        ev.addLayout(exGo)
        left.addWidget(exCard)

        # ---------- ③ 推送文件 ----------
        psCard = Card()
        pv = QVBoxLayout(psCard)
        pv.setContentsMargins(18, 14, 18, 16)
        pv.setSpacing(10)
        psT = QLabel("③  推送文件 / 文件夹到手机")
        psT.setObjectName("cardTitle")
        pv.addWidget(psT)
        psD = QLabel("可多选文件与文件夹（可反复添加）；目标目录不存在会自动创建。")
        psD.setObjectName("desc")
        psD.setWordWrap(True)
        pv.addWidget(psD)

        psRow = QHBoxLayout()
        psRow.setSpacing(8)
        self.recPushDestCb = QComboBox()
        for name, path in PUSH_PRESETS:
            self.recPushDestCb.addItem(name, path)
        self.recPushDestCb.setFixedHeight(40)
        self.recPushDestCb.setFixedWidth(190)
        self.recPushDestCb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recPushDestCb.currentIndexChanged.connect(self._rec_push_preset_changed)
        psRow.addWidget(self.recPushDestCb)
        self.recPushDestE = QLineEdit("/sdcard/Download")
        self.recPushDestE.setFixedHeight(40)
        self.recPushDestE.setPlaceholderText("/sdcard/Download")
        self.recPushDestE.setToolTip("手机上的目标目录，可以直接改，例如 /sdcard/Download/新文件夹")
        psRow.addWidget(self.recPushDestE, 1)
        pv.addLayout(psRow)

        psBtnRow = QHBoxLayout()
        psBtnRow.setSpacing(8)
        self.recPickFilesBtn = QPushButton("📂  选文件")
        self.recPickDirBtn = QPushButton("📁  选文件夹")
        self.recClearFilesBtn = QPushButton("🧹  清空")
        for btn, slot, tip in ((self.recPickFilesBtn, self.do_rec_pick_files, "一次可以多选文件"),
                               (self.recPickDirBtn, self.do_rec_pick_dir, "整个文件夹一起推"),
                               (self.recClearFilesBtn, self.do_rec_clear_push, "清空已选列表")):
            btn.setObjectName("compact")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(38)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            psBtnRow.addWidget(btn, 1)
        pv.addLayout(psBtnRow)

        self.recPushListLb = QLabel("尚未选择文件")
        self.recPushListLb.setObjectName("info")
        self.recPushListLb.setWordWrap(True)
        pv.addWidget(self.recPushListLb)

        self.recPushBtn = QPushButton("⬆  推送到手机")
        self.recPushBtn.setObjectName("primary")
        self.recPushBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recPushBtn.setFixedHeight(42)
        self.recPushBtn.clicked.connect(self.do_rec_push)
        pv.addWidget(self.recPushBtn)
        left.addWidget(psCard)
        left.addStretch(1)

        # ---------- 右栏：任务进度 + 实时输出 ----------
        outCard = Card()
        ov = QVBoxLayout(outCard)
        ov.setContentsMargins(18, 14, 18, 14)
        ov.setSpacing(8)
        outT = QLabel("📜  任务 & 实时输出")
        outT.setObjectName("cardTitle")
        ov.addWidget(outT)

        tkRow = QHBoxLayout()
        tkRow.setSpacing(10)
        self.recTaskLb = QLabel("空闲")
        self.recTaskLb.setObjectName("info")
        tkRow.addWidget(self.recTaskLb)
        self.recProgress = AnimatedProgressBar()
        self.recProgress.setFixedHeight(20)
        self.recProgress.setFormat("空闲")
        tkRow.addWidget(self.recProgress, 1)
        self.recStopBtn = QPushButton("⏹  停止")
        self.recStopBtn.setObjectName("secondary")
        self.recStopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recStopBtn.setFixedHeight(36)
        self.recStopBtn.setEnabled(False)
        self.recStopBtn.clicked.connect(self.do_rec_stop)
        tkRow.addWidget(self.recStopBtn)
        ov.addLayout(tkRow)

        self.recOutput = QTextEdit()
        self.recOutput.setReadOnly(True)
        self.recOutput.setObjectName("rootOutput")
        self.recOutput.setPlaceholderText("命令输出会实时显示在这里")
        self.recOutput.setMinimumHeight(240)
        ov.addWidget(self.recOutput, 1)

        clrRow = QHBoxLayout()
        clrRow.setSpacing(8)
        self.recClrBtn = QPushButton("🧹  清空日志")
        self.recClrBtn.setObjectName("ghost")
        self.recClrBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recClrBtn.setFixedHeight(32)
        self.recClrBtn.clicked.connect(self._rec_clear_logs)
        clrRow.addWidget(self.recClrBtn)
        clrRow.addStretch(1)
        ov.addLayout(clrRow)
        right.addWidget(outCard, 1)

        # ---------- 内部状态 ----------
        self._rec_mode = "none"
        self._rec_serial = None
        self._rec_busy = False
        self._rec_cancel = threading.Event()
        self._rec_stop_requested = False
        self._rec_proc = None
        self._rec_push_paths = []
        self._rec_export_dir = ""
        self._rec_last_prog = None

        self._rec_update_push_summary()
        self._rec_refresh_buttons()
        return w

    # ==================== 通用助手 ====================
    def _rec_enable(self, name, ok):
        try:
            btn = getattr(self, name, None)
            if btn is not None:
                btn.setEnabled(bool(ok) and not self._rec_busy)
        except Exception:
            pass

    def _rec_refresh_buttons(self):
        """按「当前模式 + 是否忙」统一决定按钮能不能点"""
        mode = self._rec_mode
        adb_ok = mode in ("system", "recovery")        # 有 shell / push / pull
        # 本地操作用户随时能点
        for n in ("recZipBtn", "recPickFilesBtn", "recPickDirBtn",
                  "recClearFilesBtn"):
            self._rec_enable(n, True)
        # 需要 adb shell 的操作
        for n in ("recExportBtn", "recPushBtn"):
            self._rec_enable(n, adb_ok)
        # Sideload：必须检测到手机真在 Sideload 模式才允许操作（其余模式按钮保持灰）
        self._rec_enable("recSideloadBtn", mode == "sideload")
        try:
            self.recOpenDirBtn.setEnabled(
                bool(self._rec_export_dir) and not self._rec_busy)
        except Exception:
            pass

    def _rec_update_sd_hint(self):
        """Sideload 卡片上的模式提示（检测到什么模式、该怎么进入 Sideload）"""
        text, state = {
            "sideload": ("✅ 已检测到 Sideload 模式，可以开始侧载刷机",
                         "ok"),
            "recovery": ("⚠ 手机已在 Recovery：请在手机上选 “Apply update from ADB”，"
                         "进入 Sideload 后按钮会自动亮起",
                         "warn"),
            "system": ("❌ 手机在系统模式：请先重启到 Recovery，"
                       "然后在手机上选 “Apply update from ADB”", "err"),
            "fastboot": ("❌ Fastboot 模式没有 Sideload：请先重启到 Recovery，"
                         "再在手机上选 “Apply update from ADB”", "err"),
            "unauthorized": ("❌ 设备未授权：请在手机屏幕上点“允许 USB 调试”", "err"),
            "offline": ("❌ 设备离线：重新插拔数据线，或在 CMD 里 adb kill-server",
                        "err"),
            "none": ("❌ 未连接设备：插好数据线并授权 USB 调试", "err"),
        }.get(self._rec_mode, ("❌ 未知状态", "err"))
        try:
            set_state(self.recSdLb, state)
            self.recSdLb.setText(text)
        except Exception:
            pass

    def _rec_set_status(self, text, state="dim"):
        try:
            set_state(self.recTaskLb, state)
            self.recTaskLb.setText(text)
        except Exception:
            pass

    def _rec_progress(self, pct):
        try:
            pct = max(0, min(100, int(pct)))
            self.recProgress.set_smooth_value(pct, f"{pct}%")
        except Exception:
            pass

    def _rec_log(self, line):
        """往输出面板追加一行（只允许 UI 线程调用）"""
        try:
            line = str(line)
            if not line:
                return
            self.recOutput.append(line)
            sb = self.recOutput.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def _rec_log_async(self, line):
        """后台线程用：投递到 UI 线程再写面板"""
        try:
            text = str(line)
        except Exception:
            return
        if not text:
            return
        self.sig.ui.emit(lambda t=text: self._rec_log(t))

    def _rec_clear_logs(self):
        try:
            self.recOutput.clear()
        except Exception:
            pass
        self.sig.log.emit("已清空 Recovery 工具输出", "info")

    def _rec_set_busy(self, busy, status=None, state="dim"):
        self._rec_busy = bool(busy)
        try:
            self.recStopBtn.setEnabled(self._rec_busy)
        except Exception:
            pass
        if status is not None:
            self._rec_set_status(status, state)
        self._rec_refresh_buttons()

    def _rec_start_task(self, status, worker, *args):
        """统一起一个后台任务：清掉取消标记、置忙、重置进度、开线程"""
        if self._rec_busy:
            return
        self._rec_cancel = threading.Event()
        self._rec_stop_requested = False
        self._rec_proc = None
        self._rec_last_prog = None
        try:
            self.recProgress.set_smooth_value(0, "0%")
        except Exception:
            pass
        self._rec_set_busy(True, status, "info")
        self._rec_log_async("")
        threading.Thread(target=worker, args=args, daemon=True).start()

    def _rec_finish(self, status, detail="", state="dim", pct=None):
        """任务收尾（后台线程调用，内部切回 UI 线程）"""
        if detail:
            self._rec_log_async(str(detail).strip()[:4000])
        if pct is not None:
            self._rec_progress(pct)
        self.sig.ui.emit(lambda: self._rec_set_busy(False, status, state))
        level = {"ok": "ok", "warn": "warn", "err": "error"}.get(state, "info")
        self.sig.log.emit(status.lstrip("✅❌⚠⏹ "), level)

    def _rec_kill_proc(self):
        p = self._rec_proc
        if p is None:
            return
        try:
            if WIN and p.pid:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            else:
                p.kill()
        except Exception:
            pass

    def do_rec_stop(self):
        if not self._rec_busy:
            return
        self._rec_stop_requested = True
        self._rec_cancel.set()
        self._rec_kill_proc()
        self._rec_set_status("⏹ 已发送停止命令…", "warn")

    def _rec_run(self, args, progress_re=None, on_pct=None):
        """跑一条 adb 命令：输出实时贴到面板，返回 (rc, 全文)

        progress_re 命中的行只喂给 on_pct（写进度条），不写面板，避免进度刷屏
        """
        if self._rec_cancel.is_set():
            return -100, "已停止"
        self._rec_log_async(f"$ {' '.join(args)}")
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", **kw)
        except FileNotFoundError:
            return -1, f"未找到 {args[0]}，请确认 platform-tools 已加入 PATH"
        except Exception as e:
            return -3, f"命令启动失败：{e}"

        self._rec_proc = p
        buf = []
        try:
            for line in p.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                buf.append(line)
                m = progress_re.search(line) if progress_re else None
                if m and on_pct is not None:
                    on_pct(int(m.group(1)))
                if m:
                    # 进度帧不整行写面板，但把「正在传哪个文件」记一次：
                    # 否则大目录导出/推送时日志一片空白，看着像没在动
                    rest = line[m.end():].strip(" :")
                    if rest and rest != getattr(self, "_rec_last_prog", None):
                        self._rec_last_prog = rest
                        self._rec_log_async(f"  → {rest}")
                    continue
                self._rec_log_async(line)
                if self._rec_cancel.is_set():
                    self._rec_kill_proc()
                    break
        except Exception as e:
            buf.append(f"读取输出异常：{e}")
        try:
            p.wait(timeout=10)
        except Exception:
            self._rec_kill_proc()
        self._rec_proc = None
        rc = p.returncode if p.returncode is not None else -1
        if self._rec_cancel.is_set():
            return -100, "\n".join(buf)
        return rc, "\n".join(buf)

    # ==================== 模式同步（检测统一放在 ui/main_window.py）====================
    def _rec_current_state(self):
        """只按 adb 状态列快速判断（不跑 shell 探测），用来决定能不能 sideload"""
        try:
            rows, _ = adb_list()
        except Exception:
            return "none"
        for st in ("sideload", "recovery", "device", "unauthorized", "offline"):
            if any(s == st for _, s in rows):
                return st
        return "none"

    def _rec_sync_mode(self, mode, serial):
        """主窗口检测到设备模式后回调这里：只更新内部状态和按钮可用性"""
        self._rec_mode = mode
        self._rec_serial = serial
        self._rec_update_sd_hint()
        self._rec_refresh_buttons()

    # ==================== ADB Sideload ====================
    def do_rec_pick_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 OTA / 侧载刷机包", "",
            "侧载刷机包 (*.zip);;所有文件 (*)")
        if path:
            self.recZipE.setText(path)

    def do_rec_sideload(self):
        if self._rec_busy:
            return
        zip_path = self.recZipE.text().strip()
        if not zip_path or not Path(zip_path).is_file():
            QMessageBox.warning(self, "缺少刷机包",
                                "请先点「📂 选择 zip」选一个 OTA / 侧载刷机包。")
            return
        if self._rec_mode != "sideload":
            QMessageBox.warning(
                self, "需要 Sideload 模式",
                "侧载刷机只能在手机处于 Sideload 模式时进行。\n\n"
                "进入方法：重启到 Recovery 后，在手机上选 “Apply update from ADB”。\n\n"
                f"当前模式：{MODE_NAMES.get(self._rec_mode, '未知')}\n"
                "检测到 Sideload 模式后，「开始 Sideload」会自动亮起。")
            return

        try:
            size_mb = Path(zip_path).stat().st_size / 1024 / 1024
            info = f"（{size_mb:.1f} MB）"
        except Exception:
            info = ""
        self.sig.log.emit(f"开始 Sideload：{Path(zip_path).name}{info}", "info")
        self._rec_start_task("准备 Sideload …", self._rec_sideload_worker, zip_path)

    def _rec_sideload_worker(self, zip_path):
        try:
            # 动手前再确认一次：必须真的是 Sideload 模式（点击到执行之间模式可能变了）
            cur = self._rec_current_state()
            if cur != "sideload":
                self._rec_finish(
                    "❌ 设备不在 Sideload 模式，已取消侧载",
                    f"当前 adb 状态：{cur}\n"
                    "进入方法：重启到 Recovery 后，在手机上选 “Apply update from ADB”；"
                    "检测到 Sideload 模式后按钮会自动亮起", "err")
                return

            serial = self._rec_serial
            args = (["adb"] + (["-s", serial] if serial else [])
                    + ["sideload", zip_path])
            self.sig.ui.emit(lambda: self._rec_set_status("正在侧载刷机…", "info"))

            def on_pct(p):
                self.sig.ui.emit(lambda p=p: self._rec_progress(p))

            rc, out = self._rec_run(args, progress_re=_RE_PCT_TILDE, on_pct=on_pct)
            if self._rec_stop_requested or rc == -100:
                self._rec_finish("⏹ 已停止 Sideload", out, "warn")
                return
            low = (out or "").lower()
            if rc == 0 and _RE_XFER_OK.search(out or "") and "failed" not in low:
                self._rec_finish(
                    "✅ 侧载完成（设备通常会自己重启；若停在 Recovery 请选 Reboot system）",
                    out, "ok", pct=100)
            elif rc == 0 and "failed" not in low:
                self._rec_finish(
                    "⚠ 命令结束，但没看到 Total xfer：请确认手机是否提示安装成功",
                    out, "warn")
            else:
                reason = _rec_explain(out, SIDELOAD_ERRORS,
                                      "侧载失败：确认包与机型匹配、数据线正常后重试")
                self._rec_finish(f"❌ 侧载失败：{reason}", out, "err")
        except Exception as e:
            self._rec_finish(f"❌ 侧载出错：{e}", "", "err")
        finally:
            self._rec_proc = None

    # ==================== 导出相册 / 目录 ====================
    def _rec_export_preset_changed(self, idx):
        path = self.recExportCb.itemData(idx)
        if path:
            self.recExportPathE.setText(path)

    def _rec_default_dest(self):
        try:
            desk = Path.home() / "Desktop"
            if desk.is_dir():
                return str(desk)
        except Exception:
            pass
        try:
            return str(Path.home())
        except Exception:
            return ""

    def do_rec_export(self):
        if self._rec_busy:
            return
        if self._rec_mode not in ("system", "recovery"):
            QMessageBox.warning(
                self, "设备状态不对",
                f"当前模式：{MODE_NAMES.get(self._rec_mode, '未连接')}\n\n"
                "导出需要设备在「系统 / Recovery」模式"
                "（Sideload / Fastboot 下没有文件服务）。")
            return
        remote = self.recExportPathE.text().strip() or "/sdcard/DCIM"
        if not remote.startswith("/"):
            QMessageBox.warning(self, "路径不对",
                                "手机上的路径要以 / 开头，例如 /sdcard/DCIM")
            return
        dest = QFileDialog.getExistingDirectory(
            self, "选择电脑上的保存位置", self._rec_default_dest())
        if not dest:
            return
        self._rec_export_dir = dest
        self._rec_refresh_buttons()
        if remote.rstrip("/") == "/sdcard":
            self._rec_log_async("⚠ 选的是整个内部存储，文件可能很多，导出会比较久…")
        self.sig.log.emit(f"开始导出：{remote} → {dest}", "info")
        self._rec_start_task("准备导出…", self._rec_export_worker, remote, dest)

    def _rec_count_local(self, target):
        """统计电脑上真正落盘的文件数与总大小（adb 的 N files pulled 有时是 0，不能全信）"""
        p = Path(str(target))
        n = 0
        size = 0
        try:
            if p.is_file():
                return 1, p.stat().st_size
            for root, _dirs, files in os.walk(str(p)):
                for f in files:
                    n += 1
                    try:
                        size += (Path(root) / f).stat().st_size
                    except Exception:
                        pass
        except Exception:
            pass
        return n, size

    def _rec_pull_summary(self, out):
        """从 adb pull 的收尾行里抠出「N 个文件」（只在有数字时才用）"""
        try:
            for line in reversed((out or "").splitlines()):
                m = re.search(r"(\d+)\s+files?\s+pulled(?:,\s*(\d+)\s+skipped)?",
                              line, re.I)
                if m:
                    pulled = int(m.group(1))
                    skipped = int(m.group(2) or 0)
                    if pulled > 0:
                        extra = f"，跳过 {skipped}" if skipped else ""
                        return f"（{pulled} 个文件{extra}）"
        except Exception:
            pass
        return ""

    def _rec_export_worker(self, remote, dest):
        try:
            serial = self._rec_serial
            base = ["adb"] + (["-s", serial] if serial else [])

            # 先报一下手机上的目录大小（失败就忽略）
            try:
                rc0, out0 = sh(base + ["shell", f"du -sh {_shq(remote)}"],
                               timeout=25)
                if rc0 == 0 and out0.strip():
                    self._rec_log_async(f"[手机目录] {out0.strip().splitlines()[0]}")
            except Exception:
                pass

            self.sig.ui.emit(lambda: self._rec_set_status("正在导出…", "info"))

            def on_pct(p):
                self.sig.ui.emit(lambda p=p: self._rec_progress(p))

            # 关键：adb 在**本地路径含中文**时建不出文件（报 cannot create '…':
            # Not a directory / Is a directory，之前 E:\download\测试\Pictures 就是这么挂的），
            # 所以先拉到「同盘、纯 ASCII」的临时名，再由本程序改名到最终位置（同盘改名瞬间完成）
            rname = str(remote).rstrip("/").rsplit("/", 1)[-1] or "export"
            final = Path(dest) / rname
            work = ascii_work_dir(final)
            target = work / f".tb_pull_{int(time.time() * 1000) % 100000000}"

            # 远端树里有非 ASCII 目录名？→ adb 建不出同名本地目录，改走混合通道
            # （ASCII 子树一次拉，含中文目录名的子树逐文件拉）
            if has_nonascii_dirs(self._rec_serial, remote) is True:
                self._rec_log_async("  手机上有非 ASCII 目录名，"
                                    "adb 无法在电脑上创建同名目录 → 按子树分别处理")

                def _prog(p):
                    self.sig.ui.emit(lambda p=p: self._rec_progress(p))

                def _file(i, total, rel):
                    self.sig.ui.emit(lambda i=i, t=total, r=rel: self._rec_set_status(
                        f"导出中（{i}/{t}）{r}", "info"))

                rc, okc, failc = adb_pull_tree(
                    self._rec_serial, remote, final, work,
                    on_file=_file, on_progress=_prog,
                    cancel=self._rec_cancel, log=self._rec_log_async)
                if rc == -100 or self._rec_stop_requested:
                    self._rec_finish("⏹ 已停止导出（已传的文件保留在电脑上）", "", "warn")
                    return
                n, size = self._rec_count_local(final)
                if rc == 0 and n:
                    self._rec_finish(
                        f"✅ 导出完成（{n} 个文件，共 {_hsize(size)}） → {final}",
                        "", "ok", pct=100)
                elif n:
                    self._rec_finish(
                        f"⚠ 导出中断：已保存 {n} 个文件（共 {_hsize(size)}）→ {final}"
                        f"（失败 {failc} 项）", "", "warn", pct=100)
                else:
                    self._rec_finish("❌ 导出失败：没能取到任何文件", "", "err")
                return

            rc, out = self._rec_run(base + ["pull", "-p", remote, str(target)],
                                    progress_re=_RE_PCT_BRACKET, on_pct=on_pct)
            if self._rec_stop_requested or rc == -100:
                if move_into_place(target, final):
                    self._rec_log_async(f"  ↻ 已把已传部分落地：{final}")
                self._rec_finish("⏹ 已停止导出（已传的文件保留在电脑上）", out, "warn")
                return
            if rc == 0:
                if move_into_place(target, final):
                    if str(final) != str(target):
                        self._rec_log_async(f"  ↻ 电脑端落地：{final}")
                    target = final
                else:
                    self._rec_log_async(f"  ⚠ 没能把结果改名到 {final}，文件仍在 {target}")

                n, size = self._rec_count_local(target)
                if n:
                    summary = f"（{n} 个文件，共 {_hsize(size)}）"
                else:
                    summary = "（手机上这个目录里没有文件，只有子文件夹或本身是空的）"
                    self._rec_log_async("提示：adb 报 0 个文件 —— 目标目录里确实没有文件，"
                                        "不代表导出失败")
                self._rec_finish(f"✅ 导出完成{summary} → {target}", out, "ok", pct=100)
            else:
                # 失败也把已经拉下来的部分留下，别白拉
                part = 0
                try:
                    if target.exists():
                        part = sum(len(fs) for _r, _d, fs in os.walk(str(target)))
                        if not move_into_place(target, final):
                            final = target
                except Exception:
                    part = 0
                reason = _rec_explain(out, PULL_ERRORS,
                                      "导出失败：检查路径与数据线后重试")
                if "cannot create" in (out or "").lower():
                    reason += "（电脑上的保存路径尽量别用中文/特殊字符；" \
                              "程序已自动用临时目录中转）"
                if part:
                    self._rec_finish(
                        f"⚠ 导出中断：已保存 {part} 个文件 → {final}（{reason}）",
                        out, "warn", pct=100)
                else:
                    self._rec_finish(f"❌ 导出失败：{reason}", out, "err")
        except Exception as e:
            self._rec_finish(f"❌ 导出出错：{e}", "", "err")
        finally:
            self._rec_proc = None

    def do_rec_open_dir(self):
        d = self._rec_export_dir
        try:
            if not d or not Path(d).is_dir():
                return
            if WIN:
                os.startfile(d)                       # noqa: S606
            else:
                for opener in ("open", "xdg-open"):
                    try:
                        subprocess.Popen([opener, d])
                        return
                    except Exception:
                        continue
        except Exception as e:
            self.sig.log.emit(f"打开目录失败：{e}", "error")

    # ==================== 推送文件到手机 ====================
    def _rec_push_preset_changed(self, idx):
        path = self.recPushDestCb.itemData(idx)
        if path:
            self.recPushDestE.setText(path)

    def do_rec_pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要推送到手机的文件", "", "所有文件 (*)")
        if paths:
            self._rec_add_push_paths(paths)

    def do_rec_pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择要推送到手机的文件夹", "")
        if d:
            self._rec_add_push_paths([d])

    def _rec_add_push_paths(self, paths):
        for p in paths:
            if p and p not in self._rec_push_paths:
                self._rec_push_paths.append(p)
        self._rec_update_push_summary()

    def do_rec_clear_push(self):
        self._rec_push_paths = []
        self._rec_update_push_summary()

    def _rec_update_push_summary(self):
        n = len(self._rec_push_paths)
        try:
            if not n:
                self.recPushListLb.setText("尚未选择文件")
                self.recPushListLb.setToolTip("")
                set_state(self.recPushListLb, "dim")
                return
            names = "、".join(Path(p).name for p in self._rec_push_paths[:4])
            more = f" 等 {n} 项" if n > 4 else ""
            self.recPushListLb.setText(f"已选 {n} 项：{names}{more}")
            self.recPushListLb.setToolTip("\n".join(self._rec_push_paths))
            set_state(self.recPushListLb, "text")
        except Exception:
            pass

    def do_rec_push(self):
        if self._rec_busy:
            return
        if self._rec_mode not in ("system", "recovery"):
            QMessageBox.warning(
                self, "设备状态不对",
                f"当前模式：{MODE_NAMES.get(self._rec_mode, '未连接')}\n\n"
                "推送需要设备在「系统 / Recovery」模式"
                "（Sideload / Fastboot 下没有文件服务）。")
            return
        paths = [p for p in self._rec_push_paths if Path(p).exists()]
        if not paths:
            QMessageBox.warning(self, "还没选文件",
                                "请先点「📂 选择文件…」或「📁 选择文件夹…」。")
            return
        dest = self.recPushDestE.text().strip() or "/sdcard/Download"
        if not dest.startswith("/"):
            QMessageBox.warning(self, "路径不对",
                                "手机目标路径要以 / 开头，例如 /sdcard/Download")
            return
        self.sig.log.emit(f"开始推送 {len(paths)} 项 → {dest}", "info")
        self._rec_start_task("准备推送…", self._rec_push_worker, paths, dest)

    def _rec_push_worker(self, paths, dest):
        try:
            serial = self._rec_serial
            base = ["adb"] + (["-s", serial] if serial else [])
            dest = (dest or "/sdcard/Download").rstrip("/") or "/"
            # 目标目录不存在就先建（toybox mkdir -p）
            try:
                sh(base + ["shell", f"mkdir -p {_shq(dest)}"], timeout=15)
            except Exception:
                pass

            total = len(paths)
            ok_cnt = fail = 0
            for i, src in enumerate(paths):
                if self._rec_cancel.is_set():
                    break
                src = str(src)
                name = Path(src).name or "unnamed"
                # 关键：远端目标必须显式写成「目录/名字」。
                # adb 自己从本地路径推导远端名时，中文名会被算成 '.' →
                # 报 remote couldn't create file: Is a directory（文件夹更惨：文件会被散落到目标目录）
                target = f"{dest}/{name}"
                ascii_name = name.isascii()
                push_to = target if ascii_name else f"{dest}/.tb_tmp_{int(time.time())}_{i}"
                self.sig.ui.emit(lambda n=name, i=i: self._rec_set_status(
                    f"推送中（{i + 1}/{total}）{n}", "info"))

                def on_pct(p, i=i, total=total):
                    overall = int((i + max(0, min(100, int(p))) / 100.0)
                                  * 100 / max(1, total))
                    self.sig.ui.emit(lambda o=overall: self._rec_progress(o))

                rc, out = self._rec_run(base + ["push", "-p", src, push_to],
                                        progress_re=_RE_PCT_BRACKET,
                                        on_pct=on_pct)
                if rc == -100:
                    break

                # 中文 / 非 ASCII 名字：先推成 ASCII 临时名，再在设备端改成真名
                if not ascii_name and rc == 0:
                    self._rec_log_async(f"  ↻ 设备端改名：{name}")
                    rc2, out2 = sh(base + ["shell",
                                           f"mv {_shq(push_to)} {_shq(target)}"],
                                   timeout=30)
                    if out2:
                        out = (out or "") + "\n" + out2
                    if rc2 != 0:
                        rc = rc2

                empty_dir = rc == 0 and "0 files pushed" in (out or "").lower()
                if empty_dir and Path(src).is_dir():
                    # 空文件夹 adb 不会在手机上建目录，手动建一个（包含只有空子目录的情况）
                    try:
                        sh(base + ["shell", f"mkdir -p {_shq(target)}"], timeout=15)
                        self._rec_log_async("  ↑ 文件夹里没有文件，只在手机上建了目录")
                    except Exception:
                        pass
                if rc == 0 and not self._rec_verified(base, target):
                    rc = 1
                    out = (out or "") + f"\n无法确认 {target} 是否已写入"

                if rc == 0:
                    ok_cnt += 1
                else:
                    fail += 1
                    self._rec_log_async("  ↑ 该项失败：" + _rec_explain(
                        out, PUSH_ERRORS, "未知错误，检查目标路径权限/空间"))
                self.sig.ui.emit(
                    lambda v=int((i + 1) * 100 / max(1, total)): self._rec_progress(v))

            if self._rec_stop_requested or self._rec_cancel.is_set():
                self._rec_finish(f"⏹ 已停止推送（成功 {ok_cnt} / {total} 项）", "", "warn")
                return
            if fail == 0:
                self._rec_finish(f"✅ 推送完成（{ok_cnt} 项 → {dest}，已校验）",
                                 "", "ok", pct=100)
            else:
                self._rec_finish(f"⚠ 推送结束：成功 {ok_cnt} 项，失败 {fail} 项", "", "warn")
        except Exception as e:
            self._rec_finish(f"❌ 推送出错：{e}", "", "err")
        finally:
            self._rec_proc = None

    def _rec_verified(self, base, remote):
        """在手机上确认目标真的存在（避免 adb 报 rc=0 但其实没写进去）"""
        try:
            rc, out = sh(base + ["shell", f"test -e {_shq(remote)} && echo TB_OK"],
                         timeout=15)
            return rc == 0 and "TB_OK" in (out or "")
        except Exception:
            return False



def _rec_explain(text, table, fallback):
    """按关键字把 adb 的英文报错翻译成人话"""
    low = (text or "").lower()
    for key, msg in table:
        if key in low:
            return msg
    return fallback











