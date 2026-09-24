# -*- coding: utf-8 -*-
"""页面 0：手机信息 & 重启"""
import time, traceback, threading
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea, QFrame, QMessageBox, QProgressBar,
)
from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QPainter, QColor, QPen
from core.widgets import Card, set_state, state_from_color
from core.utils import (
    adb_dev, fb_dev, sh, read_device_info, read_device_stats, adb_shell_dev,
)


class CircleProgress(QWidget):
    """圆形进度圈：圆内显示百分比 + 容量，名称放圆外"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(120, 120)
        self._pct = 0.0
        self._color = "#4f8cff"
        self._base = "#2a3555"      # 底环颜色
        self._txt = "#e8ecf7"       # 百分比颜色
        self._dim = "#8892b0"       # 小字颜色
        self._sub = ""              # 容量文字（圆内下方小字）

    def set_value(self, pct, color, base_color, text_color, sub="",
                  dim_color=None):
        self._pct = max(0.0, min(100.0, float(pct)))
        self._color = color
        self._base = base_color
        self._txt = text_color
        if dim_color:
            self._dim = dim_color
        self._sub = sub or ""
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(10, 10, -10, -10)

        # 底环
        pen = QPen(QColor(self._base))
        pen.setWidth(9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)

        # 进度环
        pen.setColor(QColor(self._color))
        p.setPen(pen)
        span = int(-self._pct / 100 * 360 * 16)
        p.drawArc(rect, 90 * 16, span)

        # 圆内：百分比
        f = p.font()
        f.setPointSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self._txt))
        if self._sub:
            p.drawText(
                QRect(rect.left(), rect.top() + 26, rect.width(), 26),
                Qt.AlignmentFlag.AlignCenter, f"{self._pct:.1f}%")
            # 圆内：容量小字
            f.setPointSize(6)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(self._dim))
            p.drawText(
                QRect(rect.left(), rect.top() + 54, rect.width(), 16),
                Qt.AlignmentFlag.AlignCenter, self._sub)
        else:
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{self._pct:.1f}%")
        p.end()


class InfoPageMixin:

    def _page_info(self):
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        topRow = QHBoxLayout()
        topRow.setSpacing(14)

        leftBox = QVBoxLayout()
        leftBox.setSpacing(8)

        t1 = QLabel("📱  设备信息")
        t1.setObjectName("cardTitle")
        leftBox.addWidget(t1)
        d1 = QLabel("连接设备后自动读取，支持 ADB / Fastboot 模式")
        d1.setObjectName("desc")
        leftBox.addWidget(d1)

        infoCard = Card()
        ig = QGridLayout(infoCard)
        ig.setContentsMargins(14, 8, 14, 8)     # 上下 12 → 8
        ig.setHorizontalSpacing(14)             # 16 → 14
        ig.setVerticalSpacing(2)                # 6 → 2

        self.infoLabels = {}

        pairs = [
            ("设备状态",   "state",        "A/B 分区",   "slot"),
            ("连接类型",   "mode",         "CPU 厂家",   "soc_vendor"),
            ("设备序列号", "serial",       "CPU 代号",   "platform"),
            ("设备名称",   "model",        "CPU 名称",   "soc_model"),
            ("设备代号",   "device",       "操作系统",   "host_os"),
            ("安卓版本",   "android",      "SELinux",    "selinux"),
            ("解锁状态",   "unlock",       "版本信息",   "incremental"),
            ("内核版本",   "kernel",       "构建日期",   "build_date"),
        ]

        def add_cell(row, col, label, key):
            lb = QLabel(label)
            lb.setObjectName("infoKey")
            lb.setMinimumHeight(18)
            vl = QLabel("—")
            vl.setObjectName("infoVal")
            vl.setMinimumHeight(18)
            vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ig.addWidget(lb, row, col)
            ig.addWidget(vl, row, col + 1)
            self.infoLabels[key] = vl

        for i, (l1, k1, l2, k2) in enumerate(pairs):
            add_cell(i, 0, l1, k1)
            add_cell(i, 2, l2, k2)

        ig.setColumnStretch(1, 1)
        ig.setColumnStretch(2, 0)
        ig.setColumnStretch(3, 1)
        ig.setColumnMinimumWidth(2, 20)
        ig.setColumnMinimumWidth(3, 110)

        leftBox.addWidget(infoCard)

        btnRow = QHBoxLayout()
        self.infoBtn = QPushButton("🔄  重新读取信息")
        self.infoBtn.setObjectName("primary")
        self.infoBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.infoBtn.setFixedHeight(38)
        self.infoBtn.setFixedWidth(180)
        self.infoBtn.clicked.connect(self.do_read_info)
        btnRow.addWidget(self.infoBtn)
        btnRow.addStretch()
        leftBox.addLayout(btnRow)
        leftBox.addStretch()

        leftWrap = QWidget()
        leftWrap.setLayout(leftBox)
        topRow.addWidget(leftWrap, 3)

        rightBox = QVBoxLayout()
        rightBox.setSpacing(10)

        # ---------- 电池卡片 ----------
        batCard = Card()
        batCard.setMinimumWidth(260)
        bv = QVBoxLayout(batCard)
        bv.setContentsMargins(18, 14, 18, 14)
        bv.setSpacing(10)

        batTitle = QLabel("🔋  电池状态")
        batTitle.setObjectName("cardTitle")
        bv.addWidget(batTitle)

        self.batteryBar = QProgressBar()
        self.batteryBar.setObjectName("batteryBar")
        self.batteryBar.setRange(0, 100)
        self.batteryBar.setValue(0)
        self.batteryBar.setFixedHeight(46)
        self.batteryBar.setTextVisible(True)
        self.batteryBar.setFormat("%p%")
        bv.addWidget(self.batteryBar)

        self.batteryStatus = QLabel("—")
        self.batteryStatus.setObjectName("desc")
        self.batteryStatus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bv.addWidget(self.batteryStatus)

        rightBox.addWidget(batCard)

        # ---------- 存储 & 内存卡片 ----------
        memCard = Card()
        memCard.setMinimumWidth(260)
        mv = QVBoxLayout(memCard)
        mv.setContentsMargins(18, 14, 18, 14)
        mv.setSpacing(10)

        memTitle = QLabel("💾  存储 & 内存")
        memTitle.setObjectName("cardTitle")
        mv.addWidget(memTitle)

        circles = QHBoxLayout()
        circles.setSpacing(10)

        storageBox = QVBoxLayout()
        storageBox.setSpacing(4)
        self.storageCircle = CircleProgress()
        storageBox.addWidget(self.storageCircle,
                             alignment=Qt.AlignmentFlag.AlignCenter)
        self.storageLabel = QLabel("内部存储")
        self.storageLabel.setObjectName("desc")
        self.storageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        storageBox.addWidget(self.storageLabel)
        circles.addLayout(storageBox)

        memBox = QVBoxLayout()
        memBox.setSpacing(4)
        self.memCircle = CircleProgress()
        memBox.addWidget(self.memCircle,
                         alignment=Qt.AlignmentFlag.AlignCenter)
        self.memLabel = QLabel("运行内存")
        self.memLabel.setObjectName("desc")
        self.memLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        memBox.addWidget(self.memLabel)
        circles.addLayout(memBox)

        mv.addLayout(circles)
        rightBox.addWidget(memCard)

        rightBox.addStretch()

        rightWrap = QWidget()
        rightWrap.setLayout(rightBox)
        topRow.addWidget(rightWrap, 2)

        v.addLayout(topRow)

        t2 = QLabel("🔄  重启设备")
        t2.setObjectName("cardTitle")
        v.addWidget(t2)
        d2 = QLabel("让设备重启到指定模式（需要 ADB 或 Fastboot 连接）")
        d2.setObjectName("desc")
        v.addWidget(d2)

        rebootCard = QFrame()
        rebootCard.setObjectName("card")
        rebootCard.setMinimumHeight(78)
        rv2 = QHBoxLayout(rebootCard)
        rv2.setContentsMargins(16, 14, 16, 14)
        rv2.setSpacing(10)

        self.rbSys = QPushButton("🚀  系统")
        self.rbRec = QPushButton("🔧  Recovery")
        self.rbBL = QPushButton("⚡  Bootloader")
        self.rbFB = QPushButton("📲  Fastbootd")
        self.rbSL = QPushButton("🛰  Sideload")
        self.rbDl = QPushButton("⚙  深度下载")
        for b, tip in (
            (self.rbSys, "重启到系统（正常开机）"),
            (self.rbRec, "重启到 Recovery 恢复模式"),
            (self.rbBL, "重启到 Bootloader（传统 fastboot）"),
            (self.rbFB, "重启到 Fastbootd（用户空间 fastboot）"),
            (self.rbSL, "重启到 Sideload 侧载模式：给「🛠 Recovery 工具」的侧载刷机用\n"
                        "（手机在系统 / Recovery 模式下都能发这个命令）"),
            (self.rbDl, "进入深度下载模式（需解锁 BL）"),
        ):
            b.setObjectName("secondary")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(48)
            b.setMinimumWidth(96)
            b.setToolTip(tip)
            rv2.addWidget(b)
        rv2.addStretch()
        v.addWidget(rebootCard)

        self.rbSys.clicked.connect(lambda: self.do_reboot("system"))
        self.rbRec.clicked.connect(lambda: self.do_reboot("recovery"))
        self.rbBL.clicked.connect(lambda: self.do_reboot("bootloader"))
        self.rbFB.clicked.connect(lambda: self.do_reboot("fastboot"))
        self.rbSL.clicked.connect(lambda: self.do_reboot("sideload"))
        self.rbDl.clicked.connect(lambda: self.do_reboot("download"))

        v.addStretch()

        QTimer.singleShot(300, self.do_read_stats)
        return inner

    # ==================== 颜色 / 信息刷新 ====================
    def _value_color(self, key, value):
        c = self.theme
        v = (value or "").strip()
        if key == "state":
            return c["ok"] if v.startswith("已连接") else c["err"]
        if key == "mode":
            return c["acc2"]
        if key in ("serial", "device", "platform"):
            return c["acc"]
        if key in ("model", "soc_model", "android"):
            return c["warn"]
        if key in ("incremental", "build_date", "host_os"):
            return c["err"]
        if key == "kernel":
            return c["ok"]
        if key == "unlock":
            if "解锁" in v and "锁定" not in v:
                return c["ok"]
            if "锁定" in v:
                return c["warn"]
            return c["dim"]
        if key in ("slot", "soc_vendor"):
            return c["ok"]
        if key == "selinux":
            return c["ok"] if "严格" in v else c["warn"]
        return c["text"]

    def _apply_info_color(self, key, lbl):
        try:
            color = self._value_color(key, lbl.text())
            lbl.setStyleSheet(f"color: {color}; font-weight: bold; background: transparent;")
        except Exception:
            pass

    def _set_info(self, key, value):
        try:
            lbl = self.infoLabels[key]
            lbl.setText(value)
            self._apply_info_color(key, lbl)
        except Exception:
            pass

    def do_read_info(self):
        self.lg("手动读取手机信息…", "info")
        def w():
            info, mode = read_device_info()
            if mode is None:
                self.sig.log.emit("❌ 未检测到设备（ADB / Fastboot 都无）", "error")
                for k in self.infoLabels:
                    self.sig.info.emit(k, "—")
                return
            if mode == "adb":
                self.sig.log.emit("✓ 通过 ADB 读取信息", "ok")
            else:
                self.sig.log.emit("✓ 通过 Fastboot 读取信息（字段有限）", "ok")
            for k, v in info.items():
                if k in self.infoLabels:
                    self.sig.info.emit(k, v)
                    self.sig.log.emit(f"{k}: {v}", "plain")
        threading.Thread(target=w, daemon=True).start()

    def do_check(self):
        self.lg("检测设备…", "info")
        self._last_dev = "__force__"
        self._auto_check()

    def _set_dev(self, txt, col):
        self.devLabel.setText(txt)
        set_state(self.devLabel, state_from_color(self.theme, col))
        
    # ==================== 电量 / 存储 / 内存 ====================
    def _apply_stats(self, stats):
        """收到 stats 信号时更新卡片（主线程）"""
        try:
            c = self.theme

            # 电池
            lvl = stats.get("battery_level")
            if lvl is None:
                self.batteryBar.setValue(0)
                self.batteryBar.setFormat("—")
                self.batteryStatus.setText("—")
            else:
                self.batteryBar.setValue(int(lvl))
                self.batteryBar.setFormat("%p%")
                if lvl >= 60:
                    color = c["ok"]
                elif lvl >= 20:
                    color = c["warn"]
                else:
                    color = c["err"]
                self.batteryBar.setStyleSheet(
                    "QProgressBar{"
                    f"background:{c['log']}; border:1px solid {c['line']};"
                    f"border-radius:8px; color:{c['text']};"
                    "font-size:14pt; font-weight:bold; text-align:center;}"
                    "QProgressBar::chunk{"
                    f"background:{color}; border-radius:7px;}}")
                self.batteryStatus.setText(stats.get("battery_status", "—"))

            # 存储
            sp = stats.get("storage_used_pct")
            if sp is None:
                self.storageCircle.set_value(0, c["acc"], c["line"], c["text"])
                self.storageLabel.setText("内部存储")
            else:
                col = c["err"] if sp >= 90 else (c["warn"] if sp >= 75 else c["acc"])
                sub = (f"{stats.get('storage_used','—')} / "
                       f"{stats.get('storage_total','—')}")
                self.storageCircle.set_value(sp, col, c["line"], c["text"], sub,
                                        c["dim"])
                self.storageLabel.setText("内部存储")

            # 内存
            mp = stats.get("mem_used_pct")
            if mp is None:
                self.memCircle.set_value(0, c["acc"], c["line"], c["text"])
                self.memLabel.setText("运行内存")
            else:
                col = c["err"] if mp >= 90 else (c["warn"] if mp >= 75 else c["acc2"])
                sub = (f"{stats.get('mem_used','—')} / "
                       f"{stats.get('mem_total','—')}")
                self.memCircle.set_value(mp, col, c["line"], c["text"], sub,
                                     c["dim"])
                self.memLabel.setText("运行内存")
        except Exception:
            pass

    def do_read_stats(self):
        """异步读取设备电量/存储/内存"""
        def w():
            s = read_device_stats()
            self.sig.stats.emit(s)
        threading.Thread(target=w, daemon=True).start()
        
    # ==================== Fastboot 模式探测 ====================
    def _fb_mode(self, dev_id):
        """查询当前 Fastboot 设备模式：'bootloader' / 'fastbootd' / 'unknown'"""
        try:
            rc, out = sh(["fastboot", "-s", dev_id, "getvar", "is-userspace"],
                         timeout=5)
            self.sig.log.emit(f"[探测] is-userspace 返回码={rc}，输出={out!r}", "plain")
            if out:
                for line in out.splitlines():
                    low = line.lower()
                    if "is-userspace" in low and ":" in line:
                        v = line.split(":", 1)[1].strip().lower()
                        if v in ("yes", "true", "1"):
                            return "fastbootd"
                        if v in ("no", "false", "0"):
                            return "bootloader"
        except Exception as e:
            self.sig.log.emit(f"[探测] 异常：{e}", "warn")
        return "unknown"

    # ==================== 重启 ====================
    def do_reboot(self, target):
        tips = {
            "system":     ("系统",         "重启到系统"),
            "recovery":   ("Recovery",     "重启到 Recovery 恢复模式"),
            "bootloader": ("Bootloader",   "重启到 Bootloader（传统 fastboot）"),
            "fastboot":   ("Fastbootd",    "重启到 Fastbootd（用户空间 fastboot）"),
            "sideload":   ("Sideload",     "重启到 Sideload 侧载模式（侧载刷机用）"),
            "download":   ("深度下载模式", "进入深度下载模式"),
        }
        name, desc = tips.get(target, (target, target))

        r = QMessageBox.question(
            self, "即将重启",
            f"即将{desc}\n\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            self.lg("已取消重启", "warn")
            return

        self.lg(f"🔘 收到重启指令：{desc}（target={target}）", "info")

        def w():
            try:
                ds, _ = adb_dev()
                if not ds:
                    # Recovery（TWRP 等）下 adb_dev() 不算数，但 adb reboot / adb shell 照样能用
                    serial = adb_shell_dev()
                    if serial:
                        ds = [serial]
                        self.sig.log.emit(
                            "检测到设备在 Recovery 模式（adb 命令同样可用）", "info")
                fs, _ = fb_dev()
                self.sig.log.emit(
                    f"当前连接：ADB={ds or '无'} / Fastboot={fs or '无'}", "plain")

                # ---------- 深度下载模式 ----------
                if target == "download":
                    if not ds:
                        self.sig.log.emit("❌ 下载模式需要在系统内（ADB）触发", "error")
                        return
                    cmd = ["adb", "-s", ds[0], "reboot", "download"]
                    self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                    rc, out = sh(cmd, timeout=15)
                    if out:
                        self.sig.log.emit(out, "plain")
                    self.sig.log.emit(
                        "✓ 已发送下载模式重启" if rc == 0 else f"❌ 失败（{rc}）",
                        "ok" if rc == 0 else "error")
                    return

                # ---------- 重启到 Sideload（侧载刷机用）----------
                if target == "sideload":
                    if ds:
                        cmd = ["adb", "-s", ds[0], "reboot", "sideload"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=15)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送 Sideload 重启（手机通常会显示 "
                            "“Now send the package…”）" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    if fs:
                        cmd = ["fastboot", "-s", fs[0], "reboot", "sideload"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=20)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送 Sideload 重启" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    self.sig.log.emit(
                        "❌ 未检测到设备（系统 / Recovery 模式都可以）", "error")
                    return

                # ---------- 重启到系统 ----------
                if target == "system":
                    if ds:
                        cmd = ["adb", "-s", ds[0], "reboot"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=15)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送重启到系统" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    if fs:
                        cmd = ["fastboot", "-s", fs[0], "reboot"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=20)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送重启到系统" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    self.sig.log.emit("❌ 未检测到设备", "error")
                    return

                # ---------- 重启到 Recovery ----------
                if target == "recovery":
                    if ds:
                        cmd = ["adb", "-s", ds[0], "reboot", "recovery"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=15)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送 Recovery 重启" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    if fs:
                        cmd = ["fastboot", "-s", fs[0], "reboot", "recovery"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=20)
                        if out:
                            self.sig.log.emit(out, "plain")
                        self.sig.log.emit(
                            "✓ 已发送 Recovery 重启" if rc == 0 else f"❌ 失败（{rc}）",
                            "ok" if rc == 0 else "error")
                        return
                    self.sig.log.emit("❌ 未检测到设备", "error")
                    return

                # ---------- Bootloader ----------
                if target == "bootloader":
                    if ds:
                        # ADB：正常切换
                        cmd = ["adb", "-s", ds[0], "reboot", "bootloader"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=15)
                        if out:
                            self.sig.log.emit(out, "plain")
                        if rc != 0:
                            self.sig.log.emit(f"❌ 失败（{rc}）", "error")
                            return
                        self.sig.log.emit("✓ 已发送，等待进入 Bootloader …", "ok")
                        for _ in range(20):
                            time.sleep(2)
                            f2, _ = fb_dev()
                            if f2:
                                self.sig.log.emit(f"✓ 已进入 Fastboot：{f2[0]}", "ok")
                                self.sig.dev.emit(f"● FB: {f2[0]}", self.theme["ok"])
                                return
                        self.sig.log.emit("⚠ 40 秒内未检测到 Fastboot", "warn")
                        return

                    if fs:
                        # 已经在 Fastboot —— 无论现在是什么模式，都强制再进入 bootloader
                        cur = self._fb_mode(fs[0])
                        self.sig.log.emit(f"当前模式：{cur}，执行强制进入 Bootloader", "info")
                        cmd = ["fastboot", "-s", fs[0], "reboot", "bootloader"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=20)
                        if out:
                            for line in out.splitlines():
                                self.sig.log.emit(f"  {line}", "plain")
                        if rc != 0:
                            self.sig.log.emit(f"❌ 命令失败（{rc}）", "error")
                            return
                        self.sig.log.emit("✓ 命令已发送，等待设备重新进入 Bootloader …", "ok")
                        # 等待并确认
                        old_sn = fs[0]
                        for _ in range(20):
                            time.sleep(2)
                            f2, _ = fb_dev()
                            if f2:
                                mode = self._fb_mode(f2[0])
                                if mode == "bootloader":
                                    self.sig.log.emit(
                                        f"✓ 设备已在 Bootloader：{f2[0]}", "ok")
                                    self.sig.dev.emit(
                                        f"● FB: {f2[0]}", self.theme["ok"])
                                    return
                        self.sig.log.emit("✓ 命令已发送（未重新检测到设备，可查看手机屏幕）", "ok")
                        return

                    self.sig.log.emit("❌ 未检测到设备", "error")
                    return

                # ---------- Fastbootd ----------
                if target == "fastboot":
                    if ds:
                        # ADB → 直接 reboot fastboot
                        cmd = ["adb", "-s", ds[0], "reboot", "fastboot"]
                        self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
                        rc, out = sh(cmd, timeout=15)
                        if out:
                            self.sig.log.emit(out, "plain")
                        if rc != 0:
                            self.sig.log.emit(f"❌ 失败（{rc}）", "error")
                            return
                        self.sig.log.emit("✓ 已发送，等待进入 Fastbootd …", "ok")
                        for _ in range(20):
                            time.sleep(2)
                            f2, _ = fb_dev()
                            if f2:
                                mode = self._fb_mode(f2[0])
                                if mode == "fastbootd":
                                    self.sig.log.emit(
                                        f"✓ 已进入 Fastbootd：{f2[0]}", "ok")
                                    self.sig.dev.emit(
                                        f"● FB: {f2[0]}", self.theme["ok"])
                                    return
                        self.sig.log.emit("⚠ 40 秒内未检测到 Fastbootd", "warn")
                        return

                    if fs:
                        # 已在 Fastboot —— 判断当前模式
                        cur = self._fb_mode(fs[0])
                        if cur == "fastbootd":
                            self.sig.log.emit(
                                "✓ 设备已在 Fastbootd，无需切换", "info")
                            return

                        # bootloader / unknown → 尝试多种命令
                        self.sig.log.emit(
                            f"设备当前在 {cur}，尝试切换到 Fastbootd …", "info")
                        ok = self._try_enter_fastbootd(fs[0])
                        if not ok:
                            self.sig.log.emit("❌ 所有切换命令都失败", "error")
                            self.sig.log.emit(
                                "   建议：先点「系统」重启到系统，"
                                "再用 ADB 方式进入 Fastbootd", "info")
                        return

                    self.sig.log.emit("❌ 未检测到设备", "error")
                    return

            except Exception as e:
                self.sig.log.emit(f"❌ 重启异常：{e}", "error")
                self.sig.log.emit(traceback.format_exc(), "error")

        threading.Thread(target=w, daemon=True).start()

    def _try_enter_fastbootd(self, serial):
        """从 bootloader 尝试多种命令切到 fastbootd，成功返回 True"""
        attempts = [
            ["fastboot", "-s", serial, "reboot", "fastboot"],
            ["fastboot", "-s", serial, "oem", "reboot-fastboot"],
            ["fastboot", "-s", serial, "reboot-fastboot"],
        ]
        bad_hints = ("unknown command", "not supported", "not implemented",
                     "invalid", "failed", "error")

        for i, cmd in enumerate(attempts, 1):
            self.sig.log.emit(
                f"[尝试 {i}/{len(attempts)}] {' '.join(cmd)}", "info")
            rc, out = sh(cmd, timeout=20)
            low = (out or "").lower()
            if out:
                for line in out.splitlines():
                    self.sig.log.emit(f"  {line}", "plain")

            if rc != 0 or any(h in low for h in bad_hints):
                self.sig.log.emit("  该命令不被支持，试下一个 …", "warn")
                continue

            self.sig.log.emit(
                "  ✓ 命令已接受，等待设备重新枚举 …", "ok")

            # 等待设备重新出现
            for _ in range(30):   # 最多 60 秒
                time.sleep(2)
                f2, _ = fb_dev()
                if not f2:
                    continue
                mode = self._fb_mode(f2[0])
                self.sig.log.emit(f"  检测到设备：{f2[0]}，模式={mode}", "plain")
                if mode == "fastbootd":
                    self.sig.log.emit(f"✓ 已进入 Fastbootd：{f2[0]}", "ok")
                    self.sig.dev.emit(f"● FB: {f2[0]}", self.theme["ok"])
                    return True

            self.sig.log.emit("  ⚠ 超时未检测到 Fastbootd，试下一个命令 …", "warn")

        return False