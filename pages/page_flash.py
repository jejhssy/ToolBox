# -*- coding: utf-8 -*-
"""页面 1：常规镜像刷入 + MiFlash 一键刷写（小米线刷包）"""
import os, re, threading, time, traceback
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QComboBox, QProgressBar, QMessageBox, QFileDialog,
    QCheckBox, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from core.config import FASTBOOTD_PARTS, SPECIAL_OPTS
from core.utils import sh, sh_stream, adb_dev, fb_dev


# ==================== MiFlash 脚本解析 ====================
def parse_miflash_script(script_path):
    """解析小米 flash_all*.bat，返回有序命令列表：
       [
         {"type": "erase", "partition": "boot"},
         {"type": "flash", "partition": "boot", "image": "C:\\...\\boot.img"},
         {"type": "lock",  "cmd": "flashing lock"},
         {"type": "reboot"},
       ]
    """
    p = Path(script_path)
    script_dir = p.parent

    text = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = p.read_text(encoding=enc, errors="ignore")
            break
        except Exception:
            continue
    if text is None:
        return []

    cmds = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # 去掉 || && 后面的错误处理
        for sep in ("||", "&&"):
            if sep in line:
                line = line.split(sep, 1)[0].strip()

        if not line.lower().startswith("fastboot"):
            continue

        # 替换 %~dp0 等变量
        line = line.replace("%~dp0", str(script_dir) + os.sep)
        line = line.replace("%~dpn0", str(script_dir) + os.sep + p.name)

        # 分词（保留引号内容）
        tokens = re.findall(r'"[^"]*"|\S+', line)
        if len(tokens) < 2:
            continue

        # 跳过 fastboot 和 %* / %1 / 选项
        i = 1
        while i < len(tokens):
            t = tokens[i].strip('"')
            if re.match(r"^%\d*\*?$", t):
                i += 1
                continue
            if t.startswith("-") or t.startswith("/"):
                i += 1
                continue
            break
        if i >= len(tokens):
            continue

        op = tokens[i].strip('"').lower()

        if op == "flash" and i + 2 < len(tokens):
            part = tokens[i + 1].strip('"')
            img = tokens[i + 2].strip('"')
            cmds.append({"type": "flash", "partition": part, "image": img})

        elif op == "erase" and i + 1 < len(tokens):
            part = tokens[i + 1].strip('"')
            cmds.append({"type": "erase", "partition": part})

        elif op == "flashing" and i + 1 < len(tokens):
            sub = tokens[i + 1].strip('"').lower()
            if sub in ("lock", "unlock"):
                cmds.append({"type": "lock", "cmd": f"flashing {sub}"})

        elif op == "oem" and i + 1 < len(tokens):
            sub = tokens[i + 1].strip('"').lower()
            if sub in ("lock", "unlock"):
                cmds.append({"type": "lock", "cmd": f"oem {sub}"})

        elif op == "reboot":
            cmds.append({"type": "reboot"})

    return cmds


# ==================== 页面 ====================
class FlashPageMixin:

    def _page_flash(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ============ 单分区刷入（原有） ============
        row = QHBoxLayout()
        row.setSpacing(12)

        col1 = QVBoxLayout()
        col1.setSpacing(6)
        col1.addWidget(self._label("目标分区"))
        self.partCombo = QComboBox()
        self.partCombo.addItems([
            "boot", "init_boot", "recovery", "vendor_boot",
            "dtbo", "vbmeta_system", "super",
            "— 自定义 —",
        ])
        self.partCombo.setFixedHeight(42)
        self.partCombo.setFixedWidth(180)
        self.partCombo.currentIndexChanged.connect(self._on_part_changed)
        col1.addWidget(self.partCombo)
        row.addLayout(col1)

        col2 = QVBoxLayout()
        col2.setSpacing(6)
        col2.addWidget(self._label("镜像文件"))
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.imgE = QLineEdit()
        self.imgE.setReadOnly(True)
        self.imgE.setPlaceholderText("请选择需要刷入的文件…")
        self.imgE.setFixedHeight(42)
        frow.addWidget(self.imgE, 1)
        self.imgBtn = QPushButton("📂  选择")
        self.imgBtn.setObjectName("primary")
        self.imgBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.imgBtn.setFixedHeight(42)
        self.imgBtn.setFixedWidth(110)
        self.imgBtn.clicked.connect(self.do_pick)
        frow.addWidget(self.imgBtn)
        col2.addLayout(frow)
        row.addLayout(col2, 1)

        col3 = QVBoxLayout()
        col3.setSpacing(6)
        col3.addWidget(QLabel(" "))
        self.flBtn = QPushButton("⚡  开始刷入")
        self.flBtn.setObjectName("primary")
        self.flBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.flBtn.setFixedHeight(42)
        self.flBtn.setFixedWidth(160)
        self.flBtn.setEnabled(False)
        self.flBtn.clicked.connect(self.do_flash)
        col3.addWidget(self.flBtn)
        row.addLayout(col3)
        v.addLayout(row)

        self.customPartE = QLineEdit()
        self.customPartE.setPlaceholderText("自定义分区名，例如 misc")
        self.customPartE.setFixedHeight(38)
        self.customPartE.setVisible(False)
        self.customPartE.setMaximumWidth(400)
        v.addWidget(self.customPartE)

        self.partTip = QLabel("")
        self.partTip.setObjectName("desc")
        self.partTip.setWordWrap(True)
        v.addWidget(self.partTip)

        qm = QHBoxLayout()
        qm.setSpacing(8)
        self.entBtn = QPushButton("📲  进入刷写模式")
        self.entBtn.setObjectName("secondary")
        self.entBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.entBtn.setFixedHeight(42)
        self.entBtn.setFixedWidth(180)
        self.entBtn.clicked.connect(self.do_enter)
        qm.addWidget(self.entBtn)
        self.rebootBtn = QPushButton("🔄  重启到系统")
        self.rebootBtn.setObjectName("secondary")
        self.rebootBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rebootBtn.setFixedHeight(42)
        self.rebootBtn.setFixedWidth(180)
        self.rebootBtn.clicked.connect(lambda: self.do_reboot("system"))
        qm.addWidget(self.rebootBtn)
        qm.addStretch()
        v.addLayout(qm)

        tip = QLabel(
            "提示：boot / dtbo / vbmeta / recovery 刷到 bootloader；\n"
            "init_boot / vendor_boot / super 刷到 fastbootd（用户空间）"
        )
        tip.setObjectName("desc")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # ============ 分隔 ============
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: transparent; color: #2a3555;")
        sep.setFixedHeight(1)
        v.addSpacing(6)
        v.addWidget(sep)
        v.addSpacing(6)

        # ============ MiFlash 一键刷写 ============
        mTitle = QLabel("📦  MiFlash 一键刷写（小米线刷包）")
        mTitle.setObjectName("cardTitle")
        v.addWidget(mTitle)

        mDesc = QLabel("选择解压后的刷机包里的 flash_all.bat（清除全部数据） / flash_all_lock.bat（刷写并回锁）")
        mDesc.setObjectName("desc")
        v.addWidget(mDesc)

        mRow1 = QHBoxLayout()
        mRow1.setSpacing(8)
        self.miflashScriptE = QLineEdit()
        self.miflashScriptE.setReadOnly(True)
        self.miflashScriptE.setPlaceholderText("请选择 flash 脚本…")
        self.miflashScriptE.setFixedHeight(42)
        mRow1.addWidget(self.miflashScriptE, 1)

        self.miflashPickBtn = QPushButton("📂  选择脚本")
        self.miflashPickBtn.setObjectName("primary")
        self.miflashPickBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.miflashPickBtn.setFixedHeight(42)
        self.miflashPickBtn.setFixedWidth(130)
        self.miflashPickBtn.clicked.connect(self.do_miflash_pick)
        mRow1.addWidget(self.miflashPickBtn)
        v.addLayout(mRow1)

        # 两个选项
        optRow = QHBoxLayout()
        optRow.setSpacing(20)

        self.miflashEraseCb = QCheckBox("清除数据（userdata / cache / metadata）")
        self.miflashEraseCb.setChecked(True)
        self.miflashEraseCb.setEnabled(False)
        self.miflashEraseCb.setCursor(Qt.CursorShape.PointingHandCursor)
        optRow.addWidget(self.miflashEraseCb)

        self.miflashLockCb = QCheckBox("回锁 Bootloader（重新上锁）")
        self.miflashLockCb.setChecked(False)
        self.miflashLockCb.setEnabled(False)
        self.miflashLockCb.setCursor(Qt.CursorShape.PointingHandCursor)
        optRow.addWidget(self.miflashLockCb)

        optRow.addStretch()
        v.addLayout(optRow)

        # 脚本信息
        self.miflashInfo = QLabel("尚未选择脚本")
        self.miflashInfo.setObjectName("info")
        self.miflashInfo.setWordWrap(True)
        v.addWidget(self.miflashInfo)

        # 开始按钮
        mRow2 = QHBoxLayout()
        mRow2.setSpacing(10)
        self.miflashStartBtn = QPushButton("🚀  开始 MiFlash 刷写")
        self.miflashStartBtn.setObjectName("primary")
        self.miflashStartBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.miflashStartBtn.setFixedHeight(44)
        self.miflashStartBtn.setFixedWidth(220)
        self.miflashStartBtn.setEnabled(False)
        self.miflashStartBtn.clicked.connect(self.do_miflash_start)
        mRow2.addWidget(self.miflashStartBtn)
        mRow2.addStretch()
        v.addLayout(mRow2)

        v.addStretch()

        # ============ 进度条 ============
        progRow = QHBoxLayout()
        progRow.setSpacing(10)
        plb = QLabel("刷入进度")
        plb.setObjectName("info")
        progRow.addWidget(plb)
        self.flashStageLabel = QLabel("等待开始")
        self.flashStageLabel.setObjectName("info")
        progRow.addWidget(self.flashStageLabel)
        self.flashProg = QProgressBar()
        self.flashProg.setRange(0, 100)
        self.flashProg.setValue(0)
        self.flashProg.setFixedHeight(10)
        self.flashProg.setTextVisible(False)
        progRow.addWidget(self.flashProg, 1)
        v.addLayout(progRow)
        self.flashProgressAnim = QPropertyAnimation(
            self.flashProg, b"value", self.flashProg)
        self.flashProgressAnim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 内部状态
        self._miflash_script = None
        self._miflash_cmds = []
        self._miflash_running = False

        self._on_part_changed(0)
        return w

    # ==================== 单分区刷入逻辑（原有） ====================
    def _on_part_changed(self, idx):
        t = self.partCombo.currentText()
        self.customPartE.setVisible(t.startswith("—"))
        p = self._current_part()
        mode = self._target_mode()
        tips = {
            "boot": "Android 12 及以下（或 GKI 完整 boot）",
            "init_boot": "Android 13+ GKI 设备，需 fastbootd",
            "recovery": "恢复模式分区（第三方 recovery 刷这里）",
            "vendor_boot": "Android 12+ GKI vendor_boot，需 fastbootd",
            "dtbo": "设备树覆盖表（一般跟 boot 一起刷）",
            "vbmeta_system": "系统校验元数据",
            "super": "动态分区总容器，需 fastbootd",
        }
        tip = tips.get(p, f"自定义分区：{p}" if p else "请输入自定义分区名")
        need = "fastbootd（用户空间）" if mode == "fastbootd" else "bootloader（传统 fastboot）"
        self.partTip.setText(f"{tip}\n需要模式：{need}")

    def _current_part(self):
        t = self.partCombo.currentText()
        if t.startswith("—"):
            n = self.customPartE.text().strip()
            return n if n else ""
        return t

    def _target_mode(self):
        p = self._current_part()
        return "fastbootd" if p in FASTBOOTD_PARTS else "bootloader"

    def _set_flash_prog(self, val):
        try:
            if not hasattr(self, "flashProg"):
                return
            if val < 0:
                self.flashProg.setRange(0, 0)
            else:
                self.flashProg.setRange(0, 100)
                target = max(0, min(100, int(val)))
                current = self.flashProg.value()
                self.flashProgressAnim.stop()
                self.flashProgressAnim.setStartValue(current)
                self.flashProgressAnim.setEndValue(target)
                self.flashProgressAnim.setDuration(
                    max(350, min(1400, abs(target - current) * 35)))
                self.flashProgressAnim.start()
                if target >= 90:
                    stage = "校验中…"
                elif target >= 55:
                    stage = "写入中…"
                elif target >= 20:
                    stage = "发送中…"
                else:
                    stage = "准备中…"
                self.flashStageLabel.setText(stage)
        except Exception:
            pass

    def do_pick(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择镜像文件", "",
            "IMG 镜像 (*.img);;所有文件 (*)")
        if not f:
            return
        self.img = Path(f)
        self.imgE.setText(str(self.img))
        self.lg(f"已选择镜像：{self.img}", "info")
        self.flBtn.setEnabled(True)

    def do_flash(self):
        if not self.img:
            QMessageBox.warning(self, "未选择镜像", "请先选择镜像文件。")
            return
        part = self._current_part()
        if not part:
            QMessageBox.warning(self, "未指定分区", "请选择或输入目标分区。")
            return
        mode = self._target_mode()
        special = SPECIAL_OPTS.get(part, [])
        opts_tip = f"\n附加参数：{' '.join(special)}" if special else ""
        r = QMessageBox.question(
            self, "确认刷写",
            f"即将刷入分区：{part}\n镜像：{self.img}\n"
            f"目标模式：{mode}{opts_tip}\n\n"
            f"⚠ 刷错分区可能导致无法开机！\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return

        self.sig.flashprog.emit(2)

        def w():
            fs, _ = fb_dev()
            if not fs:
                self.sig.log.emit("❌ 设备不在 Fastboot/Fastbootd 模式", "error")
                self.sig.log.emit("   请先点“进入刷写模式”", "warn")
                self.sig.flashprog.emit(0)
                return
            cmd = ["fastboot", "-s", fs[0]]
            if special:
                cmd += special
            cmd += ["flash", part, str(self.img)]
            self.sig.log.emit(f"开始刷入 {part} …", "info")
            self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")

            def on_text(line):
                s = (line or "").strip()
                if not s:
                    return
                low = s.lower()
                if "sending" in low:
                    self.sig.flashprog.emit(25)
                elif "writing" in low or "flashing" in low:
                    self.sig.flashprog.emit(65)
                elif "finished" in low:
                    self.sig.flashprog.emit(90)
                self.sig.log.emit(s, "plain")

            rc, out = sh_stream(cmd, on_text=on_text, timeout=600)
            low = (out or "").lower()
            if rc == 0 and "failed" not in low and "error" not in low:
                self.sig.flashprog.emit(100)
                self.sig.log.emit(f"✅ 刷入 {part} 成功", "ok")
                QTimer.singleShot(0, self._ask_reboot)
            else:
                self.sig.flashprog.emit(0)
                self.sig.log.emit(f"❌ 刷入失败（返回码 {rc}）", "error")

        threading.Thread(target=w, daemon=True).start()

    def _ask_reboot(self):
        r = QMessageBox.question(
            self, "刷入成功", "刷入成功！是否重启到系统？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.do_reboot("system")

    def do_enter(self):
        def w():
            ds, _ = adb_dev()
            if not ds:
                fs, _ = fb_dev()
                if fs:
                    self.sig.log.emit("设备已在 Fastboot/Fastbootd", "ok")
                    self.sig.dev.emit(f"● FB: {fs[0]}", self.theme["ok"])
                    return
                self.sig.log.emit("没有 ADB 设备，请连接手机并授权 USB 调试", "error")
                return
            mode = self._target_mode()
            part = self._current_part()
            cmd = ["adb", "-s", ds[0], "reboot",
                   "bootloader" if mode == "bootloader" else "fastboot"]
            nm = "bootloader" if mode == "bootloader" else "fastbootd"
            self.sig.log.emit(f"分区 {part} 需要进入 {nm}", "info")
            self.sig.log.emit(f"执行：{' '.join(cmd)}", "info")
            rc, out = sh(cmd, timeout=15)
            if out:
                self.sig.log.emit(out, "plain")
            self.sig.log.emit(f"等待进入 {nm} …", "info")
            for _ in range(30):
                time.sleep(2)
                fs, _ = fb_dev()
                if fs:
                    self.sig.log.emit(f"✓ 已进入 Fastboot：{fs[0]}", "ok")
                    self.sig.dev.emit(f"● FB: {fs[0]}", self.theme["ok"])
                    return
            self.sig.log.emit("⚠ 未检测到 fastboot 设备，请手动检查屏幕", "warn")
        threading.Thread(target=w, daemon=True).start()

    # ==================== MiFlash 一键刷写 ====================
    def do_miflash_pick(self):
        """选择脚本：支持选 .bat 文件，或直接选解压好的刷机包目录"""
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 flash_all.bat（或解压包内的任意 .bat）", "",
            "刷机脚本 (*.bat);;所有文件 (*)")
        if not f:
            # 尝试选目录
            d = QFileDialog.getExistingDirectory(self, "或者选择刷机包目录")
            if not d:
                return
            f = self._miflash_find_bat(d)
            if not f:
                QMessageBox.warning(
                    self, "未找到脚本",
                    "该目录下没找到 flash_all*.bat 脚本。\n"
                    "请确认选择的是解压后的刷机包目录。")
                return
        self._miflash_load(f)

    def _miflash_find_bat(self, folder):
        """在目录（含子目录一层）里查找 flash_all*.bat"""
        cands = ["flash_all.bat", "flash_all_lock.bat",
                 "flash_all_except_storage.bat",
                 "flash_all_except_data_storage.bat",
                 "flash_all_except_data.bat",
                 "flash_all_wipe.bat"]
        p = Path(folder)
        for name in cands:
            if (p / name).exists():
                return str(p / name)
        # 兜底：模糊匹配
        for child in p.glob("flash*.bat"):
            return str(child)
        return None

    def _miflash_load(self, script_path):
        self._miflash_script = script_path
        cmds = parse_miflash_script(script_path)

        self.miflashScriptE.setText(script_path)
        self._miflash_cmds = cmds

        if not cmds:
            self.miflashInfo.setText("⚠ 脚本中没有解析出任何 fastboot 命令")
            self.miflashInfo.setStyleSheet(
                f"color: {self.theme['warn']}; background: transparent;")
            self.miflashStartBtn.setEnabled(False)
            self.miflashEraseCb.setEnabled(False)
            self.miflashLockCb.setEnabled(False)
            return

        # 分类统计
        n_flash = sum(1 for c in cmds if c["type"] == "flash")
        n_erase = sum(1 for c in cmds if c["type"] == "erase")
        n_lock = sum(1 for c in cmds if c["type"] == "lock")
        has_reboot = any(c["type"] == "reboot" for c in cmds)

        # 判断脚本类型提示
        name_low = Path(script_path).name.lower()
        if "lock" in name_low:
            kind_tip = "回锁版"
        elif "except_storage" in name_low or "except_data" in name_low:
            kind_tip = "不清除数据版"
        else:
            kind_tip = "完整版"

        lines = [
            f"✓ 已加载脚本：{Path(script_path).name}    （{kind_tip}）",
            f"  · 刷写命令：{n_flash} 条",
            f"  · 擦除命令：{n_erase} 条",
            f"  · 回锁命令：{n_lock} 条",
            f"  · 重启命令：{'是' if has_reboot else '无'}",
        ]
        self.miflashInfo.setText("\n".join(lines))
        self.miflashInfo.setStyleSheet(
            f"color: {self.theme['text']}; background: transparent;")

        # 勾选框启用状态
        self.miflashEraseCb.setEnabled(n_erase > 0)
        self.miflashEraseCb.setChecked(n_erase > 0)
        self.miflashLockCb.setEnabled(n_lock > 0)
        # 回锁默认不勾选（危险操作）
        self.miflashLockCb.setChecked(False)

        self.miflashStartBtn.setEnabled(True)
        self.lg(f"MiFlash 脚本已加载：{Path(script_path).name}"
                f"（刷写 {n_flash} / 擦除 {n_erase} / 回锁 {n_lock}）", "info")
        # 在日志中列出前 10 条
        for i, c in enumerate(cmds[:10], 1):
            if c["type"] == "flash":
                self.lg(f"  [{i}] flash {c['partition']} <- {Path(c['image']).name}", "plain")
            elif c["type"] == "erase":
                self.lg(f"  [{i}] erase {c['partition']}", "plain")
            elif c["type"] == "lock":
                self.lg(f"  [{i}] {c['cmd']}", "plain")
            elif c["type"] == "reboot":
                self.lg(f"  [{i}] reboot", "plain")
        if len(cmds) > 10:
            self.lg(f"  …（共 {len(cmds)} 条）", "plain")

    def do_miflash_start(self):
        if self._miflash_running:
            self.lg("MiFlash 正在刷写中，请稍候…", "warn")
            return
        if not self._miflash_cmds:
            QMessageBox.warning(self, "未加载脚本", "请先选择 flash_all.bat 脚本。")
            return

        will_erase = self.miflashEraseCb.isChecked()
        will_lock = self.miflashLockCb.isChecked()

        # 拼出确认信息
        tip_lines = ["即将开始 MiFlash 一键刷写", ""]
        tip_lines.append(f"脚本：{Path(self._miflash_script).name}")
        tip_lines.append(f"刷写命令：{sum(1 for c in self._miflash_cmds if c['type']=='flash')} 条")
        if any(c["type"] == "erase" for c in self._miflash_cmds):
            tip_lines.append(f"清除数据：{'✅ 是' if will_erase else '❌ 否（跳过擦除）'}")
        if any(c["type"] == "lock" for c in self._miflash_cmds):
            tip_lines.append(f"回锁 BL ：{'⚠ 是' if will_lock else '❌ 否（跳过回锁）'}")
        tip_lines.append("")
        tip_lines.append("⚠ 刷机有风险！确认继续？")

        r = QMessageBox.question(
            self, "确认刷写",
            "\n".join(tip_lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return

        # 回锁二次确认
        if will_lock:
            r2 = QMessageBox.warning(
                self, "⚠ 回锁确认",
                "即将回锁 Bootloader！\n\n"
                "回锁后设备将无法再刷入非官方系统，\n"
                "重新解锁需要再次申请，并会清空所有数据。\n\n"
                "确定要回锁吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r2 != QMessageBox.StandardButton.Yes:
                self.lg("已取消回锁，继续刷写但不回锁", "warn")
                will_lock = False

        # 检查设备
        fs, _ = fb_dev()
        if not fs:
            QMessageBox.warning(
                self, "未检测到设备",
                "请确保手机已进入 Fastboot 模式。")
            return

        self._miflash_running = True
        self.miflashStartBtn.setEnabled(False)
        self.miflashStartBtn.setText("⏳  刷写中…")
        self.miflashPickBtn.setEnabled(False)
        self.flashProg.setRange(0, 100)
        self.flashProg.setValue(0)

        threading.Thread(
            target=self._miflash_worker,
            args=(fs[0], will_erase, will_lock),
            daemon=True,
        ).start()

    def _miflash_worker(self, dev_id, will_erase, will_lock):
        try:
            cmds = self._miflash_cmds
            # 过滤出要执行的命令
            todo = []
            for c in cmds:
                if c["type"] == "erase":
                    if not will_erase:
                        continue
                    todo.append(c)
                elif c["type"] == "flash":
                    todo.append(c)
                elif c["type"] == "lock":
                    if not will_lock:
                        continue
                    todo.append(c)
                # reboot 命令不在这里执行（后面统一询问）

            total = len(todo)
            if total == 0:
                self.sig.log.emit("⚠ 没有需要执行的命令（都被跳过了）", "warn")
                self.sig.flashprog.emit(0)
                return

            self.sig.log.emit("=" * 60, "plain")
            self.sig.log.emit(
                f"🚀 MiFlash 开始刷写：共 {total} 条命令（设备 {dev_id}）", "info")
            self.sig.log.emit(
                f"   清除数据：{'是' if will_erase else '否'}   "
                f"回锁 BL：{'是' if will_lock else '否'}", "plain")
            self.sig.log.emit("=" * 60, "plain")

            success = 0
            failed = 0
            failed_parts = []

            for i, c in enumerate(todo, 1):
                percent = int((i - 1) * 100 / total)
                self.sig.flashprog.emit(percent)

                if c["type"] == "flash":
                    part = c["partition"]
                    img = c["image"]
                    if not Path(img).exists():
                        self.sig.log.emit(
                            f"[{i}/{total}] ❌ 文件不存在：{img}", "error")
                        failed += 1
                        failed_parts.append(f"{part}(文件缺失)")
                        continue
                    self.sig.log.emit(
                        f"[{i}/{total}] 正在刷写 {part} ← {Path(img).name}", "info")
                    cmd = ["fastboot", "-s", dev_id, "flash", part, img]

                elif c["type"] == "erase":
                    part = c["partition"]
                    self.sig.log.emit(
                        f"[{i}/{total}] 正在擦除 {part} …", "info")
                    cmd = ["fastboot", "-s", dev_id, "erase", part]

                elif c["type"] == "lock":
                    self.sig.log.emit(
                        f"[{i}/{total}] 正在回锁：{c['cmd']} …", "warn")
                    cmd = ["fastboot", "-s", dev_id] + c["cmd"].split()

                else:
                    continue

                def on_text(line):
                    s = (line or "").strip()
                    if not s:
                        return
                    self.sig.log.emit(f"    {s}", "plain")

                rc, out = sh_stream(cmd, on_text=on_text, timeout=1800)
                low = (out or "").lower()
                ok = rc == 0 and "failed" not in low and "error" not in low

                if ok:
                    success += 1
                    self.sig.log.emit(f"[{i}/{total}] ✅ 完成", "ok")
                else:
                    failed += 1
                    failed_parts.append(f"{c.get('partition', c['type'])}")
                    self.sig.log.emit(
                        f"[{i}/{total}] ❌ 失败（返回码 {rc}）", "error")

            self.sig.flashprog.emit(100)
            self.sig.log.emit("=" * 60, "plain")
            self.sig.log.emit(
                f"🏁 MiFlash 刷写完成：成功 {success} / 失败 {failed}",
                "ok" if failed == 0 else "warn")
            if failed_parts:
                self.sig.log.emit(f"   失败项：{', '.join(failed_parts)}", "error")
            self.sig.log.emit("=" * 60, "plain")

        except Exception as e:
            self.sig.log.emit(f"❌ MiFlash 刷写异常：{e}", "error")
            self.sig.log.emit(traceback.format_exc(), "error")

        finally:
            # 子线程里不能直接用 QTimer，改为 emit 信号，主线程执行
            self.sig.miflash_done.emit()
            
    def _miflash_finalize(self):
        """MiFlash 刷写结束回调（主线程执行）—— 恢复 UI 并询问是否重启"""
        self._miflash_running = False
        self.miflashStartBtn.setEnabled(True)
        self.miflashStartBtn.setText("🚀  开始 MiFlash 刷写")
        self.miflashPickBtn.setEnabled(True)
        cmds = getattr(self, "_miflash_cmds", [])
        has_erase = any(c["type"] == "erase" for c in cmds)
        has_lock = any(c["type"] == "lock" for c in cmds)
        self.miflashEraseCb.setEnabled(has_erase)
        self.miflashLockCb.setEnabled(has_lock)
        self._miflash_ask_reboot()

    def _miflash_ask_reboot(self):
        r = QMessageBox.question(
            self, "刷写完成",
            "MiFlash 刷写完成。\n\n是否立即重启手机？\n"
            "（回锁后的手机第一次开机可能会自动清除数据，属正常现象）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.sig.log.emit("执行：fastboot reboot", "info")
            def w():
                fs, _ = fb_dev()
                if not fs:
                    self.sig.log.emit("❌ 未检测到 fastboot 设备", "error")
                    return
                rc, out = sh(["fastboot", "-s", fs[0], "reboot"], timeout=20)
                if out:
                    self.sig.log.emit(out, "plain")
                self.sig.log.emit(
                    "✓ 已发送重启命令" if rc == 0 else f"❌ 失败（{rc}）",
                    "ok" if rc == 0 else "error")
            threading.Thread(target=w, daemon=True).start()
        else:
            self.sig.log.emit("已取消重启，可手动执行 fastboot reboot", "info")