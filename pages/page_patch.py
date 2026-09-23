# -*- coding: utf-8 -*-
"""页面 2：Preloader 修补"""
import threading, traceback
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QProgressBar, QMessageBox, QFileDialog,
)
from PyQt6.QtCore import Qt
from core.config import SIZE_OK, MAGIC, OUT_DIR, ZIP_MAGICS, AUTHOR


class PatchPageMixin:

    def _page_patch(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        t = QLabel("🔧  Preloader 修补")
        t.setObjectName("cardTitle")
        v.addWidget(t)
        d = QLabel("输入从设备导出的 preloader 文件（boot1.bin / boot2.bin）")
        d.setObjectName("desc")
        v.addWidget(d)

        v.addWidget(self._label("文件路径"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.pE = QLineEdit()
        self.pE.setReadOnly(True)
        self.pE.setPlaceholderText("请选择 preloader 文件…")
        self.pE.setFixedHeight(42)
        row.addWidget(self.pE, 1)
        self.brBtn = QPushButton("📂  选择")
        self.brBtn.setObjectName("primary")
        self.brBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brBtn.setFixedHeight(42)
        self.brBtn.setFixedWidth(110)
        self.brBtn.clicked.connect(self.pick_pre)
        row.addWidget(self.brBtn)
        v.addLayout(row)

        self.pInfo = QLabel("")
        self.pInfo.setObjectName("info")
        v.addWidget(self.pInfo)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        self.dot = QLabel("●")
        self.dot.setObjectName("dot")
        row2.addWidget(self.dot)
        self.sTxt = QLabel("未选择文件")
        self.sTxt.setObjectName("status")
        row2.addWidget(self.sTxt)
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.setFixedHeight(10)
        self.pbar.setTextVisible(False)
        row2.addWidget(self.pbar, 1)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(10)
        self.stBtn = QPushButton("🚀  开始修补")
        self.stBtn.setObjectName("primary")
        self.stBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stBtn.setFixedHeight(44)
        self.stBtn.setFixedWidth(180)
        self.stBtn.setEnabled(False)
        self.stBtn.clicked.connect(self.run_patch)
        row3.addWidget(self.stBtn)
        self.oBtn = QPushButton("📁  选择输出目录")
        self.oBtn.setObjectName("secondary")
        self.oBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.oBtn.setFixedHeight(44)
        self.oBtn.setFixedWidth(200)
        self.oBtn.clicked.connect(self.outdir_ask)
        row3.addWidget(self.oBtn)
        row3.addStretch()
        v.addLayout(row3)
        v.addStretch()
        return w

    def _dotc(self, t):
        c = self.theme
        return {"未选择文件": c["dim"], "已就绪": c["ok"],
                "处理中…": c["warn"], "完成": c["ok"],
                "失败": c["err"]}.get(t, c["dim"])

    def _set_stat(self, t):
        self.sTxt.setText(t)
        self.dot.setStyleSheet(f"color: {self._dotc(t)}; font-size: 16pt;")

    def _set_prog(self, val):
        try:
            self.pbar.setValue(max(0, min(100, int(val))))
        except Exception:
            pass

    def _reset_patch_state(self, status_text="未选择文件"):
        self.working = False
        self.outpath = None
        try:
            self.stBtn.setEnabled(False)
            self.stBtn.setText("🚀  开始修补")
            self.pbar.setValue(0)
            self._set_stat(status_text)
        except Exception:
            pass

    def pick_pre(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 preloader 文件", "",
            "BIN 文件 (*.bin);;所有文件 (*)")
        if not f:
            return

        self._reset_patch_state("未选择文件")
        self.src = None
        self.pE.setText("")
        self.pInfo.setText("")

        p = Path(f)
        name_low = p.name.lower()

        try:
            head = p.read_bytes()[:16]
        except Exception as e:
            QMessageBox.warning(self, "无法读取", f"读取文件失败：\n{e}")
            self._set_stat("未选择文件")
            return

        if any(head.startswith(m) for m in ZIP_MAGICS) or \
           name_low.endswith((".rar", ".zip", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst")):
            QMessageBox.warning(
                self, "文件类型错误",
                "❌ 你选择的是压缩包，不是 preloader 镜像！\n\n"
                "请先解压，然后选择解压出来的 boot1.bin / boot2.bin 等 .bin 文件。")
            self._set_stat("未选择文件")
            self.lg("❌ 拒绝了压缩包文件（请解压后选择 .bin）", "error")
            return

        if not name_low.endswith(".bin"):
            self.lg(f"⚠ 注意：文件后缀不是 .bin（{p.name}）", "warn")

        self.src = p
        self.pE.setText(str(p))
        s = p.stat().st_size
        if s == SIZE_OK:
            self.pInfo.setText(f"✓ 文件大小：{s:,} 字节（{hex(s)}），与 0x400000 一致")
            self.pInfo.setStyleSheet(f"color: {self.theme['ok']}; background: transparent;")
        else:
            self.pInfo.setText(f"⚠ 文件大小：{s:,} 字节（{hex(s)}），期望 0x{SIZE_OK:X}")
            self.pInfo.setStyleSheet(f"color: {self.theme['warn']}; background: transparent;")
        self.lg(f"已选择：{self.src}", "info")
        self.stBtn.setEnabled(True)
        self._set_stat("已就绪")

    def outdir_ask(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.outdir = Path(d)
            self.lg(f"输出目录：{self.outdir}", "info")

    def run_patch(self):
        if self.working:
            self.lg("修补任务正在运行，请稍候…", "warn")
            return
        if not self.src:
            QMessageBox.warning(self, "未选择文件", "请先选择 preloader 文件。")
            return

        self.working = True
        self.stBtn.setEnabled(False)
        self.stBtn.setText("⏳  修补中…")
        self._set_stat("处理中…")
        self.pbar.setValue(0)
        self.lg("─" * 60)
        self.lg(f"开始修补：{self.src.name}", "info")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            self._do_patch()
        except Exception as e:
            self.sig.log.emit(f"[错误] {e}", "error")
            self.sig.log.emit(traceback.format_exc(), "error")
            try:
                if self.outpath and Path(self.outpath).exists():
                    Path(self.outpath).unlink()
                    self.sig.log.emit(f"已删除未完成的输出：{self.outpath}", "warn")
            except Exception:
                pass
            self.outpath = None
            self.sig.patch_fail.emit(str(e))
            return
        self.sig.patch_ok.emit()

    def _do_patch(self):
        src = self.src
        data = bytearray(src.read_bytes())
        size = len(data)

        head16 = bytes(data[:16])
        if any(head16.startswith(m) for m in ZIP_MAGICS):
            raise Exception("选择的文件是压缩包，不是 preloader 镜像，请解压后选择 .bin 文件")

        self.outdir.mkdir(parents=True, exist_ok=True)
        out = self.outdir / src.name

        self.sig.log.emit(f"读取文件：{size:,} 字节（{hex(size)}）", "plain")
        if head16.startswith(b"UFS_BOOT"):
            self.sig.log.emit("内存类型：UFS_BOOT", "plain")
        elif head16.startswith(b"EMMC_BOOT"):
            self.sig.log.emit("内存类型：EMMC_BOOT", "plain")
        elif head16.startswith(b"COMBO_BOOT"):
            self.sig.log.emit("内存类型：COMBO_BOOT（UFS）", "plain")
        elif head16.startswith(b"MMM\x018\x00\x00\x00FILE_INF"):
            raise Exception("RAW 格式 preloader 不支持")
        else:
            self.sig.log.emit(f"内存类型：{head16!r}（未知）", "warn")
        self.sig.prog.emit(20)

        pos = data.find(MAGIC)
        if pos == -1:
            raise Exception("未找到 flag block 魔数（不是有效的 preloader 镜像）")
        self.sig.log.emit("✓ 找到 flag block", "ok")
        flag = bytes(data[pos:pos + 0x78])
        lk = data[pos + 0x4C]
        if lk == 0x22:
            self.sig.log.emit("锁定状态：22（已锁定）", "plain")
        elif lk == 0x11:
            self.sig.log.emit("锁定状态：11（硬锁定）", "plain")
        else:
            self.sig.log.emit(f"锁定状态：{hex(lk)}（已解锁）", "ok")
        self.sig.prog.emit(40)

        co = data[0x20D] * 256
        c1, c2, c3, c4, c5 = (data[0x21D], data[0x211], data[0x212],
                              data[0x221], data[0x222])
        self.sig.log.emit(f"代码偏移 = 0x{co:X}", "plain")
        if co >= size:
            raise Exception(f"代码偏移 0x{co:X} 超过文件大小，文件可能损坏")
        raw = bytes(data[co:size - 0x3000]) if size > 0x3000 else b""
        data[co:] = b"\x00" * (size - co)
        if 0x2000 - co >= 0:
            self.sig.log.emit(f"代码跳转：0x{co:X} → 0x2000", "plain")
            if 0x2000 + len(raw) > size:
                raise Exception("代码段超出文件末尾，文件可能不完整")
            data[0x2000:0x2000 + len(raw)] = raw
        else:
            raise Exception("代码缩进超过 0x2000")
        self.sig.prog.emit(60)

        self.sig.log.emit("修改 BRLYT 偏移", "plain")
        data[0x20D] = 0x20
        data[0x21D] = 0x20
        data[0x211] = 0x10
        data[0x212] = 0x10
        data[0x221] = 0x10
        data[0x222] = 0x10
        self.sig.log.emit(f"0x20d：{co // 256:02x} → 20 | 0x21d：{c1:02x} → 20", "plain")
        self.sig.log.emit(f"0x211：{c2:02x} → 10 | 0x212：{c3:02x} → 10", "plain")
        self.sig.log.emit(f"0x221：{c4:02x} → 10 | 0x222：{c5:02x} → 10", "plain")
        self.sig.prog.emit(80)

        data[0x1000:0x1000 + len(flag)] = flag
        self.sig.log.emit(f"Fastboot 锁定标志：0x{lk:02x} → 00", "plain")
        data[0x104C] = 0x00
        self.sig.prog.emit(95)

        self.outpath = None
        out.write_bytes(bytes(data))
        self.outpath = out
        self.sig.log.emit(f"✅ 已生成：{out.resolve()}", "ok")
        self.sig.prog.emit(100)

    def _ok(self):
        self.working = False
        self.stBtn.setEnabled(True)
        self.stBtn.setText("🚀  开始修补")
        self._set_stat("完成")
        self.lg("修补完成。", "ok")
        QMessageBox.information(
            self, "成功",
            f"已生成：\n{self.outpath.resolve()}\n\n"
            f"后续：用 mtkclient / GeekFlashTool 写回设备\n\n—— {AUTHOR}")

    def _fail(self, err):
        self.working = False
        self.stBtn.setEnabled(True)
        self.stBtn.setText("🚀  开始修补")
        self._set_stat("失败")
        self.pbar.setValue(0)
        self.lg(f"❌ 修补失败，已终止：{err}", "error")
        QMessageBox.critical(
            self, "修补失败",
            f"❌ 修补失败，已立即停止！\n\n原因：{err}\n\n"
            f"请重新点击「📂 选择」按钮选择正确的 preloader 文件后再试。")