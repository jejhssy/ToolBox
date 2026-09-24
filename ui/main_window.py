# -*- coding: utf-8 -*-
"""主窗口 —— 侧边栏、顶部栏、日志区、主题、自动检测"""
import random, subprocess, sys, threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QTextEdit, QFrame,
    QStackedWidget, QListWidget, QListWidgetItem, QScrollArea, QSplitter,
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QFont

from core.config import (
    APP, VER, AUTHOR, CREDIT, OUT_DIR, THEMES, WIN,
)
from core.signals import Sig
from core.widgets import Card, AnimatedProgressBar, set_state
from ui.theme import build_qss, SIDEBAR_W, MENU_ITEM_H, H_BTN
from core.utils import (
    adb_dev, fb_dev, read_device_info, read_device_stats,
    detect_device_mode, MODE_LABELS, MODE_TIPS,
)

from pages.page_info import InfoPageMixin
from pages.page_flash import FlashPageMixin
from pages.page_patch import PatchPageMixin
from pages.page_files import FilesPageMixin
from pages.page_mirror import MirrorPageMixin
from pages.page_payload import PayloadPageMixin
from pages.page_root import RootPageMixin
from pages.page_recovery import RecoveryPageMixin

# 页面索引（删掉脱机修补后重排）
PAGE_INFO = 0
PAGE_FLASH = 1
PAGE_PATCH = 2
PAGE_PAYLOAD = 3
PAGE_FILES = 4
PAGE_MIRROR = 5
PAGE_ROOT = 6
PAGE_RECOVERY = 7

# 这些页面各自有内部输出面板 → 隐藏底部公共运行日志（把空间让给页面）
NO_LOG_PAGES = {PAGE_PATCH, PAGE_PAYLOAD, PAGE_MIRROR, PAGE_ROOT, PAGE_RECOVERY}

# 设备模式 → 右上角胶囊的（文字, 状态标记）
REC_MODE_UI = {
    "none":         ("未连接",        "err"),
    "system":       ("系统模式",      "ok"),
    "recovery":     ("Recovery 模式", "warn"),
    "sideload":     ("Sideload 模式", "acc"),
    "fastboot":     ("Fastboot 模式", "warn"),
    "unauthorized": ("未授权",        "warn"),
    "offline":      ("离线",          "err"),
}


class MainWin(InfoPageMixin, FlashPageMixin, PatchPageMixin,
              PayloadPageMixin,
              FilesPageMixin, MirrorPageMixin,
              RootPageMixin, RecoveryPageMixin, QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP}  ·  {VER}")
        self.resize(1260, 940)
        self.setMinimumSize(1040, 720)

        self.src = None
        self.outdir = Path(OUT_DIR)
        self.outpath = None
        self.working = False
        self.img = None
        self.theme = THEMES[0]          # 默认固定主题（🎲 可随机换）
        self.scrcpy_proc = None
        self._last_dev = "__init__"
        self._sel_name = None
        self._sel_is_dir = False
        self.file_cards = []

        self._cur_rows = []
        self._cur_ok = False
        self._cur_path = ""
        self._cur_cols = 6

        self.sig = Sig()
        self.sig.log.connect(self._lg)
        self.sig.dev.connect(self._set_dev)
        self.sig.prog.connect(self._set_prog)
        self.sig.info.connect(self._set_info)
        self.sig.files.connect(self._on_files_ready)
        self.sig.patch_ok.connect(self._ok)
        self.sig.patch_fail.connect(self._fail)
        self.sig.prog2.connect(self._set_prog2)
        self.sig.flashprog.connect(self._set_flash_prog)
        self.sig.ui.connect(self._run_ui_callback)
        self.sig.miflash_done.connect(self._miflash_finalize)
        self.sig.stats.connect(self._apply_stats)
        self.sig.recmode.connect(self._apply_rec_mode)
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout_files_grid)

        self._theme_hooks = []      # 页面注册的"主题变化"回调（自绘控件用）
        self._build()
        self._apply_theme()
        self._switch_page(0)
        self.lg(f"{APP} {VER}  ·  作者：{AUTHOR}", "info")
        self.lg(CREDIT)
        self.lg("提示：点 🎲 可以随机换主题", "info")
        self.lg("提示：连接设备后会自动读取信息和文件", "info")

        self._auto_timer = QTimer()
        self._auto_timer.timeout.connect(self._auto_check)
        self._auto_timer.start(3000)
        QTimer.singleShot(800, self._auto_check)

    def _run_ui_callback(self, callback):
        try:
            callback()
        except Exception as e:
            self.lg(f"UI 回调异常：{e}", "error")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._resize_timer.start(150)
        except Exception:
            pass

    # ==================== 布局 ====================
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(SIDEBAR_W)
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        top = QFrame()
        top.setObjectName("sideTop")
        tv = QVBoxLayout(top)
        tv.setContentsMargins(16, 16, 16, 14)
        tv.setSpacing(3)
        title = QLabel("⚡  一键工具箱")
        title.setObjectName("sideTitle")
        sub = QLabel(f"{VER}   ·   {AUTHOR}")
        sub.setObjectName("sideSub")
        tv.addWidget(title)
        tv.addWidget(sub)
        sv.addWidget(top)

        self.menu = QListWidget()
        self.menu.setObjectName("sideMenu")
        self.menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        items = [
            ("📱  主页", "info"),
            ("📦  常规镜像刷入", "flash"),
            ("🔧  文件修补", "patch"),
            ("🧩  Payload 可视化", "payload"),
            ("📂  文件传输", "files"),
            ("🖥  投屏", "mirror"),
            ("🛡  临时提权", "root"),
            ("🛠  Recovery 工具", "recovery"),
        ]
        for text, key in items:
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setSizeHint(QSize(0, MENU_ITEM_H))
            self.menu.addItem(it)
        self.menu.currentRowChanged.connect(self._switch_page)
        sv.addWidget(self.menu, 1)

        bottom = QFrame()
        bottom.setObjectName("sideBottom")
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(10, 10, 10, 10)
        bv.setSpacing(8)

        self.rBtn = QPushButton("🎲  随机主题")
        self.rBtn.setObjectName("sideBtn")
        self.rBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rBtn.setFixedHeight(40)
        self.rBtn.clicked.connect(self.reroll)
        bv.addWidget(self.rBtn)

        self.cmdBtn = QPushButton("⌨  CMD 命令行")
        self.cmdBtn.setObjectName("sideBtn2")
        self.cmdBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cmdBtn.setFixedHeight(40)
        self.cmdBtn.clicked.connect(self.open_cmd)
        bv.addWidget(self.cmdBtn)

        ver = QLabel(f"作者：{AUTHOR}")
        ver.setObjectName("sideVer")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setWordWrap(True)
        ver.setFixedHeight(28)
        bv.addWidget(ver)

        sv.addWidget(bottom)
        outer.addWidget(self.sidebar)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(20, 16, 20, 14)
        rv.setSpacing(12)
        outer.addWidget(right, 1)

        topBar = QFrame()
        topBar.setObjectName("topBar")
        hd = QHBoxLayout(topBar)
        hd.setContentsMargins(18, 11, 14, 11)
        hd.setSpacing(12)
        self.pageTitle = QLabel("手机信息 & 重启")
        self.pageTitle.setObjectName("pageTitle")
        hd.addWidget(self.pageTitle)
        hd.addStretch()
        self.devLabel = QLabel("● 未检测")
        self.devLabel.setObjectName("devStatus")
        hd.addWidget(self.devLabel)
        # 设备模式胶囊（系统 / Recovery / Sideload / Fastboot），由 3 秒一次的自动检测刷新
        self.modeLabel = QLabel("")
        self.modeLabel.setObjectName("modeChip")
        self.modeLabel.setToolTip(
            "设备当前模式（系统 / Recovery / Sideload / Fastboot）\n"
            "每 3 秒自动检测一次；Sideload、导出、推送是否可用看这里")
        self.modeLabel.setVisible(False)
        hd.addWidget(self.modeLabel)
        self.chkTopBtn = QPushButton("🔄  检测设备")
        self.chkTopBtn.setObjectName("ghost")
        self.chkTopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chkTopBtn.setFixedHeight(H_BTN)
        self.chkTopBtn.clicked.connect(self.do_check)
        hd.addWidget(self.chkTopBtn)
        rv.addWidget(topBar)

        self.stack = QStackedWidget()
        for builder in (self._page_info, self._page_flash, self._page_patch,
                        self._page_payload,
                        self._page_files, self._page_mirror, self._page_root,
                        self._page_recovery):
            self.stack.addWidget(self._wrap_page(builder()))

        self.logCard = Card()
        ll = QVBoxLayout(self.logCard)
        ll.setContentsMargins(18, 14, 18, 14)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        t = QLabel("📜  运行日志")
        t.setObjectName("cardTitle")
        lh.addWidget(t)
        lh.addStretch()
        self.clrBtn = QPushButton("清空")
        self.clrBtn.setObjectName("smallGhost")
        self.clrBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clrBtn.setFixedHeight(30)
        self.clrBtn.clicked.connect(lambda: self.logbox.clear())
        lh.addWidget(self.clrBtn)
        ll.addLayout(lh)
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setObjectName("log")
        self.logbox.setFont(QFont("Consolas", 9))
        self.logbox.setMinimumHeight(84)
        ll.addWidget(self.logbox)

        # 页面区 / 日志区 可拖拽分配高度
        self.vsplit = QSplitter(Qt.Orientation.Vertical)
        self.vsplit.setObjectName("mainSplit")
        self.vsplit.setChildrenCollapsible(False)
        self.vsplit.setHandleWidth(8)
        self.vsplit.addWidget(self.stack)
        self.vsplit.addWidget(self.logCard)
        self.vsplit.setStretchFactor(0, 4)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setSizes([560, 190])
        rv.addWidget(self.vsplit, 1)

        ft = QLabel(f"⚠  刷机有风险 · 本工具由 {AUTHOR} 制作，仅供学习研究")
        ft.setObjectName("footer")
        ft.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rv.addWidget(ft)

    def _wrap_page(self, widget):
        """内容比可视区域高时自动出现滚动条（本来就有滚动区的页面不重复包）"""
        try:
            if isinstance(widget, QScrollArea) or widget.findChild(QScrollArea):
                return widget
        except Exception:
            return widget
        sa = QScrollArea()
        sa.setObjectName("pageScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setWidget(widget)
        return sa

    def _switch_page(self, idx):
        if idx < 0:
            return
        self.stack.setCurrentIndex(idx)
        # 让侧边栏同步高亮（启动时 currentRow 为 -1，否则没有任何激活项）
        try:
            if 0 <= idx < self.menu.count() and self.menu.currentRow() != idx:
                self.menu.setCurrentRow(idx)
        except Exception:
            pass
        titles = [
            "手机信息 & 重启",
            "常规镜像刷入",
            "Preloader 修补",
            "Payload 可视化",
            "文件传输 & 预览",
            "投屏（scrcpy）",
            "临时提权",
            "Recovery 工具 & Sideload",
        ]
        self.pageTitle.setText(titles[idx] if 0 <= idx < len(titles) else "")

        # 这些页面自带输出面板 → 隐藏底部公共日志，空间给页面
        try:
            want_log = idx not in NO_LOG_PAGES
            if self.logCard.isVisible() != want_log:
                self.logCard.setVisible(want_log)
        except Exception:
            pass

        if idx == PAGE_FILES:
            QTimer.singleShot(0, self._relayout_files_grid)
        elif idx == PAGE_FLASH:
            # 进常规刷入页时自动看一眼 root 状态（有 ADB 设备才真正去探测）
            QTimer.singleShot(0, self._maint_auto_probe)
        elif idx == PAGE_RECOVERY:
            # 进 Recovery 页时立刻刷新一次设备模式（之后由 3 秒轮询维护）
            QTimer.singleShot(0, self._auto_check)

    def _label(self, text):
        lb = QLabel(text)
        lb.setObjectName("label")
        return lb

    # ==================== 主题 ====================
    def reroll(self):
        self.theme = random.choice(THEMES)
        self._apply_theme()
        self.lg("🎲 已随机换主题", "info")

    def _apply_theme(self):
        c = self.theme
        qss = build_qss(c)
        self.setStyleSheet(qss)
        if hasattr(self, "dot"):
            self.dot.setStyleSheet(f"color: {self._dotc(self.sTxt.text())}; font-size: 16pt;")
        for k in getattr(self, "infoLabels", {}):
            lbl = self.infoLabels[k]
            self._apply_info_color(k, lbl)
        for cb in list(getattr(self, "_theme_hooks", [])):
            try:
                cb()
            except Exception:
                pass
        # 所有动画进度条统一跟主题（各页面无需自己处理）
        for bar in self.findChildren(AnimatedProgressBar):
            try:
                bar.set_colors(c["acc"], c["log"], c["text"],
                               c["line"], c["acc2"])
            except Exception:
                pass

    # ==================== 日志 ====================
    def lg(self, msg, level="plain"):
        self.sig.log.emit(msg, level)

    def _lg(self, msg, level):
        ts = datetime.now().strftime("%H:%M:%S")
        c = self.theme
        colors = {"info": c["acc2"], "ok": c["ok"], "warn": c["warn"],
                  "error": c["err"], "root": c["err"], "plain": c["text"]}
        col = colors.get(level, c["text"])
        body = (str(msg).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
        html = (f'<span style="color:{c["dim"]}">[{ts}]</span> '
                f'<span style="color:{col}">{body}</span>')
        self.logbox.append(html)
        sb = self.logbox.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ==================== 打开 CMD ====================
    def open_cmd(self):
        try:
            if WIN:
                try:
                    subprocess.Popen(["wt", "cmd", "/k",
                                      "echo === ToolBox CMD === & "
                                      "echo. & adb devices & echo."],
                                     shell=True)
                    self.lg("✓ 已打开 Windows Terminal", "ok")
                    return
                except Exception:
                    pass
                subprocess.Popen(
                    'start cmd /k "echo === ToolBox CMD === && '
                    'echo. && adb devices && echo."',
                    shell=True)
                self.lg("✓ 已打开 CMD 命令行（已自动执行 adb devices）", "ok")
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", "Terminal"])
                self.lg("✓ 已打开 Terminal", "ok")
            else:
                for term in ("x-terminal-emulator", "gnome-terminal",
                             "konsole", "xterm"):
                    try:
                        subprocess.Popen([term])
                        self.lg(f"✓ 已打开 {term}", "ok")
                        return
                    except Exception:
                        continue
                self.lg("❌ 未找到可用的终端程序", "error")
        except Exception as e:
            self.lg(f"❌ 打开命令行失败：{e}", "error")

    # ==================== 自动检测 ====================
    def _apply_rec_mode(self, mode, serial, detail=""):
        """接收后台检测出的设备模式：更新右上角胶囊 + 同步给 Recovery 工具页"""
        self.rec_mode = mode
        text, state = REC_MODE_UI.get(mode, REC_MODE_UI["none"])
        try:
            set_state(self.modeLabel, state)
            self.modeLabel.setText(text)
            self.modeLabel.setVisible(True)
            tip = f"设备模式：{text}"
            if serial:
                tip += f"\n序列号：{serial}"
            hint = MODE_TIPS.get(mode)
            if hint:
                tip += f"\n{hint}"
            if detail:
                tip += f"\n检测依据：{detail}"
            self.modeLabel.setToolTip(tip)
        except Exception:
            pass
        if getattr(self, "_last_rec_mode", None) != mode:
            self._last_rec_mode = mode
            self.sig.log.emit(
                f"设备模式：{text}" + (f"（{serial}）" if serial else ""),
                {"ok": "ok", "warn": "warn", "err": "error"}.get(state, "info"))
        try:
            self._rec_sync_mode(mode, serial)
        except Exception:
            pass

    def _auto_check(self):
        def w():
            mode, serial, detail = detect_device_mode()
            self.sig.recmode.emit(mode, serial, detail)

            adb_ready = mode in ("system", "recovery")
            if adb_ready:
                cur = f"adb:{mode}:{serial}"
            elif mode in ("sideload", "unauthorized", "offline"):
                cur = f"{mode}:{serial}"
            elif mode == "fastboot":
                cur = f"fastboot:{serial}"
            else:
                cur = None

            # ---------- 每次都刷新电量/存储/内存（有 adb shell 才有意义）----------
            if adb_ready:
                try:
                    self.sig.stats.emit(read_device_stats())
                except Exception:
                    pass
            else:
                self.sig.stats.emit({
                    "battery_level": None, "battery_status": "—",
                    "storage_used_pct": None, "storage_used": "—",
                    "storage_total": "—",
                    "mem_used_pct": None, "mem_used": "—", "mem_total": "—",
                })

            # ---------- 设备状态变化时更新其它信息 ----------
            if cur == self._last_dev:
                return
            self._last_dev = cur

            if cur is None:
                self.sig.dev.emit("● 未检测到", self.theme["warn"])
                self.sig.log.emit("设备已断开", "warn")
                for k in self.infoLabels:
                    self.sig.info.emit(k, "—")
                QTimer.singleShot(0, self.do_browse_phone)
                return

            if mode == "system":
                self.sig.dev.emit(f"● ADB: {serial}", self.theme["ok"])
                self.sig.log.emit(f"✓ 检测到设备（系统模式）：{serial}", "ok")
                QTimer.singleShot(200, self.do_browse_phone)
            elif mode == "recovery":
                self.sig.dev.emit(f"● Recovery: {serial}", self.theme["warn"])
                self.sig.log.emit(f"✓ 检测到设备（Recovery 模式）：{serial}", "ok")
                QTimer.singleShot(200, self.do_browse_phone)
            elif mode == "sideload":
                self.sig.dev.emit(f"● Sideload: {serial}", self.theme["ok"])
                self.sig.log.emit(f"✓ 设备已进入 Sideload 待机：{serial}", "ok")
            elif mode in ("unauthorized", "offline"):
                self.sig.dev.emit(f"● {MODE_LABELS.get(mode, '未就绪')}: {serial}",
                                  self.theme["warn"])
                self.sig.log.emit(
                    f"⚠ 设备未就绪（{MODE_LABELS.get(mode, mode)}）："
                    "请在手机上点“允许 USB 调试”", "warn")
            else:
                self.sig.dev.emit(f"● FB: {serial}", self.theme["ok"])
                self.sig.log.emit(f"✓ 检测到设备（Fastboot）：{serial}", "ok")

            info, _kind = read_device_info()
            for k, v in info.items():
                if k in self.infoLabels:
                    self.sig.info.emit(k, v)
        threading.Thread(target=w, daemon=True).start()
