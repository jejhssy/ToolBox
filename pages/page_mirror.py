# -*- coding: utf-8 -*-
"""页面 4：投屏（scrcpy）"""
import os, subprocess, threading, time
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QComboBox, QSpinBox, QMessageBox, QTextEdit,
)
from PyQt6.QtCore import Qt, QTimer
from core.config import APP, WIN
from core.widgets import set_state
from core.utils import sh, adb_dev


def _find_scrcpy():
    """查找 scrcpy.exe：先 PATH，再项目根/tools 下（含一层子目录）"""
    import shutil
    found = shutil.which("scrcpy")
    if found:
        return found
    here = Path(__file__).resolve().parent.parent
    search_dirs = [here, here / "tools", Path.cwd(), Path.cwd() / "tools"]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for name in ("scrcpy.exe", "scrcpy"):
            f = d / name
            if f.exists():
                return str(f)
        try:
            for child in d.iterdir():
                if child.is_dir():
                    for name in ("scrcpy.exe", "scrcpy"):
                        f = child / name
                        if f.exists():
                            return str(f)
        except Exception:
            pass
    return None


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

        # ---- scrcpy 状态 ----
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

        # ---- 参数 ----
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

        # ---- 起停 ----
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

        # ---- 快捷操作按钮 ----
        v.addWidget(self._label("快捷操作（按键发送到手机）"))
        navRow = QHBoxLayout()
        navRow.setSpacing(8)
        for text, tip, kc in (
            ("🏠  桌面",   "返回桌面",             3),
            ("◀  返回",    "返回上一级",           4),
            ("☰  任务",    "打开最近任务/多任务",  187),
            ("🔒  锁屏",   "锁定/电源键",          26),
            ("🔊  音量+",  "音量加",               24),
            ("🔉  音量-",  "音量减",               25),
            ("📷  截图",   "截屏并保存到电脑",     None),
        ):
            b = QPushButton(text)
            b.setObjectName("secondary")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(40)
            b.setToolTip(tip)
            if kc is None:
                b.clicked.connect(self.do_mirror_screenshot)
            else:
                b.clicked.connect(lambda _=False, k=kc: self._mirror_key(k))
            navRow.addWidget(b)
        navRow.addStretch()
        v.addLayout(navRow)

        # ---- 投屏内部日志 ----
        logHead = QHBoxLayout()
        logHead.setSpacing(8)
        logHead.addWidget(self._label("投屏日志"))
        logHead.addStretch()
        self.mirrorClearBtn = QPushButton("清空")
        self.mirrorClearBtn.setObjectName("smallGhost")
        self.mirrorClearBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mirrorClearBtn.setFixedHeight(30)
        self.mirrorClearBtn.clicked.connect(self._mirror_clear_log)
        logHead.addWidget(self.mirrorClearBtn)
        v.addLayout(logHead)

        self.mirrorLog = QTextEdit()
        self.mirrorLog.setReadOnly(True)
        self.mirrorLog.setObjectName("log")
        self.mirrorLog.setPlaceholderText("scrcpy 输出、快捷操作反馈会显示在这里")
        self.mirrorLog.setFixedHeight(150)
        v.addWidget(self.mirrorLog)

        v.addStretch()
        return w

    # ==================== 内部日志 ====================
    def _mirror_log(self, msg, level="plain"):
        """写投屏页内部日志（主线程调用）"""
        if not hasattr(self, "mirrorLog"):
            return
        colors = {
            "info": self.theme["acc2"],
            "ok": self.theme["ok"],
            "warn": self.theme["warn"],
            "error": self.theme["err"],
            "plain": self.theme["text"],
        }
        col = colors.get(level, self.theme["text"])
        ts = datetime.now().strftime("%H:%M:%S")
        html = (f'<span style="color:{self.theme["dim"]}">[{ts}]</span> '
                f'<span style="color:{col}">{msg}</span>')
        self.mirrorLog.append(html)
        sb = self.mirrorLog.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _mirror_log_async(self, msg, level="plain"):
        """从子线程安全写日志"""
        self.sig.ui.emit(lambda m=msg, l=level: self._mirror_log(m, l))

    def _mirror_clear_log(self):
        if hasattr(self, "mirrorLog"):
            self.mirrorLog.clear()

    # ==================== 环境检测 ====================
    def do_check_scrcpy(self):
        self._mirror_log("检测 scrcpy …", "info")

        def w():
            exe = _find_scrcpy()
            if not exe:
                self._mirror_log_async("❌ 未找到 scrcpy", "error")
                self._mirror_log_async(
                    "请把 scrcpy-win64-vX.X 解压到项目根或 tools/ 子目录", "info")
                self._mirror_log_async(
                    "下载：https://github.com/Genymobile/scrcpy/releases", "info")
                QTimer.singleShot(0, lambda: self.mirrorStatus.setText("● 未找到"))
                QTimer.singleShot(0, lambda: set_state(self.mirrorStatus, "err"))
                return

            rc, out = sh([exe, "--version"], timeout=5)
            if rc == 0:
                ver = out.splitlines()[0] if out else "scrcpy"
                self._mirror_log_async(f"✓ 已安装：{ver}", "ok")
                self._mirror_log_async(f"  路径：{exe}", "plain")
                QTimer.singleShot(0, lambda: self.mirrorStatus.setText("● 已就绪"))
                QTimer.singleShot(0, lambda: set_state(self.mirrorStatus, "ok"))
            else:
                self._mirror_log_async(f"❌ scrcpy 无法运行：{out}", "error")
                self._mirror_log_async(f"  路径：{exe}", "plain")
                QTimer.singleShot(0, lambda: self.mirrorStatus.setText("● 异常"))
                QTimer.singleShot(0, lambda: set_state(self.mirrorStatus, "err"))
        threading.Thread(target=w, daemon=True).start()

    # ==================== 起停 ====================
    def do_start_mirror(self):
        if self.scrcpy_proc is not None:
            self._mirror_log("投屏已在运行", "warn")
            return
        ds, _ = adb_dev()
        if not ds:
            QMessageBox.warning(self, "无设备",
                                "未检测到 ADB 设备。\n请连接手机并授权 USB 调试。")
            return
        exe = _find_scrcpy()
        if not exe:
            QMessageBox.warning(
                self, "未找到 scrcpy",
                "没找到 scrcpy.exe。\n\n"
                "请把 scrcpy-win64-vX.X 解压到：\n"
                "  · 项目根目录\n"
                "  · 或 tools/ 子目录")
            self._mirror_log("❌ 未找到 scrcpy.exe", "error")
            return
        cmd = [exe, "--stay-awake", "--turn-screen-off",
               "--window-title", f"{APP} - 投屏"]
        res = self.mirrorRes.currentText()
        if res != "原始":
            cmd += ["--max-size", res]
        cmd += ["--video-bit-rate", f"{self.mirrorBit.value()}M"]
        cmd += ["--max-fps", str(self.mirrorFps.value())]
        cmd += ["-s", ds[0]]
        self._mirror_log(f"启动：{' '.join(cmd)}", "info")
        try:
            kw = {}
            if WIN:
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.scrcpy_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", **kw)
            self._mirror_log("✓ 投屏已启动", "ok")
            self.mirrorStartBtn.setEnabled(False)
            self.mirrorStopBtn.setEnabled(True)
            threading.Thread(target=self._watch_scrcpy, daemon=True).start()
        except FileNotFoundError:
            self._mirror_log("❌ 未找到 scrcpy，请先安装", "error")
            self.scrcpy_proc = None
        except Exception as e:
            self._mirror_log(f"❌ 启动失败：{e}", "error")
            self.scrcpy_proc = None

    def _watch_scrcpy(self):
        try:
            if self.scrcpy_proc and self.scrcpy_proc.stdout:
                for line in self.scrcpy_proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._mirror_log_async(f"[scrcpy] {line}", "plain")
        except Exception:
            pass
        self._mirror_log_async("投屏已停止", "info")
        QTimer.singleShot(0, self._mirror_stopped)

    def _mirror_stopped(self):
        self.mirrorStartBtn.setEnabled(True)
        self.mirrorStopBtn.setEnabled(False)
        self.scrcpy_proc = None

    def do_stop_mirror(self):
        if self.scrcpy_proc is None:
            return
        self._mirror_log("停止投屏…", "info")
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

    # ==================== 快捷操作 ====================
    def _mirror_key(self, keycode):
        """发送 keyevent 到手机"""
        def w():
            ds, _ = adb_dev()
            if not ds:
                self._mirror_log_async("❌ 无 ADB 设备", "error")
                return
            name = {3: "返回桌面", 4: "返回上一级", 187: "最近任务",
                    26: "电源键", 24: "音量+", 25: "音量-",
                    82: "菜单"}.get(keycode, str(keycode))
            cmd = ["adb", "-s", ds[0], "shell", "input", "keyevent", str(keycode)]
            rc, out = sh(cmd, timeout=5)
            if rc == 0:
                self._mirror_log_async(f"✓ {name}", "ok")
            else:
                self._mirror_log_async(f"❌ {name} 失败：{out}", "error")
        threading.Thread(target=w, daemon=True).start()

    def do_mirror_screenshot(self):
        """截屏并拉取到电脑图片目录"""
        def w():
            ds, _ = adb_dev()
            if not ds:
                self._mirror_log_async("❌ 无 ADB 设备", "error")
                return
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            pics = Path(os.environ.get("USERPROFILE")
                        or os.path.expanduser("~")) / "Pictures"
            try:
                pics.mkdir(parents=True, exist_ok=True)
            except Exception:
                pics = Path(os.environ.get("TEMP", "."))
            local = pics / f"screenshot_{ts}.png"
            remote = f"/sdcard/screenshot_{ts}.png"
            rc, out = sh(["adb", "-s", ds[0], "shell", "screencap", "-p", remote],
                         timeout=15)
            if rc != 0:
                self._mirror_log_async(f"❌ 截图失败：{out}", "error")
                return
            rc, out = sh(["adb", "-s", ds[0], "pull", remote, str(local)],
                         timeout=30)
            sh(["adb", "-s", ds[0], "shell", "rm", remote], timeout=5)
            if rc == 0:
                self._mirror_log_async(f"✓ 截图已保存：{local}", "ok")
            else:
                self._mirror_log_async(f"❌ 拉取截图失败：{out}", "error")
        threading.Thread(target=w, daemon=True).start()