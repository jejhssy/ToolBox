# -*- coding: utf-8 -*-
"""页面：Payload 可视化（本地）

设计参考 VioletToolBox 的「Payload 可视化」面板，但**全部本地化**：
  · 只读本地 payload.bin / 全量包 ZIP / 7z，不联网
  · 先展示概览信息（版本、包类型、block_size、时间戳、安全补丁、动态分区、APEX…）
  · 分区表可视化（勾选 + 大小 + 操作数 + 备注），双击看操作分布
  · 按需提取所选分区；普通压缩包（无 payload.bin）也能列目录并提取
解析内核：core/payload_parser.py（纯 Python，无需外部工具）
"""
import os
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path

try:
    import py7zr
except ImportError:          # 7z 支持可选
    py7zr = None

from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QMessageBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QTextEdit, QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush

from core.config import OUT_DIR
from core.widgets import CheckCellDelegate, RowTintDelegate, AnimatedProgressBar, \
    set_state, state_from_color
from core.payload_parser import (
    PayloadParser, PayloadExtractor, PayloadError, ExtractionCancelled,
)

ARCHIVE_SUFFIXES = (".zip", ".7z")


class _ZipPayloadSource:
    """直接从 zip 里随机读取 payload.bin（仅未压缩条目）

    OTA 包里的 payload.bin 通常是 STORED（内容本身已压缩），
    这样可以省掉"把几 GB 解到磁盘再读回来"的步骤。
    """

    def __init__(self, zip_path, entry):
        self._zf = zipfile.ZipFile(zip_path)
        info = self._zf.getinfo(entry)
        self._fh = self._zf.open(entry)
        self.name = Path(entry).name
        self.size = int(info.file_size)

    def read(self, n=-1):
        return self._fh.read(n)

    def seek(self, pos, whence=0):
        return self._fh.seek(pos, whence)

    def tell(self):
        return self._fh.tell()

    def close(self):
        for obj in (self._fh, self._zf):
            try:
                obj.close()
            except Exception:
                pass


class PayloadPageMixin:

    # ================================================================
    # 页面构建
    # ================================================================
    def _page_payload(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        t = QLabel("🧩  Payload 可视化（本地）")
        t.setObjectName("cardTitle")
        v.addWidget(t)
        d = QLabel("读取本地 payload.bin / 全量包 ZIP / 7z，查看分区信息并按需提取。"
                   "点任意一行即可勾选 / 取消。全程本地，不联网。")
        d.setObjectName("desc")
        d.setWordWrap(True)
        v.addWidget(d)

        # ---- 源文件 ----
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(self._label("源"))
        self.plSrcE = QLineEdit()
        self.plSrcE.setReadOnly(True)
        self.plSrcE.setPlaceholderText("选择 payload.bin、OTA 全量包 .zip 或 .7z")
        self.plSrcE.setFixedHeight(40)
        r1.addWidget(self.plSrcE, 1)
        self.plPickBtn = QPushButton("📂  本地文件")
        self.plPickBtn.setObjectName("secondary")
        self.plPickBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plPickBtn.setFixedHeight(40)
        self.plPickBtn.setFixedWidth(130)
        self.plPickBtn.clicked.connect(self.do_pl_pick)
        r1.addWidget(self.plPickBtn)
        self.plReadBtn = QPushButton("🔍  读取信息")
        self.plReadBtn.setObjectName("primary")
        self.plReadBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plReadBtn.setFixedHeight(40)
        self.plReadBtn.setFixedWidth(130)
        self.plReadBtn.clicked.connect(self.do_pl_read)
        r1.addWidget(self.plReadBtn)
        v.addLayout(r1)

        # ---- 概览信息 ----
        infoCard = QFrame()
        infoCard.setObjectName("card")
        iv = QVBoxLayout(infoCard)
        iv.setContentsMargins(12, 8, 12, 8)
        iv.setSpacing(4)
        self.plInfo = QTextEdit()
        self.plInfo.setObjectName("log")
        self.plInfo.setReadOnly(True)
        self.plInfo.setFixedHeight(150)
        self.plInfo.setPlaceholderText("payload 概览信息会显示在这里（版本 / 包类型 / 时间戳 / 安全补丁 …）")
        iv.addWidget(self.plInfo)
        v.addWidget(infoCard)

        # ---- 分区表工具行 ----
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(self._label("分区列表"))
        self.plSelLb = QLabel("已选 0 项")
        self.plSelLb.setObjectName("info")
        r2.addWidget(self.plSelLb)
        # 剩余空间放在这里 → 左边的文字标签永远不会被挤到裁切
        r2.addStretch(1)
        self.plSearchE = QLineEdit()
        self.plSearchE.setPlaceholderText("搜索分区…")
        self.plSearchE.setClearButtonEnabled(True)
        self.plSearchE.setFixedHeight(30)
        self.plSearchE.setFixedWidth(200)
        self.plSearchE.textChanged.connect(self._pl_filter)
        r2.addWidget(self.plSearchE)
        for text, cb in (("全选", lambda: self._pl_check_all(True)),
                         ("取消", lambda: self._pl_check_all(False)),
                         ("反选", self._pl_invert),
                         ("仅全量", self._pl_select_full_only)):
            b = QPushButton(text)
            b.setObjectName("ghostSmall")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(30)
            # 不写死宽度：按文字自适应，避免"仅全量"这种较长文字被截断
            b.setMinimumWidth(64)
            b.clicked.connect(cb)
            r2.addWidget(b)
        v.addLayout(r2)

        # ---- 分区表 ----
        self.plTable = QTableWidget()
        self.plTable.setObjectName("oplusTable")
        self.plTable.setColumnCount(5)
        self.plTable.setHorizontalHeaderLabels(
            ["选择", "名称", "大小", "操作数", "备注"])
        hh = self.plTable.horizontalHeader()
        hh.setFixedHeight(32)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(0, 80)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(1, 190)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(2, 110)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(3, 70)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.plTable.verticalHeader().setVisible(False)
        self.plTable.verticalHeader().setDefaultSectionSize(36)
        self.plTable.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.plTable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.plTable.setAlternatingRowColors(True)
        self.plTable.setShowGrid(False)
        self.plTable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.plTable.setMinimumHeight(210)
        # ---- 自绘"选择"列：大号圆角勾选框（默认小勾太不显眼）----
        self._pl_check_delegate = CheckCellDelegate(
            self.plTable, self.theme["acc"], self.theme["line"],
            self.theme["log"], self.theme["acc2"], self.theme["dim"],
            self.theme["ok"])
        self.plTable.setItemDelegateForColumn(0, self._pl_check_delegate)
        # 其它列：委托负责把"勾选行"的整行底色铺满（QSS 会吃掉 BackgroundRole）
        self._pl_row_delegate = RowTintDelegate(self.plTable)
        self.plTable.setItemDelegate(self._pl_row_delegate)
        self.plTable.itemChanged.connect(self._pl_on_item_changed)
        self.plTable.cellClicked.connect(self._pl_on_cell_clicked)
        self.plTable.itemDoubleClicked.connect(self._pl_show_detail)
        v.addWidget(self.plTable, 1)

        # ---- 输出 / 进度 ----
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(self._label("输出目录"))
        self.plOutE = QLineEdit(str(Path(OUT_DIR) / "extracted"))
        self.plOutE.setFixedHeight(36)
        r3.addWidget(self.plOutE, 1)
        self.plOutBtn = QPushButton("📁  选择")
        self.plOutBtn.setObjectName("secondary")
        self.plOutBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plOutBtn.setFixedHeight(36)
        self.plOutBtn.setFixedWidth(90)
        self.plOutBtn.clicked.connect(self._pl_pick_outdir)
        r3.addWidget(self.plOutBtn)
        self.plExtractBtn = QPushButton("📤  提取选中")
        self.plExtractBtn.setObjectName("primary")
        self.plExtractBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plExtractBtn.setFixedHeight(36)
        self.plExtractBtn.setMinimumWidth(150)   # 文案带数量会变长 → 用最小宽自适应
        self.plExtractBtn.setEnabled(False)
        self.plExtractBtn.clicked.connect(self.do_pl_extract)
        r3.addWidget(self.plExtractBtn)
        self.plStopBtn = QPushButton("⏹  停止")
        self.plStopBtn.setObjectName("secondary")
        self.plStopBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plStopBtn.setFixedHeight(36)
        self.plStopBtn.setFixedWidth(90)
        self.plStopBtn.setEnabled(False)
        self.plStopBtn.clicked.connect(self.do_pl_stop)
        r3.addWidget(self.plStopBtn)
        v.addLayout(r3)

        r4 = QHBoxLayout()
        r4.setSpacing(10)
        self.plStatus = QLabel("等待选择文件…")
        self.plStatus.setObjectName("info")
        r4.addWidget(self.plStatus, 1)
        self.plStageLb = QLabel("")          # 当前正在处理的分区
        self.plStageLb.setObjectName("hint")
        r4.addWidget(self.plStageLb)
        v.addLayout(r4)

        # 进度条：单独一行、铺满宽度、条内显示百分比（比原来那一小条明显）
        self.plProg = AnimatedProgressBar()
        self.plProg.setFixedHeight(22)
        self.plProg.setFormat("空闲")
        self.plProg.set_colors(self.theme["acc"], self.theme["log"],
                               self.theme["text"], self.theme["line"],
                               self.theme["acc2"])
        v.addWidget(self.plProg)

        # ---- 内部状态 ----
        self._pl_src = ""            # 源文件路径
        self._pl_payload_path = ""   # 真正的 payload.bin（zip/7z 时是临时解出的）
        self._pl_src_reader = None   # zip 直读句柄（未压缩的 payload.bin）
        self._pl_parser = None
        self._pl_archive_mode = False
        self._pl_rows = []           # [(name, size_str, ops, note, payload_part)]
        self._pl_busy = False
        self._pl_cancel = threading.Event()
        self._pl_temp_dir = None
        self._pl_work = None
        # 自绘控件（勾选框 / 进度条 / 行配色）需要随主题刷新
        self._theme_hooks = getattr(self, "_theme_hooks", [])
        self._theme_hooks.append(self._pl_refresh_colors)
        return w

    # ================================================================
    # 基础工具
    # ================================================================
    def _pl_log(self, msg, level="plain"):
        try:
            self.lg(msg, level)
        except Exception:
            pass

    def _pl_set_prog(self, val, busy_text="处理中…"):
        """val < 0 → 未知进度（滚动动画）；否则 0~100 平滑推进并显示百分比"""
        try:
            if val is None or val < 0:
                self.plProg.set_busy(True, busy_text)
            else:
                v = max(0, min(100, int(val)))
                self.plProg.set_smooth_value(v, f"{v}%")
        except Exception:
            pass

    def _pl_stage(self, name):
        """显示当前正在处理的分区"""
        try:
            self.plStageLb.setText(f"当前：{name}" if name else "")
        except Exception:
            pass

    def _pl_progress_ui(self, val):
        """提取过程中的动态反馈：进度条 + 状态文字"""
        if not self._pl_busy:
            return                      # 任务已结束，忽略迟到的进度事件
        self._pl_set_prog(val)
        if 0 <= val < 100:
            self._pl_set_status(f"⏳ 正在提取 … {int(val)}%", self.theme["warn"])

    def _pl_refresh_colors(self):
        """主题切换后刷新自绘控件（QSS 管不到的部分）"""
        c = self.theme
        try:
            self.plProg.set_colors(c["acc"], c["log"], c["text"],
                                   c["line"], c["acc2"])
        except Exception:
            pass
        try:
            self._pl_check_delegate.set_colors(c["acc"], c["line"], c["log"],
                                               c["acc2"], c["dim"], c["ok"])
            self.plTable.viewport().update()
        except Exception:
            pass
        try:
            self._pl_apply_row_style()
        except Exception:
            pass

    def _pl_set_status(self, text, color=None):
        try:
            self.plStatus.setText(text)
            set_state(self.plStatus, state_from_color(self.theme, color))
        except Exception:
            pass

    def _pl_set_info(self, lines):
        try:
            self.plInfo.setPlainText("\n".join(lines))
        except Exception:
            pass

    # ---- 表格 ----
    def _pl_fill_table(self, rows):
        """rows: [(名称, 大小文本, 操作数, 备注)]"""
        self.plTable.blockSignals(True)
        self.plTable.setRowCount(0)
        for name, size_s, ops, note in rows:
            r = self.plTable.rowCount()
            self.plTable.insertRow(r)
            chk = QTableWidgetItem()
            # 必须带 ItemIsSelectable：否则整行选中时这一格不算"被选中"，
            # 高亮大横条会在勾选框这里断开（右边有、勾选列没有）
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.plTable.setItem(r, 0, chk)
            for col, text in ((1, name), (2, size_s), (3, str(ops)), (4, note)):
                it = QTableWidgetItem(text)
                it.setForeground(QColor(self.theme["dim"] if col == 4
                                        else self.theme["text"]))
                self.plTable.setItem(r, col, it)
        self.plTable.blockSignals(False)
        self._pl_rows = rows
        self._pl_update_sel()

    def _pl_on_item_changed(self, _item):
        self._pl_update_sel()

    def _pl_on_cell_clicked(self, row, col):
        """点表格里任意位置都能勾选/取消（不必非得点准那个方框）"""
        if col == 0:
            return                      # 第 0 列由自绘委托处理，避免被翻两次
        it = self.plTable.item(row, 0)
        if it is None:
            return
        it.setCheckState(Qt.CheckState.Unchecked
                         if it.checkState() == Qt.CheckState.Checked
                         else Qt.CheckState.Checked)

    def _pl_checked_names(self):
        out = []
        for r in range(self.plTable.rowCount()):
            it = self.plTable.item(r, 0)
            nm = self.plTable.item(r, 1)
            if it and nm and it.checkState() == Qt.CheckState.Checked:
                out.append(nm.text())
        return out

    def _pl_update_sel(self):
        n = len(self._pl_checked_names())
        total = self.plTable.rowCount()
        self.plSelLb.setText(f"已选 {n}/{total} 项")
        self.plExtractBtn.setEnabled(n > 0 and not self._pl_busy)
        self.plExtractBtn.setText(f"📤  提取选中 ({n})" if n else "📤  提取选中")
        self._pl_apply_row_style()

    def _pl_apply_row_style(self):
        """勾选的行：整行绿色浅底（含"选择"列）+ 名称加粗，让"已选"一眼可见

        绿色 = 已选（和整行点选时的蓝色高亮条区分开）。
        注意：QSS 的 QTableWidget::item 规则会让 Qt 忽略 BackgroundRole，
        第 0 列由自绘委托补画这一层底色（见 CheckCellDelegate.paint）。
        """
        c = self.theme
        tint = QColor(c["ok"])
        tint.setAlpha(62)                        # 比之前更明显：勾选后是一条完整横条
        on_brush = QBrush(tint)
        off_brush = QBrush(Qt.BrushStyle.NoBrush)
        for r in range(self.plTable.rowCount()):
            chk = self.plTable.item(r, 0)
            on = bool(chk) and chk.checkState() == Qt.CheckState.Checked
            # 第 0 列（自绘勾选框）也要铺底，否则高亮横条会缺一块
            if chk is not None:
                chk.setBackground(on_brush if on else off_brush)
            for col in range(1, self.plTable.columnCount()):
                it = self.plTable.item(r, col)
                if it is None:
                    continue
                it.setBackground(on_brush if on else off_brush)
                f = it.font()
                f.setBold(on)
                it.setFont(f)
                it.setForeground(QColor(c["dim"] if (col == 4 and not on)
                                        else c["text"]))
        try:
            self.plTable.viewport().update()
        except Exception:
            pass

    def _pl_check_all(self, checked):
        self.plTable.blockSignals(True)
        for r in range(self.plTable.rowCount()):
            it = self.plTable.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Checked if checked
                                 else Qt.CheckState.Unchecked)
        self.plTable.blockSignals(False)
        self._pl_update_sel()

    def _pl_invert(self):
        self.plTable.blockSignals(True)
        for r in range(self.plTable.rowCount()):
            it = self.plTable.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Unchecked
                                 if it.checkState() == Qt.CheckState.Checked
                                 else Qt.CheckState.Checked)
        self.plTable.blockSignals(False)
        self._pl_update_sel()

    def _pl_select_full_only(self):
        """只勾选「全量·可提取」的分区（增量分区不勾）"""
        self.plTable.blockSignals(True)
        for r in range(self.plTable.rowCount()):
            it = self.plTable.item(r, 0)
            note = self.plTable.item(r, 4)
            if not it:
                continue
            ok = bool(note) and not note.text().startswith(("增量", "⚠"))
            it.setCheckState(Qt.CheckState.Checked if ok else Qt.CheckState.Unchecked)
        self.plTable.blockSignals(False)
        self._pl_update_sel()
        self._pl_log("已按「仅全量」勾选（增量分区无法提取，已跳过）", "info")

    def _pl_filter(self, text):
        q = (text or "").strip().casefold()
        for r in range(self.plTable.rowCount()):
            nm = self.plTable.item(r, 1)
            hit = (not q) or (q in (nm.text().casefold() if nm else ""))
            self.plTable.setRowHidden(r, not hit)

    def _pl_show_detail(self, item):
        """双击某行 → 输出该分区/条目的详情"""
        r = item.row()
        nm = self.plTable.item(r, 1)
        if not nm:
            return
        name = nm.text()
        if self._pl_archive_mode:
            self._pl_log(f"压缩包条目：{name}", "info")
            return
        parts = self._pl_parser.manifest.partitions if self._pl_parser else []
        part = next((p for p in parts if p.name == name), None)
        if part is None:
            return
        self._pl_log("─" * 50, "plain")
        for line in self._pl_parser.partition_detail_lines(part):
            self._pl_log("   " + line, "plain")

    def _pl_pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.plOutE.setText(d)

    # ================================================================
    # 读取流程
    # ================================================================
    def do_pl_pick(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "选择 payload.bin / OTA 全量包", "",
            "Payload / 压缩包 (*.bin *.payload *.zip *.7z *.img);;所有文件 (*)")
        if not f:
            return
        self.plSrcE.setText(f)
        self._pl_log(f"已选择：{f}", "info")
        QTimer.singleShot(50, self.do_pl_read)

    def do_pl_read(self):
        src = self.plSrcE.text().strip()
        if not src or not Path(src).is_file():
            QMessageBox.warning(self, "未选择文件",
                                "请先选择 payload.bin 或 OTA 全量包（zip/7z）。")
            return
        if self._pl_busy:
            QMessageBox.information(self, "正在处理", "上一个任务还没结束。")
            return
        if self._pl_temp_dir:
            shutil.rmtree(self._pl_temp_dir, ignore_errors=True)
            self._pl_temp_dir = None
        self._pl_src = src
        self._pl_archive_mode = False
        self._pl_parser = None
        self._pl_payload_path = ""
        self._pl_release_src()          # 关掉上一次的 zip 直读句柄
        self._pl_fill_table([])
        self._pl_set_info([])
        self._pl_busy = True
        self._pl_cancel = threading.Event()
        self.plReadBtn.setEnabled(False)
        self.plPickBtn.setEnabled(False)
        self.plStopBtn.setEnabled(True)
        self._pl_set_prog(-1)
        self._pl_set_status("🔍 正在读取 …", self.theme["warn"])
        self._pl_log("═" * 60, "plain")
        self._pl_log(f"📖 读取：{Path(src).name}", "info")
        threading.Thread(target=self._pl_read_worker, args=(src,),
                         daemon=True).start()

    def _pl_read_worker(self, src):
        try:
            suffix = Path(src).suffix.lower()
            payload_path = src

            if suffix in ARCHIVE_SUFFIXES:
                name = self._pl_find_in_archive(src)
                if not name:
                    entries = self._pl_list_archive(src)
                    self._pl_archive_mode = True
                    rows = [(n, sz, "", "压缩包文件") for n, sz in entries]

                    def fill_arch():
                        self._pl_set_info([
                            f"文件：{Path(src).name}",
                            f"大小：{self._pl_hsize(Path(src).stat().st_size)}",
                            "普通压缩包（未找到 payload.bin）",
                            f"条目数：{len(rows)}",
                            "可勾选条目后点「提取选中」解出文件",
                        ])
                        self._pl_fill_table(rows)
                        self._pl_set_status(f"✓ 压缩包共 {len(rows)} 个文件（无 payload.bin）",
                                            self.theme["ok"])
                    self._pl_log("压缩包内没有 payload.bin，已按普通压缩包列出文件", "warn")
                    self.sig.ui.emit(fill_arch)
                    return

                # 能直读就直读（zip 里未压缩的 payload.bin），省掉解压几个 GB
                reader = self._pl_open_zip_payload(src, name)
                if reader is not None:
                    self._pl_src_reader = reader
                    payload_path = reader
                    self._pl_log(
                        f"✓ 直接从压缩包读取 payload.bin"
                        f"（{self._pl_hsize(reader.size)}，无需解压）", "ok")
                else:
                    work = self._pl_make_work()
                    target = work / "payload.bin"
                    self._pl_log(f"从压缩包提取 payload.bin（内部路径：{name}）…",
                                 "info")
                    self._pl_set_status("📦 正在解出 payload.bin …",
                                        self.theme["warn"])
                    self._pl_extract_from_archive(src, name, target)
                    payload_path = str(target)
                    self._pl_temp_dir = str(work)
                    self._pl_log(f"✓ payload.bin 已解出：{target}", "ok")

            self._pl_log(f"解析：{getattr(payload_path, 'name', payload_path)}",
                         "info")
            parser = PayloadParser(payload_path)
            self._pl_payload_path = payload_path
            self._pl_parser = parser
            self._pl_log(parser.describe(), "info")
            rows = []
            broken = []
            for p in parser.manifest.partitions:
                st = parser.partition_stats(p)
                if p.size:
                    size_txt = self._pl_hsize(p.size)
                else:
                    size_txt = "未知"          # 清单里没记录大小（不是 0M）
                rows.append((p.name, size_txt, st["ops"],
                             parser.partition_note(p)))
                if not p.has_ops():
                    broken.append(p.name)
            info = parser.info_lines()

            # 解析不到操作的分区：直接打印原始字段，便于定位厂商变体格式
            if broken:
                self._pl_log(
                    f"⚠ 有 {len(broken)} 个分区解析不到操作记录："
                    + "、".join(broken[:8]), "warn")
                for name in broken[:3]:
                    for line in parser.debug_fields(name):
                        self._pl_log("   " + line, "plain")
            for w in parser.manifest.parse_warnings[:5]:
                self._pl_log(f"⚠ {w}", "warn")

            def fill():
                self._pl_set_info(info)
                self._pl_fill_table(rows)
                if broken:
                    self._pl_set_status(
                        f"⚠ 读取完成：{len(rows)} 个分区，其中 {len(broken)} 个无法提取",
                        self.theme["warn"])
                else:
                    self._pl_set_status(f"✓ 读取成功：共 {len(rows)} 个分区",
                                        self.theme["ok"])
            self.sig.ui.emit(fill)
        except PayloadError as e:
            self._pl_log(f"❌ 读取失败：{e}", "error")
            self.sig.ui.emit(lambda m=str(e): self._pl_set_status(
                f"❌ {m}", self.theme["err"]))
        except ExtractionCancelled:
            self._pl_log("⏹ 已停止", "warn")
        except Exception as e:
            self._pl_log(f"❌ 异常：{e}", "error")
        finally:
            self.sig.ui.emit(self._pl_finish)

    # ================================================================
    # 压缩包 / 工作目录 / 收尾
    # ================================================================
    @staticmethod
    def _pl_hsize(n):
        try:
            n = int(n)
        except Exception:
            return "—"
        if n >= 1024 ** 3:
            return f"{n / 1024**3:.2f} GB"
        if n >= 1024 ** 2:
            return f"{n / 1024**2:.1f} MB"
        if n >= 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n} B"

    def _pl_make_work(self):
        """解出 payload.bin 的工作目录：桌面\\VioletTmp\\Payload_时间戳（与脱机修补一致）"""
        try:
            desktop = Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Desktop"
        except Exception:
            desktop = Path.home()
        work = desktop / "VioletTmp" / f"Payload_{datetime.now():%Y%m%d_%H%M%S}"
        try:
            work.mkdir(parents=True, exist_ok=True)
        except Exception:
            work = Path(OUT_DIR) / "payload_work"
            work.mkdir(parents=True, exist_ok=True)
        self._pl_work = work
        return work

    def _pl_find_in_archive(self, src):
        """在 zip/7z 里查找 payload.bin，返回内部路径或 None"""
        s = Path(src)
        if s.suffix.lower() == ".7z":
            if py7zr is None:
                raise PayloadError("读取 7z 需要安装 py7zr：pip install py7zr")
            with py7zr.SevenZipFile(s, mode="r") as z:
                names = z.getnames()
        else:
            with zipfile.ZipFile(s) as z:
                names = z.namelist()
        for n in names:
            if n.replace("\\", "/").lower().endswith("payload.bin"):
                return n
        return None

    @staticmethod
    def _pl_open_zip_payload(src, entry):
        """zip 内未压缩的 payload.bin → 直接随机读；压缩过或失败 → None（走解压）"""
        try:
            with zipfile.ZipFile(src) as z:
                if z.getinfo(entry).compress_type != zipfile.ZIP_STORED:
                    return None
            return _ZipPayloadSource(src, entry)
        except Exception:
            return None

    def _pl_release_src(self):
        """释放上一次的直读句柄"""
        r = getattr(self, "_pl_src_reader", None)
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        self._pl_src_reader = None

    def _pl_list_archive(self, src):
        s = Path(src)
        if s.suffix.lower() == ".7z":
            if py7zr is None:
                raise PayloadError("读取 7z 需要安装 py7zr：pip install py7zr")
            with py7zr.SevenZipFile(s, mode="r") as z:
                return [(n, "—") for n in z.getnames()]
        out = []
        with zipfile.ZipFile(s) as z:
            for it in z.infolist():
                if it.is_dir():
                    continue
                out.append((it.filename, self._pl_hsize(it.file_size)))
        return out

    def _pl_extract_from_archive(self, src, name, target):
        """把压缩包内的 name 解到 target（zip 流式，7z 解后改名）"""
        s = Path(src)
        target = Path(target)
        if s.suffix.lower() == ".7z":
            with py7zr.SevenZipFile(s, mode="r") as z:
                z.extract(path=str(target.parent), targets=[name])
            got = target.parent / name
            if got.exists() and got.resolve() != target.resolve():
                if target.exists():
                    target.unlink()
                shutil.move(str(got), str(target))
        else:
            with zipfile.ZipFile(s) as z, z.open(name) as fsrc, \
                    open(target, "wb") as fdst:
                while True:
                    if self._pl_cancel.is_set():
                        raise ExtractionCancelled()
                    chunk = fsrc.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    fdst.write(chunk)
        if not target.exists():
            raise PayloadError("payload.bin 提取后未生成")

    def _pl_finish(self):
        self._pl_busy = False
        self.plReadBtn.setEnabled(True)
        self.plPickBtn.setEnabled(True)
        self.plStopBtn.setEnabled(False)
        try:
            self.plProg.reset()               # 先复位
            self.plProg.setFormat("空闲")      # 再显示为空闲态
            self.plStageLb.setText("")
        except Exception:
            pass
        self._pl_update_sel()

    # ================================================================
    # 提取流程
    # ================================================================
    def do_pl_extract(self):
        names = self._pl_checked_names()
        if not names:
            QMessageBox.information(self, "未选择", "请先勾选要提取的项目。")
            return
        if self._pl_busy:
            QMessageBox.information(self, "正在处理", "上一个任务还没结束。")
            return
        outdir = self.plOutE.text().strip()
        if not outdir:
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "输出目录不可用", f"{outdir}\n\n{e}")
            return

        self._pl_busy = True
        self._pl_cancel = threading.Event()
        self.plExtractBtn.setEnabled(False)
        self.plStopBtn.setEnabled(True)
        self.plReadBtn.setEnabled(False)
        self._pl_set_prog(0)
        self._pl_log("═" * 60, "plain")
        self._pl_log(f"📤 开始提取 {len(names)} 项 → {outdir}", "info")
        self._pl_set_status(f"⏳ 正在提取 {len(names)} 项 …", self.theme["warn"])
        threading.Thread(target=self._pl_extract_worker,
                         args=(names, outdir), daemon=True).start()

    def _pl_extract_worker(self, names, outdir):
        try:
            if self._pl_archive_mode:
                self._pl_extract_archive_entries(names, outdir)
            else:
                src_obj = getattr(self, "_pl_src_reader", None) \
                    or self._pl_payload_path
                if isinstance(src_obj, str) and (
                        not src_obj or not Path(src_obj).is_file()):
                    self._pl_log("❌ payload 路径失效，请重新读取", "error")
                    return
                extractor = PayloadExtractor(
                    src_obj, outdir,
                    cancel_event=self._pl_cancel,
                    log_cb=lambda m, lv="plain": self._pl_log(m, lv),
                    prog_cb=lambda v: self.sig.ui.emit(
                        lambda v=v: self._pl_progress_ui(v)),
                    stage_cb=lambda nm: self.sig.ui.emit(
                        lambda nm=nm: self._pl_stage(nm)),
                )
                ok_list, fail_list = extractor.extract(names)
                if fail_list and not ok_list:
                    names_txt = "、".join(n for n, _ in fail_list[:6])
                    self._pl_log(f"❌ 全部失败（{len(fail_list)} 个）：{names_txt}",
                                 "error")
                    reason = fail_list[0][1] if fail_list else ""
                    self.sig.ui.emit(lambda r=reason: self._pl_set_status(
                        f"❌ 提取失败：{r[:70]}", self.theme["err"]))
                elif fail_list:
                    self._pl_log(
                        f"⚠ 完成 {len(ok_list)} 个，失败 {len(fail_list)} 个："
                        + "、".join(n for n, _ in fail_list[:6]), "warn")
                    self._pl_log(f"✅ 已提取的文件在 → {outdir}", "ok")
                    self.sig.ui.emit(lambda: self._pl_set_status(
                        f"⚠ 部分完成：{len(ok_list)} 成功 / {len(fail_list)} 失败",
                        self.theme["warn"]))
                else:
                    self._pl_log(f"✅ 提取完成 → {outdir}", "ok")
                    self.sig.ui.emit(lambda: self._pl_set_status(
                        "✅ 提取完成", self.theme["ok"]))
        except ExtractionCancelled:
            self._pl_log("⏹ 已停止", "warn")
            self.sig.ui.emit(lambda: self._pl_set_status(
                "⏹ 已停止", self.theme["warn"]))
        except PayloadError as e:
            self._pl_log(f"❌ 提取失败：{e}", "error")
            self.sig.ui.emit(lambda m=str(e): self._pl_set_status(
                f"❌ {m}", self.theme["err"]))
        except Exception as e:
            self._pl_log(f"❌ 异常：{e}", "error")
        finally:
            self.sig.ui.emit(self._pl_finish)

    def _pl_extract_archive_entries(self, names, outdir):
        """普通压缩包模式：解出勾选条目（含 zip-slip 校验）"""
        src = Path(self._pl_src)
        root = Path(outdir).resolve()
        total = max(1, len(names))
        for i, name in enumerate(names, 1):
            if self._pl_cancel.is_set():
                raise ExtractionCancelled()
            pct = int(i * 100 / total)
            self.sig.ui.emit(lambda v=pct: self._pl_set_prog(v))
            if src.suffix.lower() == ".7z":
                if py7zr is None:
                    raise PayloadError("读取 7z 需要安装 py7zr：pip install py7zr")
                with py7zr.SevenZipFile(src, mode="r") as z:
                    z.extract(path=str(root), targets=[name])
            else:
                target = (root / name).resolve()
                if root != target and root not in target.parents:
                    raise PayloadError(f"非法压缩包路径：{name}")
                with zipfile.ZipFile(src) as z:
                    z.extract(name, root)
            self._pl_log(f"  ✓ {name}", "plain")
        self._pl_log(f"✅ 已解出 {total} 个条目 → {outdir}", "ok")
        self.sig.ui.emit(lambda: self._pl_set_status("✅ 提取完成", self.theme["ok"]))

    def do_pl_stop(self):
        if not self._pl_busy:
            return
        self._pl_cancel.set()
        self.plStopBtn.setEnabled(False)
        self._pl_log("⏹ 正在停止 …", "warn")
        self._pl_set_status("⏹ 正在停止 …", self.theme["warn"])