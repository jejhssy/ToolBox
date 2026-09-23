# -*- coding: utf-8 -*-
"""主窗口 —— 侧边栏、顶部栏、日志区、主题、自动检测"""
import random, subprocess, sys, threading
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QTextEdit, QFrame,
    QStackedWidget, QListWidget, QListWidgetItem,
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QFont

from core.config import (
    APP, VER, AUTHOR, CREDIT, OUT_DIR, THEMES, WIN,
)
from core.signals import Sig
from core.widgets import Card
from core.utils import adb_dev, fb_dev, read_device_info, read_device_stats

from pages.page_info import InfoPageMixin
from pages.page_flash import FlashPageMixin
from pages.page_patch import PatchPageMixin
from pages.page_files import FilesPageMixin
from pages.page_mirror import MirrorPageMixin
from pages.page_payload import PayloadPageMixin
from pages.page_root import RootPageMixin


class MainWin(InfoPageMixin, FlashPageMixin, PatchPageMixin,
              FilesPageMixin, MirrorPageMixin, PayloadPageMixin,
              RootPageMixin, QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP}  ·  {VER}")
        self.resize(1200, 920)
        self.setMinimumSize(1080, 820)

        self.src = None
        self.outdir = Path(OUT_DIR)
        self.outpath = None
        self.working = False
        self.img = None
        self.theme = random.choice(THEMES)
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
        self.sig.payloadprog.connect(self._payload_set_prog)
        self.sig.ui.connect(self._run_ui_callback)
        self.sig.miflash_done.connect(self._miflash_finalize)
        self.sig.stats.connect(self._apply_stats)
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._relayout_files_grid)

        self._build()
        self._apply_theme()
        self._switch_page(0)
        self.lg(f"{APP} {VER}  ·  作者：{AUTHOR}", "info")

    def _run_ui_callback(self, callback):
        callback()
        self.lg(CREDIT)
        self.lg("提示：点 🎲 可以随机换主题", "info")
        self.lg("提示：连接设备后会自动读取信息和文件", "info")

        self._auto_timer = QTimer()
        self._auto_timer.timeout.connect(self._auto_check)
        self._auto_timer.start(3000)
        QTimer.singleShot(800, self._auto_check)

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
        self.sidebar.setFixedWidth(200)
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        title = QLabel("⚡ 工具")
        title.setObjectName("sideTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFixedHeight(80)
        sv.addWidget(title)

        self.menu = QListWidget()
        self.menu.setObjectName("sideMenu")
        self.menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        items = [
            ("📱   手机信息 & 重启", "info"),
            ("📦   常规镜像刷入", "flash"),
            ("🔧   Preloader 修补", "patch"),
            ("📂   文件传输 & 预览", "files"),
            ("🖥   投屏（scrcpy）", "mirror"),
            ("🧩   Payload / 链接提取", "payload"),  
            ("🛡   临时提权", "root"),
        ]
        for text, key in items:
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setSizeHint(QSize(0, 52))
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

        ver = QLabel(f"{VER}   ·   {AUTHOR}")
        ver.setObjectName("sideVer")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setFixedHeight(24)
        bv.addWidget(ver)

        sv.addWidget(bottom)
        outer.addWidget(self.sidebar)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(24, 20, 24, 20)
        rv.setSpacing(14)
        outer.addWidget(right, 1)

        hd = QHBoxLayout()
        self.pageTitle = QLabel("手机信息 & 重启")
        self.pageTitle.setObjectName("pageTitle")
        hd.addWidget(self.pageTitle)
        hd.addStretch()
        self.devLabel = QLabel("● 未检测")
        self.devLabel.setObjectName("devStatus")
        hd.addWidget(self.devLabel)
        self.chkTopBtn = QPushButton("🔄  检测设备")
        self.chkTopBtn.setObjectName("ghost")
        self.chkTopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chkTopBtn.setFixedHeight(36)
        self.chkTopBtn.clicked.connect(self.do_check)
        hd.addWidget(self.chkTopBtn)
        rv.addLayout(hd)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_info())
        self.stack.addWidget(self._page_flash())
        self.stack.addWidget(self._page_patch())
        self.stack.addWidget(self._page_files())
        self.stack.addWidget(self._page_mirror())
        self.stack.addWidget(self._page_payload()) 
        self.stack.addWidget(self._page_root())
        rv.addWidget(self.stack, 1)

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
        self.clrBtn.setObjectName("ghost")
        self.clrBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clrBtn.setFixedHeight(30)
        self.clrBtn.clicked.connect(lambda: self.logbox.clear())
        lh.addWidget(self.clrBtn)
        ll.addLayout(lh)
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setObjectName("log")
        self.logbox.setFont(QFont("Consolas", 9))
        self.logbox.setFixedHeight(140)
        ll.addWidget(self.logbox)
        rv.addWidget(self.logCard)

        ft = QLabel(f"⚠  刷机有风险 · 本工具由 {AUTHOR} 制作，仅供学习研究")
        ft.setObjectName("footer")
        ft.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rv.addWidget(ft)

    def _switch_page(self, idx):
        if idx < 0:
            return
        self.stack.setCurrentIndex(idx)
        titles = [
            "手机信息 & 重启",
            "常规镜像刷入",
            "Preloader 修补",
            "文件传输 & 预览",
            "投屏（scrcpy）",
            "Payload / 链接提取",           # ← 新增
            "临时提权",
        ]
        self.pageTitle.setText(titles[idx] if 0 <= idx < len(titles) else "")
        if idx == 3:
            QTimer.singleShot(0, self._relayout_files_grid)

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
        qss = f"""
        QMainWindow, QWidget {{
            background: {c['bg']};
            color: {c['text']};
            font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
            font-size: 10pt;
        }}
        QLabel {{ background: transparent; }}
        QFrame#sidebar {{ background: {c['menu']}; border: none; }}
        QLabel#sideTitle {{
            font-size: 16pt; font-weight: bold; color: {c['text']};
            background: {c['menu']}; border: none;
        }}
        QFrame#sideBottom {{
            background: {c['menu']};
            border-top: 1px solid {c['line']};
        }}
        QLabel#sideVer {{ color: {c['dim']}; font-size: 8pt; background: {c['menu']}; }}
        QListWidget#sideMenu {{
            background: {c['menu']}; border: none; outline: 0;
                QTextEdit#rootOutput QScrollBar:vertical,
                QTextEdit#rootOutput QScrollBar::handle:vertical,
            color: {c['menuTxt']}; font-size: 11pt; padding: 8px 0;
        }}
        QListWidget#sideMenu::item {{
            padding: 12px 16px; margin: 2px 8px; border-radius: 8px;
            color: {c['menuTxt']};
        }}
        QListWidget#sideMenu::item:hover {{
            background: {c['panel']}; color: {c['text']};
        }}
        QListWidget#sideMenu::item:selected {{
            background: {c['menuSel']}; color: white; font-weight: bold;
        }}
        QPushButton#sideBtn {{
            background: {c['panel2']}; color: {c['text']};
            border: 1px solid {c['line']}; border-radius: 8px;
            font-weight: bold;
        }}
        QPushButton#sideBtn:hover {{
            background: {c['acc']}; color: white; border: 1px solid {c['acc']};
                QTextEdit#rootOutput QScrollBar:horizontal,
        }}
        QPushButton#sideBtn2 {{
            background: {c['panel']}; color: {c['text']};
            border: 1px solid {c['line']}; border-radius: 8px;
            font-weight: bold;
        }}
        QPushButton#sideBtn2:hover {{
            background: {c['acc2']}; color: white; border: 1px solid {c['acc2']};
        }}
        QLabel#pageTitle {{ font-size: 18pt; font-weight: bold; color: {c['text']}; }}
        QLabel#devStatus {{ color: {c['dim']}; font-weight: bold; }}
        QLabel#cardTitle {{ font-size: 12pt; font-weight: bold; color: {c['text']}; }}
        QLabel#desc {{ color: {c['dim']}; }}
        QLabel#label {{ color: {c['text']}; font-weight: bold; }}
        QLabel#info {{ color: {c['dim']}; }}
        QLabel#infoKey {{ color: {c['dim']}; font-weight: normal; }}
        QLabel#infoVal {{ color: {c['text']}; font-weight: bold; }}
        QLabel#status {{ color: {c['text']}; font-weight: bold; }}
        QLabel#footer {{ color: {c['err']}; padding: 4px; }}
        QFrame#card {{
            background: {c['panel']};
            border: 1px solid {c['line']};
            border-radius: 12px;
        }}
        QLineEdit {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 8px; padding: 8px 12px;
            color: {c['text']}; selection-background-color: {c['acc']};
        }}
        QLineEdit:focus {{ border: 1px solid {c['acc']}; }}
        QComboBox {{
            background: {c['log']};
            border: 1px solid {c['line']};
            border-radius: 8px;
            padding: 6px 30px 6px 12px;
            color: {c['text']};
            min-height: 22px;
            selection-background-color: {c['acc']};
        }}
        QComboBox:hover, QComboBox:focus, QComboBox:on {{
            border: 1px solid {c['acc']};
            background: {c['panel2']};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 26px;
            border-left: 1px solid {c['line']};
            border-top-right-radius: 8px;
            border-bottom-right-radius: 8px;
            background: {c['panel2']};
        }}
        QComboBox::drop-down:hover {{ background: {c['acc']}; }}
        QComboBox::down-arrow {{
            image: none;
            width: 0px; height: 0px;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 6px solid {c['acc2']};
            margin-right: 8px;
        }}
        QComboBox::down-arrow:on {{
            border-top: none;
            border-bottom: 6px solid white;
        }}
        QComboBox QAbstractItemView {{
            background: {c['panel2']}; color: {c['text']};
            border: 1px solid {c['acc']}; border-radius: 8px;
            padding: 6px; outline: 0;
            selection-background-color: {c['acc']};
            selection-color: white;
        }}
        QComboBox QAbstractItemView::item {{
            min-height: 32px; padding: 4px 12px;
            border-radius: 6px; color: {c['text']};
        }}
        QComboBox QAbstractItemView::item:hover {{
            background: {c['line']}; color: {c['text']};
        }}
        QComboBox QAbstractItemView::item:selected {{
            background: {c['acc']}; color: white;
        }}
        QSpinBox {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 8px; padding: 6px 10px;
            color: {c['text']}; min-height: 22px;
        }}
        QSpinBox:hover, QSpinBox:focus {{ border: 1px solid {c['acc']}; }}
        QTextEdit#log {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 8px; padding: 8px; color: {c['text']};
        }}
        QTextEdit#rootOutput {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 8px; padding: 10px; color: {c['err']};
        }}
        QListWidget#payloadList {{
            background: {c['log']};
            border: 1px solid {c['line']};
            border-radius: 8px;
            padding: 6px;
            color: {c['text']};
            outline: 0;
        }}
        QListWidget#payloadList::item {{
            padding: 10px 12px;
            border-radius: 6px;
            min-height: 22px;
        }}
        QListWidget#payloadList::item:hover {{
            background: {c['panel2']};
        }}
        QTextEdit#log QScrollBar:vertical,
        QTextEdit#rootOutput QScrollBar:vertical,
        QListWidget#payloadList QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 3px 2px 3px 0;
        }}
        QTextEdit#log QScrollBar::handle:vertical,
        QTextEdit#rootOutput QScrollBar::handle:vertical,
        QListWidget#payloadList QScrollBar::handle:vertical {{
            background: {c['acc2']};
            min-height: 48px;
            border: 2px solid transparent;
            border-radius: 5px;
        }}
        QTextEdit#log QScrollBar::handle:vertical:hover,
        QTextEdit#rootOutput QScrollBar::handle:vertical:hover,
        QListWidget#payloadList QScrollBar::handle:vertical:hover {{
            background: {c['acc']};
        }}
        QTextEdit#log QScrollBar::add-page:vertical,
        QTextEdit#log QScrollBar::sub-page:vertical,
        QTextEdit#rootOutput QScrollBar::add-page:vertical,
        QTextEdit#rootOutput QScrollBar::sub-page:vertical,
        QListWidget#payloadList QScrollBar::add-page:vertical,
        QListWidget#payloadList QScrollBar::sub-page:vertical {{
            background: transparent;
        }}
        QTextEdit#log QScrollBar::add-line:vertical,
        QTextEdit#log QScrollBar::sub-line:vertical,
        QTextEdit#rootOutput QScrollBar::add-line:vertical,
        QTextEdit#rootOutput QScrollBar::sub-line:vertical,
        QListWidget#payloadList QScrollBar::add-line:vertical,
        QListWidget#payloadList QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QTextEdit#log QScrollBar:horizontal,
        QTextEdit#rootOutput QScrollBar:horizontal,
        QListWidget#payloadList QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
            margin: 0 3px 2px 3px;
        }}
        QTextEdit#log QScrollBar::handle:horizontal,
        QTextEdit#rootOutput QScrollBar::handle:horizontal,
        QListWidget#payloadList QScrollBar::handle:horizontal {{
            background: {c['acc2']};
            min-width: 48px;
            border: 2px solid transparent;
            border-radius: 5px;
        }}
        QTextEdit#log QScrollBar::handle:horizontal:hover,
        QTextEdit#rootOutput QScrollBar::handle:horizontal:hover,
        QListWidget#payloadList QScrollBar::handle:horizontal:hover {{
            background: {c['acc']};
        }}
        QTextEdit#log QScrollBar::add-page:horizontal,
        QTextEdit#log QScrollBar::sub-page:horizontal,
        QTextEdit#rootOutput QScrollBar::add-page:horizontal,
        QTextEdit#rootOutput QScrollBar::sub-page:horizontal,
        QListWidget#payloadList QScrollBar::add-page:horizontal,
        QListWidget#payloadList QScrollBar::sub-page:horizontal {{
            background: transparent;
        }}
        QScrollArea#fileScroll {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 10px;
        }}
        QScrollArea#pageScroll {{ background: transparent; border: none; }}
        QScrollArea#pageScroll > QWidget > QWidget {{ background: transparent; }}
        QWidget#fileGrid {{ background: {c['log']}; }}
        QFrame#fileCard {{
            background: {c['panel']};
            border: 1px solid {c['line']};
            border-radius: 10px;
        }}
        QFrame#fileCard:hover {{
            background: {c['panel2']};
            border: 1px solid {c['acc2']};
        }}
        QFrame#fileCard[selected="true"] {{
            background: {c['panel2']};
            border: 2px solid {c['acc']};
        }}
        QLabel#fileIcon {{ font-size: 22pt; background: transparent; }}
        QLabel#fileName {{ color: {c['text']}; font-size: 9pt; background: transparent; }}
        QLabel#fileSize {{ color: {c['dim']}; font-size: 8pt; background: transparent; }}
        QLabel#payloadSelectionInfo {{
            color: {c['dim']}; background: transparent;
            padding: 2px 8px;
        }}
        QLineEdit#payloadSearch {{
            background: {c['log']}; color: {c['text']};
            border: 1px solid {c['line']}; border-radius: 7px;
            padding: 4px 9px;
        }}
        QLineEdit#payloadSearch:focus {{ border: 1px solid {c['acc']}; }}
        QPushButton {{ border-radius: 9px; padding: 8px 18px; border: none; font-weight: bold; }}
        QPushButton#primary {{ background: {c['acc']}; color: white; }}
        QPushButton#primary:hover {{ background: {c['acc2']}; }}
        QPushButton#primary:disabled {{ background: {c['line']}; color: {c['dim']}; }}
        QPushButton#secondary {{
            background: {c['panel2']}; color: {c['text']};
            border: 1px solid {c['line']};
        }}
        QPushButton#secondary:hover {{ background: {c['line']}; border: 1px solid {c['acc']}; }}
        QPushButton#ghost {{
            background: transparent; color: {c['text']};
            border: 1px solid {c['line']};
        }}
        QPushButton#ghost:hover {{ background: {c['panel2']}; border: 1px solid {c['acc']}; }}
        QProgressBar {{
            background: {c['log']}; border: 1px solid {c['line']};
            border-radius: 5px;
        }}
        QProgressBar::chunk {{ background: {c['acc']}; border-radius: 5px; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; }}
        QScrollBar::handle:vertical {{
            background: {c['line']}; border-radius: 4px; min-height: 30px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {c['acc']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """
        self.setStyleSheet(qss)
        if hasattr(self, "dot"):
            self.dot.setStyleSheet(f"color: {self._dotc(self.sTxt.text())}; font-size: 16pt;")
        for k in getattr(self, "infoLabels", {}):
            lbl = self.infoLabels[k]
            self._apply_info_color(k, lbl)

    # ==================== 日志 ====================
    def lg(self, msg, level="plain"):
        self.sig.log.emit(msg, level)

    def _lg(self, msg, level):
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {"info": "#60a5fa", "ok": self.theme["ok"],
                  "warn": self.theme["warn"], "error": self.theme["err"],
              "root": "#ff2020",
                  "plain": self.theme["text"]}
        col = colors.get(level, self.theme["text"])
        html = f'<span style="color:#64748b">[{ts}]</span> ' \
               f'<span style="color:{col}">{msg}</span>'
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
    def _auto_check(self):
        def w():
            ds, _ = adb_dev()
            if ds:
                cur = f"adb:{ds[0]}"
            else:
                fs, _ = fb_dev()
                cur = f"fastboot:{fs[0]}" if fs else None

            # ---------- 每次都刷新电量/存储/内存 ----------
            if ds:
                try:
                    stats = read_device_stats()
                    self.sig.stats.emit(stats)
                except Exception:
                    pass
            else:
                # 没有 ADB 设备（拔线 / fastboot）→ 清空卡片
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

            kind, name = cur.split(":", 1)
            if kind == "adb":
                self.sig.dev.emit(f"● ADB: {name}", self.theme["ok"])
                self.sig.log.emit(f"✓ 检测到设备（ADB）：{name}", "ok")
                QTimer.singleShot(200, self.do_browse_phone)
            else:
                self.sig.dev.emit(f"● FB: {name}", self.theme["ok"])
                self.sig.log.emit(f"✓ 检测到设备（Fastboot）：{name}", "ok")

            info, mode = read_device_info()
            for k, v in info.items():
                if k in self.infoLabels:
                    self.sig.info.emit(k, v)
        threading.Thread(target=w, daemon=True).start()