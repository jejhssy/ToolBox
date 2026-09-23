# -*- coding: utf-8 -*-
"""页面：临时提权（通过设备已有的 su 能力执行一次性命令）"""
import re
import subprocess
import threading
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTextEdit, QMessageBox, QFileDialog, QProgressBar,
)
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QPainter
from core.config import WIN


class RootProgressBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextVisible(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.palette().text().color())
        painter.drawText(
            self.rect().adjusted(14, 0, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{self.value()}%",
        )
        painter.end()


class RootPageMixin:

    def _page_root(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("🛡  临时提权")
        title.setObjectName("cardTitle")
        v.addWidget(title)

        desc = QLabel("选择提权文件后，按步骤推送、授权、重启并验证临时权限。")
        desc.setObjectName("desc")
        v.addWidget(desc)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("提权文件"))
        self.rootFileE = QLineEdit()
        self.rootFileE.setPlaceholderText("选择 preload.so 或对应提权文件")
        self.rootFileE.setFixedHeight(40)
        file_row.addWidget(self.rootFileE, 1)
        self.rootFileBtn = QPushButton("📂  选择文件")
        self.rootFileBtn.setObjectName("secondary")
        self.rootFileBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rootFileBtn.setFixedHeight(40)
        self.rootFileBtn.clicked.connect(self.do_root_pick_file)
        file_row.addWidget(self.rootFileBtn)
        v.addLayout(file_row)

        action = QHBoxLayout()
        self.rootStartBtn = QPushButton("🚀  开始临时提权")
        self.rootStartBtn.setObjectName("primary")
        self.rootStartBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rootStartBtn.setFixedHeight(42)
        self.rootStartBtn.clicked.connect(self.do_root_start)
        action.addWidget(self.rootStartBtn)

        self.rootStopBtn = QPushButton("⏹  停止")
        self.rootStopBtn.setObjectName("secondary")
        self.rootStopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rootStopBtn.setFixedHeight(42)
        self.rootStopBtn.setEnabled(False)
        self.rootStopBtn.clicked.connect(self.do_root_stop)
        action.addWidget(self.rootStopBtn)
        action.addStretch()
        v.addLayout(action)

        progress_row = QHBoxLayout()
        self.rootProgressLabel = QLabel("当前进度：等待开始")
        self.rootProgressLabel.setObjectName("info")
        progress_row.addWidget(self.rootProgressLabel)
        self.rootProgress = RootProgressBar()
        self.rootProgress.setObjectName("rootProgress")
        self.rootProgress.setRange(0, 100)
        self.rootProgress.setValue(0)
        self.rootProgress.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.rootProgress.setFixedHeight(18)
        progress_row.addWidget(self.rootProgress, 1)
        v.addLayout(progress_row)

        self.rootStatus = QLabel("等待检测设备…")
        self.rootStatus.setObjectName("info")
        v.addWidget(self.rootStatus)

        self.rootOutput = QTextEdit()
        self.rootOutput.setReadOnly(True)
        self.rootOutput.setObjectName("rootOutput")
        self.rootOutput.setPlaceholderText("命令输出会显示在这里")
        v.addWidget(self.rootOutput, 1)
        output_actions = QHBoxLayout()
        self.rootClearBtn = QPushButton("🧹  清空日志")
        self.rootClearBtn.setObjectName("ghost")
        self.rootClearBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rootClearBtn.setFixedHeight(32)
        self.rootClearBtn.clicked.connect(self._root_clear_logs)
        output_actions.addWidget(self.rootClearBtn)
        output_actions.addStretch()
        v.addLayout(output_actions)

        self._root_proc = None
        self._root_busy = False
        self._root_cancel = threading.Event()
        self._root_stop_requested = False
        self._root_privileged = False
        self._root_phase_start = 0
        self._root_phase_span = 25
        self._root_phase_text = "准备中…"
        self.rootProgressAnim = QPropertyAnimation(
            self.rootProgress, b"value", self.rootProgress)
        self.rootProgressAnim.setEasingCurve(QEasingCurve.Type.OutCubic)
        return w

    def _root_set_busy(self, busy):
        self._root_busy = busy
        self.rootStartBtn.setEnabled(not busy)
        self.rootFileBtn.setEnabled(not busy)
        self.rootStopBtn.setEnabled(busy)

    def do_root_pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择提权文件", "", "动态库/二进制文件 (*.so *.bin *.elf);;所有文件 (*)")
        if path:
            self.rootFileE.setText(path)

    def do_root_start(self):
        if self._root_busy:
            return
        source = self.rootFileE.text().strip()
        if not source or not Path(source).is_file():
            QMessageBox.warning(self, "缺少提权文件", "请选择有效的本地提权文件。")
            return
        self._root_set_busy(True)
        self._root_cancel = threading.Event()
        self._root_stop_requested = False
        self._root_privileged = False
        self._root_phase_text = "准备中…"
        self.rootProgress.setValue(0)
        self.rootProgressLabel.setText("当前进度：准备中")
        self.rootStatus.setText("正在准备临时提权流程…")
        self.rootOutput.clear()
        threading.Thread(
            target=self._root_worker,
            args=(source,),
            daemon=True,
        ).start()

    def do_root_stop(self):
        if not self._root_busy:
            return
        self._root_stop_requested = True
        self._root_cancel.set()
        process = self._root_proc
        if process is not None:
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
        self.rootStatus.setText("⏹ 已停止")
        self.rootProgressLabel.setText("当前进度：提权已停止")

    def _root_clear_logs(self):
        self.rootOutput.clear()
        self.sig.log.emit("已清空临时提权输出", "info")

    def _root_run(self, args, on_text=None):
        if self._root_cancel.is_set():
            return -100, "已停止"
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **kw)
        self._root_proc = process
        output = []
        try:
            for line in process.stdout:
                line = line.rstrip("\r\n")
                output.append(line)
                if on_text:
                    on_text(line)
            process.wait()
            return process.returncode, "\n".join(output)
        finally:
            self._root_proc = None

    def _root_progress(self, value, text):
        self.sig.ui.emit(lambda: self._root_set_progress(value, text))

    def _root_set_phase(self, start, span, text):
        self._root_phase_start = start
        self._root_phase_span = span
        self._root_phase_text = text
        self._root_progress(start, text)

    def _root_output_line(self, line):
        self.sig.log.emit(f"[临时提权] {line}", "root")
        self.sig.ui.emit(lambda s=line: self._root_append_output(s))
        match = re.search(r"(\d{1,3})\s*%", line or "")
        if match:
            percent = min(100, int(match.group(1)))
            value = self._root_phase_start + int(
                self._root_phase_span * percent / 100)
            self._root_progress(value, self._root_phase_text)

    def _root_append_output(self, line):
        self.rootOutput.append(line)

    def _root_set_progress(self, value, text):
        if self._root_stop_requested:
            return
        current = self.rootProgress.value()
        self.rootProgressAnim.stop()
        self.rootProgressAnim.setStartValue(current)
        self.rootProgressAnim.setEndValue(value)
        self.rootProgressAnim.setDuration(
            max(450, min(1600, abs(value - current) * 45)))
        self.rootProgressAnim.start()
        self._root_phase_text = text
        self.rootProgressLabel.setText(f"当前进度：{text}")
        self.rootStatus.setText(text)

    def _root_worker(self, source):
        try:
            remote = "/data/local/tmp/preload.so"
            steps = [
                (0, 25, ["adb", "push", source, remote], "推送中…"),
                (25, 40, ["adb", "shell", "chmod", "+x", remote], "设置权限中…"),
                (40, 55, ["adb", "reboot"], "重启中…"),
            ]
            output = []
            for start, end, command, label in steps:
                self._root_set_phase(start, end - start, label)
                code, text = self._root_run(command, self._root_output_line)
                output.append(text)
                if code != 0:
                    self.sig.ui.emit(lambda c=code, o="\n".join(output): self._root_finish(c, o))
                    return
                self._root_progress(end, label.replace("中…", "完成"))

            self._root_set_phase(55, 20, "等待设备重新上线…")
            code, text = self._root_run(["adb", "wait-for-device"], self._root_output_line)
            output.append(text)
            if code != 0:
                self.sig.ui.emit(lambda c=code, o="\n".join(output): self._root_finish(c, o))
                return
            self._root_progress(75, "重启并等待设备完成")

            self._root_set_phase(75, 25, "提权验证中…")
            code, text = self._root_run(
                ["adb", "shell", "LD_PRELOAD=/data/local/tmp/preload.so", "/system/bin/id"],
                self._root_output_line)
            output.append(text)
            self._root_progress(100, "提权验证完成")
            self.sig.ui.emit(lambda c=code, o="\n".join(output): self._root_finish(c, o))
        except FileNotFoundError:
            self.sig.ui.emit(lambda: self._root_finish(-1, "未找到 adb，请安装 Android platform-tools。"))
        except Exception as exc:
            self.sig.ui.emit(lambda: self._root_finish(-2, str(exc)))
        finally:
            self._root_proc = None

    def _root_finish(self, code, output):
        self._root_set_busy(False)
        self.rootOutput.setPlainText(output.strip())
        if self._root_stop_requested:
            self.rootStatus.setText("⏹ 已停止")
        elif code == 0 and "uid=0" in output:
            self.rootStatus.setText("✅ 已获得临时 root 权限")
            self.sig.log.emit("临时提权成功", "ok")
        elif code == 0:
            self.rootStatus.setText("⚠ 流程完成，但未检测到 uid=0")
            self.sig.log.emit("临时提权流程完成，但未检测到 uid=0", "warn")
        else:
            self.rootStatus.setText("❌ 临时提权失败")
            self.sig.log.emit(f"❌ 临时提权失败：{output.strip()[:500]}", "error")
            QMessageBox.critical(
                self, "临时提权失败",
                output.strip() or "提权流程返回失败，请检查设备连接和提权文件。")
