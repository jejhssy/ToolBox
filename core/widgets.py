# -*- coding: utf-8 -*-
"""自定义控件：Card / FileCard / 表格勾选框委托 / 动画进度条"""
from PyQt6.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QProgressBar, QStyledItemDelegate,
    QStyleOptionViewItem, QStyle, QApplication,
)
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, QEvent,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QLinearGradient, QBrush,
    QFontMetrics, QPalette,
)
from core.config import CARD_W, CARD_H


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)


def set_state(widget, state):
    """给控件打状态标记（ok/warn/err/dim/text/acc）

    颜色由 ui/theme.py 里的 QLabel[state=...] 规则决定，
    这样换主题时颜色会自动跟着变（内联 setStyleSheet 做不到）。
    """
    if widget is None:
        return
    try:
        if widget.property("state") == state:
            return
        widget.setProperty("state", state)
        st = widget.style()
        st.unpolish(widget)
        st.polish(widget)
        widget.update()
    except Exception:
        pass


def state_from_color(theme, color):
    """把主题里的颜色值反查成状态名（兼容旧的 _xxx(text, color) 调用方式）"""
    if not color:
        return "dim"
    low = str(color).strip().lower()
    for state, key in (("ok", "ok"), ("warn", "warn"), ("err", "err"),
                       ("text", "text"), ("acc", "acc2"), ("dim", "dim")):
        try:
            if str(theme.get(key, "")).lower() == low:
                return state
        except Exception:
            continue
    return "dim"


class FileCard(QFrame):
    def __init__(self, name, is_dir, icon, color, info, on_click, on_double, parent=None):
        super().__init__(parent)
        self.setObjectName("fileCard")
        self.setFixedSize(CARD_W, CARD_H)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy().Fixed,
            self.sizePolicy().verticalPolicy().Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", "false")
        self._name = name
        self._is_dir = is_dir

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 12, 6, 8)
        v.setSpacing(6)

        ico = QLabel(icon)
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico.setObjectName("fileIcon")
        ico.setStyleSheet(f"color: {color}; background: transparent;")
        v.addWidget(ico)

        nm = QLabel()
        nm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nm.setWordWrap(False)
        nm.setObjectName("fileName")
        fm = nm.fontMetrics()
        elided = fm.elidedText(name, Qt.TextElideMode.ElideMiddle, CARD_W - 14)
        nm.setText(elided)
        nm.setToolTip(name)
        v.addWidget(nm)

        if info:
            sz = QLabel(info)
            sz.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sz.setObjectName("fileSize")
            v.addWidget(sz)

        self._on_click_cb = on_click
        self._on_double_cb = on_double

    def set_selected(self, sel: bool):
        val = "true" if sel else "false"
        if self.property("selected") == val:
            return
        self.setProperty("selected", val)
        st = self.style()
        st.unpolish(self)
        st.polish(self)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click_cb(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_double_cb(self)
        super().mouseDoubleClickEvent(event)


def _blend(c1, c2, t):
    """两种颜色线性混合（t=0 → c1，t=1 → c2）"""
    a, b = QColor(c1), QColor(c2)
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _deepen(color, factor=0.72):
    """把颜色加深（保色相、略提饱和），"已勾选"看起来更实、更醒目"""
    c = QColor(color)
    h, s, v, a = c.getHsv()
    if h < 0:                              # 灰色系：直接压暗亮度
        return QColor(int(c.red() * factor), int(c.green() * factor),
                      int(c.blue() * factor), a)
    return QColor.fromHsv(h, min(255, int(s * 1.08)),
                          max(0, int(v * factor)), a)


class CheckCellDelegate(QStyledItemDelegate):
    """表格"选择"列：自绘的大号圆角勾选框

    Qt 默认的勾选标记很小、深色主题下几乎看不见，这里自己画：
      · 未勾选：空心描边方框（边框加深，浅色主题也看得清）
      · 勾选：实心绿色方块 + 描边（绿色=已选，和整行选中的蓝底区分得开）
      · 悬停：描边变亮 + 淡光圈
    同时负责把整行底色（勾选行的浅色底 / 选中高亮）铺满这一列，
    否则高亮大横条会在这里断掉、看起来被切掉一块。
    """

    def __init__(self, parent=None, accent="#4f8cff", line="#2a3555",
                 track="#0a0f1e", accent2=None, dim=None, ok=None):
        super().__init__(parent)
        self._size = 22
        self.set_colors(accent, line, track, accent2, dim, ok)

    def set_colors(self, accent, line, track, accent2=None, dim=None, ok=None):
        self._acc = QColor(accent)
        self._acc2 = QColor(accent2 or accent)
        self._line = QColor(line)
        self._track = QColor(track)
        self._line_dim = QColor(dim or line)
        self._ok = QColor(ok or accent)          # 勾选色：主题绿
        self._ok_deep = _deepen(self._ok, 0.78)  # 加深的绿（用户反馈浅色"太浅"）
        self._edge = _blend(self._line_dim, self._acc2, 0.38)  # 未勾选边框色

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                     # 不画文字，选框自己画
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        # 背景/选中/悬停底色用**原始 option** 画（带着 Selected、Alternate、
        # MouseOver 状态），这样第 0 列也会被整行高亮条盖住；
        # PE_PanelItemViewItem 只画底，不会额外画 Qt 默认的小勾选框。
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                            option, painter, widget)
        # 勾选行的浅色底：QSS 的 ::item 规则会忽略 BackgroundRole，
        # 这里自己补一层，保证"选择"列和其它列连成一条完整的横条。
        if not selected:
            bg = index.data(Qt.ItemDataRole.BackgroundRole)
            if isinstance(bg, QBrush) and bg.style() != Qt.BrushStyle.NoBrush:
                painter.fillRect(option.rect, bg)
        checked = (index.data(Qt.ItemDataRole.CheckStateRole)
                   == Qt.CheckState.Checked.value)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        # 整行被选中时底色就是强调色，勾选框要用"对比色"描边才看得清
        on_bar = selected
        white = QColor(255, 255, 255)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(self._size)
        box = QRectF(option.rect.center().x() - s / 2.0,
                     option.rect.center().y() - s / 2.0, s, s)
        if hover and not checked:
            ring = QColor(self._acc)
            ring.setAlpha(60)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ring)
            painter.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 9, 9)

        if checked:
            # 已勾选：实心绿色方块（绿色=已选）+ 描边
            painter.setBrush(self._ok_deep)
            if on_bar:                                 # 在强调色横条上 → 白描边更清楚
                border = QColor(white)
                border.setAlpha(190)
            else:
                border = QColor(self._ok)
            painter.setPen(QPen(border, 1.6))
            painter.drawRoundedRect(box.adjusted(0.8, 0.8, -0.8, -0.8), 6.5, 6.5)
        else:
            # 未勾选：空心描边方框（边框加深，避免浅色主题下"看不见框"）
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if on_bar:
                edge = QColor(white)
                edge.setAlpha(150)
            else:
                edge = QColor(self._acc2) if hover else QColor(self._edge)
            painter.setPen(QPen(edge, 1.8))
            painter.drawRoundedRect(box.adjusted(0.9, 0.9, -0.9, -0.9), 6.5, 6.5)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        """点整个单元格都算勾选（默认只有那个小指示器区域有效，很难点中）"""
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            cur = index.data(Qt.ItemDataRole.CheckStateRole)
            new = (Qt.CheckState.Unchecked.value
                   if cur == Qt.CheckState.Checked.value
                   else Qt.CheckState.Checked.value)
            model.setData(index, new, Qt.ItemDataRole.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        sz.setWidth(max(sz.width(), 48))
        sz.setHeight(max(sz.height(), 34))
        return sz


class RowTintDelegate(QStyledItemDelegate):
    """普通单元格委托：让"整行底色"真的画得出来

    ui/theme.py 给 QTableWidget::item 设了 padding / border 后，Qt 就改用
    QStyleSheetStyle 自绘单元格，模型里的 BackgroundRole 会被直接忽略，
    于是勾选行的整行浅色底怎么都显示不出来（只有被自绘委托接管的第 0 列能看到）。
    这里在画完单元格底之后、画文字之前补一层底色，整行才是一条完整横条。
    """

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        state = option.state                 # initStyleOption 会清掉 view 给的状态
        opt.state = state                    # （选中/斑马纹/悬停都要保留）
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                            opt, painter, widget)
        if not (state & QStyle.StateFlag.State_Selected):
            bg = index.data(Qt.ItemDataRole.BackgroundRole)
            if isinstance(bg, QBrush) and bg.style() != Qt.BrushStyle.NoBrush:
                painter.fillRect(opt.rect, bg)
        if text:
            fm = QFontMetrics(opt.font)
            rect = opt.rect.adjusted(8, 6, -8, -6)     # 对齐 QSS 的 padding: 6px 8px
            align = int(opt.displayAlignment) or int(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            painter.save()
            painter.setFont(opt.font)
            painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
            painter.drawText(rect, align,
                             fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                           rect.width()))
            painter.restore()


class AnimatedProgressBar(QProgressBar):
    """带动画的进度条

    · set_busy(True, "读取中…")：进度未知时来回滚动的高亮块
    · set_smooth_value(37)：数值平滑过渡，而不是硬跳
    · set_colors(...)：跟随主题换色
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 先初始化内部状态，最后再调 setRange/setValue（它们会用到 _timer/_busy）
        self._busy = False
        self._busy_text = "处理中…"
        self._phase = 0.0
        self._fill = QColor("#4f8cff")
        self._fill2 = QColor("#6ea8ff")
        self._track = QColor("#0a0f1e")
        self._line = QColor("#2a3555")
        self._text = QColor("#e8ecf7")
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._anim = QPropertyAnimation(self, b"value", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        super().setRange(0, 100)
        super().setValue(0)
        self.setTextVisible(True)

    # ---------------- 主题 ----------------
    def set_colors(self, fill, track, text, line=None, fill2=None):
        self._fill = QColor(fill)
        self._fill2 = QColor(fill2 or fill)
        self._track = QColor(track)
        self._text = QColor(text)
        self._line = QColor(line or track)
        self.update()

    # ---------------- 兼容 QProgressBar 旧用法 ----------------
    def setRange(self, mn, mx):
        """setRange(0, 0) 是 Qt 里"进度未知"的写法 → 转成滚动动画"""
        try:
            if int(mn) == 0 and int(mx) == 0:
                self.set_busy(True, self._busy_text)
                return
        except Exception:
            pass
        self.set_busy(False)
        super().setRange(mn, mx)

    # ---------------- 状态 ----------------
    def set_busy(self, busy, text="处理中…"):
        self._busy_text = text
        busy = bool(busy)
        if busy:
            try:
                self.setFormat(text)     # 与绘制文字保持一致
            except Exception:
                pass
            if not self._timer.isActive():
                self._phase = 0.0
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()
        changed = busy != self._busy
        self._busy = busy
        if changed:
            self.update()

    def is_busy(self):
        return self._busy

    def set_smooth_value(self, val, text=None):
        if text is not None:
            self.setFormat(text)
        if self._busy:
            self.set_busy(False)
        v = max(self.minimum(), min(self.maximum(), int(val)))
        cur = self.value()
        if abs(v - cur) <= 1:
            self.setValue(v)
            return
        self._anim.stop()
        self._anim.setStartValue(cur)
        self._anim.setEndValue(v)
        self._anim.start()

    def reset(self):
        self._anim.stop()
        self.set_busy(False)
        self.setValue(0)
        self.setFormat("%p%")

    def _tick(self):
        self._phase = (self._phase + 0.032) % 1.0
        self.update()

    # ---------------- 自绘 ----------------
    def _fmt(self):
        """替换 %p / %v / %m，避免把 "%p%" 原样画出来"""
        t = self.format() or ""
        return (t.replace("%p", str(self.value()))
                 .replace("%v", str(self.value()))
                 .replace("%m", str(self.maximum())))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = max(1.0, r.height() / 2.0)
        p.setPen(QPen(self._line, 1.0))
        p.setBrush(self._track)
        p.drawRoundedRect(r, rad, rad)

        inner = r.adjusted(1.5, 1.5, -1.5, -1.5)
        if inner.width() > 4 and inner.height() > 2:
            clip = QPainterPath()
            clip.addRoundedRect(inner, rad, rad)
            p.save()
            p.setClipPath(clip)
            p.setPen(Qt.PenStyle.NoPen)
            if self._busy:
                w = max(inner.width() * 0.30, 40.0)
                span = inner.width() + w
                x = inner.left() - w + span * self._phase
                g = QLinearGradient(x, 0, x + w, 0)
                c0 = QColor(self._fill)
                c0.setAlpha(0)
                c1 = QColor(self._fill2)
                c1.setAlpha(0)
                g.setColorAt(0.0, c0)
                g.setColorAt(0.5, self._fill2)
                g.setColorAt(1.0, c1)
                p.setBrush(g)
                p.drawRect(QRectF(x, inner.top(), w, inner.height()))
            else:
                rng = self.maximum() - self.minimum()
                frac = 0.0 if rng <= 0 else (self.value() - self.minimum()) / rng
                cw = inner.width() * frac
                if cw > 0:
                    w = max(cw, inner.height())
                    g = QLinearGradient(inner.left(), 0, inner.left() + w, 0)
                    g.setColorAt(0.0, self._fill)
                    g.setColorAt(1.0, self._fill2)
                    p.setBrush(g)
                    p.drawRoundedRect(
                        QRectF(inner.left(), inner.top(), w, inner.height()),
                        rad, rad)
            p.restore()

        txt = self._busy_text if self._busy else self._fmt()
        if self.isTextVisible() and txt:
            f = self.font()
            f.setPointSizeF(max(7.5, f.pointSizeF() - 1.0))
            p.setFont(f)
            p.setPen(self._text)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, txt)
        p.end()