# -*- coding: utf-8 -*-
"""页面 4：投屏（scrcpy）"""
import subprocess, threading, time
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QComboBox, QSpinBox, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from core.config import APP, WIN
from core.utils import sh, adb_dev


class MirrorPageMixin:

    def _page_mirror(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        t = QLabel("🖥  投屏（scrcpy）")
        t.setObjectName("cardTitle")
        v.addWidget(t)
        d = QLabel("把手机屏幕投射到电脑，支持鼠标键盘操作。需要先安装 scrcpy 并加入 PATH")
        d.setObjectName("desc")
        v.addWidget(d)

        st = QHBoxLayout()
        st.setSpacing(10)
        st.addWidget(self._label("scrcpy 状态"))
        self.mirrorStatus = QLabel("● 未检测")
        self.mirrorStatus.setObjectName("status")
        st.addWidget(self.mirrorStatus)
        self.mirrorChkBtn = QPushButton("🔄  检测环境")
        self.mirrorChkBtn.setObjectName("secondary")
        self.mirrorChkBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mirrorChkBtn.setFixedHeight(38)
        self.mirrorChkBtn.setFixedWidth(150)
        self.mirrorChkBtn.clicked.connect(self.do_check_scrcpy)
        st.addWidget(self.mirrorChkBtn)
        st.addStretch()
        v.addLayout(st)

        v.addWidget(self._label("投屏参数"))
        params = QHBoxLayout()
        params.setSpacing(12)
        params.addWidget(QLabel("分辨率："))
        self.mirrorRes = QComboBox()
        self.mirrorRes.addItems(["原始", "1920", "1280", "1024", "800"])
        self.mirrorRes.setFixedWidth(100)
        self.mirrorRes.setFixedHeight(36)
        params.addWidget(self.mirrorRes)
        params.addWidget(QLabel("码率(Mbps)："))
        self.mirrorBit = QSpinBox()
        self.mirrorBit.setRange(1, 50)
        self.mirrorBit.setValue(8)
        self.mirrorBit.setFixedWidth(80)
        self.mirrorBit.setFixedHeight(36)
        params.addWidget(self.mirrorBit)
        params.addWidget(QLabel("最大FPS："))
        self.mirrorFps = QSpinBox()
        self.mirrorFps.setRange(15, 120)
        self.mirrorFps.setValue(60)
        self.mirrorFps.setFixedWidth(80)
        self.mirrorFps.setFixedHeight(36)
        params.addWidget(self.mirrorFps)
        params.addStretch()
        v.addLayout(params)

        op = QHBoxLayout()
        op.setSpacing(10)
        self.mirrorStartBtn = QPushButton("▶  开始投屏")
        self.mirrorStartBtn.setObjectName("primary")
        self.mirrorStartBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mirrorStartBtn.setFixedHeight(44)
        self.mirrorStartBtn.setFixedWidth(180)
        self.mirrorStartBtn.clicked.connect(self.do_start_mirror)
        op.addWidget(self.mirrorStartBtn)
        self.mirrorStopBtn = QPushButton("■  停止投屏")
        self.mirrorStopBtn.setObjectName("secondary")
        self.mirrorStopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mirrorStopBtn.setFixedHeight(44)
        self.mirrorStopBtn.setFixedWidth(180)
        self.mirrorStopBtn.setEnabled(False)
        self.mirrorStopBtn.clicked.connect(self.do_stop_mirror)
        op.addWidget(self.mirrorStopBtn)
        op.addStretch()
        v.addLayout(op)

        tip = QLabel("提示：投屏中鼠标可操作手机，Ctrl+H 返回桌面、Ctrl+P 电源键")
        tip.setObjectName("desc")
        v.addWidget(tip)
        v.addStretch()
        return w

    def do_check_scrcpy(self):
        self.lg("检测 scrcpy…", "info")
        def w():
            rc, out = sh(["scrcpy", "--version"], timeout=5)
            if rc == 0:
                ver = out.splitlines()[0] if out else "scrcpy"
                self.sig.log.emit(f"✓ 已安装：{ver}", "ok")
                QTimer.singleShot(0, lambda: self.mirrorStatus.setText("● 已就绪"))
                QTimer.singleShot(0, lambda: self.mirrorStatus.setStyleSheet(
                    f"color: {self.theme['ok']}; font-weight: bold; background: transparent;"))
            else:
                self.sig.log.emit("❌ 未找到 scrcpy，请安装并加入 PATH", "error")
                self.sig.log.emit(
                    "下载：https://github.com/Genymobile/scrcpy/releases", "info")
                QTimer.singleShot(0, lambda: self.mirrorStatus.setText("● 未找到"))
                QTimer.singleShot(0, lambda: self.mirrorStatus.setStyleSheet(
                    f"color: {self.theme['err']}; font-weight: bold; background: transparent;"))
        threading.Thread(target=w, daemon=True).start()

    def do_start_mirror(self):
        if self.scrcpy_proc is not None:
            self.lg("投屏已在运行", "warn")
            return
        ds, _ = adb_dev()
        if not ds:
            QMessageBox.warning(self, "无设备",
                                "未检测到 ADB 设备。\n请连接手机并授权 USB 调试。")
            return
        cmd = ["scrcpy", "--stay-awake", "--turn-screen-off",
               "--window-title", f"{APP} - 投屏"]
        res = self.mirrorRes.currentText()
        if res != "原始":
            cmd += ["--max-size", res]
        cmd += ["--video-bit-rate", f"{self.mirrorBit.value()}M"]
        cmd += ["--max-fps", str(self.mirrorFps.value())]
        cmd += ["-s", ds[0]]
        self.lg(f"启动：{' '.join(cmd)}", "info")
        try:
            kw = {}
            if WIN:
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.scrcpy_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, **kw)
            self.lg("✓ 投屏已启动", "ok")
            self.mirrorStartBtn.setEnabled(False)
            self.mirrorStopBtn.setEnabled(True)
            threading.Thread(target=self._watch_scrcpy, daemon=True).start()
        except FileNotFoundError:
            self.lg("❌ 未找到 scrcpy，请先安装", "error")
            self.scrcpy_proc = None
        except Exception as e:
            self.lg(f"❌ 启动失败：{e}", "error")
            self.scrcpy_proc = None

    def _watch_scrcpy(self):
        try:
            if self.scrcpy_proc and self.scrcpy_proc.stdout:
                for line in self.scrcpy_proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.sig.log.emit(f"[scrcpy] {line}", "plain")
        except Exception:
            pass
        self.sig.log.emit("投屏已停止", "info")
        QTimer.singleShot(0, self._mirror_stopped)

    def _mirror_stopped(self):
        self.mirrorStartBtn.setEnabled(True)
        self.mirrorStopBtn.setEnabled(False)
        self.scrcpy_proc = None

    def do_stop_mirror(self):
        if self.scrcpy_proc is None:
            return
        self.lg("停止投屏…", "info")
        try:
            self.scrcpy_proc.terminate()
        except Exception:
            pass

        def kill_later():
            time.sleep(3)
            if self.scrcpy_proc is not None:
                try:
                    self.scrcpy_proc.kill()
                except Exception:
                    pass
        threading.Thread(target=kill_later, daemon=True).start()