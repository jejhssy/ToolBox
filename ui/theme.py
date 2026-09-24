# -*- coding: utf-8 -*-
"""统一设计系统：全局 QSS + 尺寸令牌

要点
  · 用 $token$ 占位符替换，避免 f-string 里 CSS 花括号要写成双写的坑
  · 覆盖全部用到的控件（表格 / 表头 / 复选框 / 列表 / 输入框 / 滚动条 /
    提示框 / 右键菜单 / 进度条），避免出现"系统默认灰"的控件
  · 颜色全部来自主题字典，深浅主题都能自适应

用法：self.setStyleSheet(build_qss(self.theme))
"""

import colorsys

# ---------------- 尺寸令牌 ----------------
R_CARD = 12          # 卡片圆角
R_BOX = 10           # 输入框 / 表格 / 列表
R_BTN = 9            # 按钮
R_SMALL = 6          # 小块
H_INPUT = 34         # 输入框高度
H_BTN = 36           # 按钮高度
SIDEBAR_W = 212      # 侧边栏宽度
MENU_ITEM_H = 46     # 侧边栏菜单项高度


def _rgb(hexstr: str):
    """#rrggbb → (r, g, b)，非法值返回 None"""
    h = (hexstr or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _rgba(hexstr: str, alpha: int) -> str:
    """#rrggbb + 透明度 → rgba(...) 字符串（QSS 支持）"""
    rgb = _rgb(hexstr)
    if rgb is None:
        return hexstr
    r, g, b = rgb
    return f"rgba({r}, {g}, {b}, {alpha})"


def _deep(hexstr: str, factor: float = 0.72) -> str:
    """加深颜色（勾选态用）：保持色相、略提饱和、压暗亮度"""
    rgb = _rgb(hexstr)
    if rgb is None:
        return hexstr
    r, g, b = (v / 255.0 for v in rgb)
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    sat = min(1.0, sat * 1.08)
    val = max(0.0, val * factor)
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _mix(hex1: str, hex2: str, t: float) -> str:
    """两种颜色线性混合（t=0 → hex1，t=1 → hex2）"""
    a, b = _rgb(hex1), _rgb(hex2)
    if a is None or b is None:
        return hex1 or hex2
    return "#%02x%02x%02x" % tuple(
        round(a[i] + (b[i] - a[i]) * t) for i in range(3))


FONT_STACK = ('"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", '
              '"Noto Sans CJK SC", "Segoe UI", sans-serif')
MONO_STACK = '"Cascadia Mono", Consolas, "JetBrains Mono", monospace'


QSS_TEMPLATE = f"""
/* ============================================================
   基础
   ============================================================ */
* {{
    font-family: {FONT_STACK};
}}
QMainWindow, QDialog, QMessageBox, QFileDialog, QInputDialog {{
    background: $bg$;
    color: $text$;
}}
QWidget {{
    color: $text$;
    font-size: 10pt;
}}
QLabel {{
    background: transparent;
    border: none;
}}
QLabel#pageTitle {{
    font-size: 17pt;
    font-weight: bold;
    color: $text$;
}}
QLabel#cardTitle {{
    font-size: 11.5pt;
    font-weight: bold;
    color: $text$;
}}
QLabel#desc {{
    color: $dim$;
    font-size: 9.5pt;
}}
QLabel#label {{
    color: $text$;
    font-weight: bold;
}}
QLabel#info {{ color: $dim$; }}
QLabel#infoKey {{ color: $dim$; }}
QLabel#infoVal {{ color: $text$; font-weight: bold; }}
QLabel#status {{ color: $text$; font-weight: bold; }}
QLabel#modeChip {{
    color: $text$;
    font-weight: bold;
    background: $panel2$;
    border: 1px solid $line$;
    border-radius: {R_SMALL}px;
    padding: 3px 10px;
}}
QLabel#hint {{ color: $dim$; font-size: 9pt; }}
/* 状态色：页面只打状态标记（state），颜色由主题决定 → 换肤自动跟随。
   带 objectName 前缀是为了压过 QLabel#info 这类 ID 选择器的优先级 */
$state_rules$
QLabel#footer {{
    color: $warn$;
    font-size: 8.5pt;
    padding: 2px 4px 6px 4px;
}}
QFrame#hline {{
    background: $line$;
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame#vline {{
    background: $line$;
    border: none;
    max-width: 1px;
    min-width: 1px;
}}

/* ============================================================
   卡片 / 分区
   ============================================================ */
QFrame#card {{
    background: $panel$;
    border: 1px solid $line$;
    border-radius: {R_CARD}px;
}}
QFrame#cardFlat {{
    background: $panel$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
}}
QFrame#topBar {{
    background: $panel$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
}}
QFrame#sep {{
    background: $line$;
    border: none;
    max-height: 1px;
}}

/* ============================================================
   侧边栏
   ============================================================ */
QFrame#sidebar {{
    background: $menu$;
    border: none;
    border-right: 1px solid $line$;
}}
QFrame#sideTop {{
    background: $menu$;
    border: none;
    border-bottom: 1px solid $line$;
}}
QLabel#sideTitle {{
    font-size: 15pt;
    font-weight: bold;
    color: $text$;
    background: transparent;
}}
QLabel#sideSub {{
    font-size: 8pt;
    color: $dim$;
    background: transparent;
}}
QLabel#sideVer {{
    color: $dim$;
    font-size: 8pt;
    background: transparent;
}}
QFrame#sideBottom {{
    background: $menu$;
    border: none;
    border-top: 1px solid $line$;
}}
QListWidget#sideMenu {{
    background: $menu$;
    border: none;
    outline: 0;
    padding: 8px 0;
    color: $menuTxt$;
    font-size: 10.5pt;
}}
QListWidget#sideMenu::item {{
    padding: 10px 14px;
    margin: 3px 10px;
    border-radius: {R_BTN}px;
    color: $menuTxt$;
}}
QListWidget#sideMenu::item:hover {{
    background: $panel$;
    color: $text$;
}}
QListWidget#sideMenu::item:selected {{
    background: $menuSel$;
    color: $white$;
    font-weight: bold;
}}

/* ============================================================
   列表（通用）
   ============================================================ */
QListWidget {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
    outline: 0;
    padding: 4px;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: {R_SMALL}px;
    color: $text$;
}}
QListWidget::item:hover {{ background: $hov$; }}
QListWidget::item:selected {{
    background: $acc$;
    color: $white$;
    font-weight: bold;
}}

/* ============================================================
   表格
   ============================================================ */
QTableWidget, QTableView {{
    background: $log$;
    alternate-background-color: $hov2$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
    gridline-color: $line$;
    color: $text$;
    outline: 0;
    selection-background-color: $acc$;
    selection-color: $white$;
}}
QTableWidget::item, QTableView::item {{
    padding: 6px 8px;
    border: none;
}}
QTableWidget::item:hover, QTableView::item:hover {{ background: $hov$; }}
QTableWidget::item:selected, QTableView::item:selected {{
    background: $acc$;
    color: $white$;
}}
QHeaderView {{
    background: transparent;
    border: none;
}}
QHeaderView::section {{
    background: $panel2$;
    color: $text$;
    font-weight: bold;
    padding: 8px 10px;
    border: none;
    border-right: 1px solid $line$;
    border-bottom: 1px solid $line$;
}}
QHeaderView::section:hover {{ background: $line$; }}
QHeaderView::section:first {{ border-top-left-radius: {R_BOX}px; }}
QHeaderView::section:last {{
    border-top-right-radius: {R_BOX}px;
    border-right: none;
}}
QTableCornerButton::section {{
    background: $panel2$;
    border: none;
    border-bottom: 1px solid $line$;
}}

/* ============================================================
   复选框
   ============================================================ */
QCheckBox {{
    color: $text$;
    spacing: 8px;
    padding: 3px 0;
    background: transparent;
}}
QCheckBox:hover {{ color: $acc2$; }}
QCheckBox:disabled {{ color: $dim$; }}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid $edge$;
    background: transparent;
}}
QCheckBox::indicator:hover {{ border: 1px solid $ok$; }}
QCheckBox::indicator:checked {{
    /* 勾选 = 实心绿（绿色=已选/完成，比亮蓝更醒目，也不会和蓝色高亮条糊在一起） */
    background: $okDeep$;
    border: 1px solid $ok$;
}}
QCheckBox::indicator:checked:hover {{ background: $ok$; border: 1px solid $ok$; }}
QCheckBox::indicator:disabled {{
    background: transparent;
    border: 1px dashed $dim$;
}}
QCheckBox::indicator:checked:disabled {{
    /* 禁用时也要看得出"已勾选"，只是变淡（MiFlash 选项选脚本前就是禁用态） */
    background: $okFade$;
    border: 1px solid $okMute$;
}}

/* ============================================================
   输入 / 下拉 / 数字
   ============================================================ */
QLineEdit, QPlainTextEdit {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 7px 12px;
    color: $text$;
    min-height: {H_INPUT - 16}px;
    selection-background-color: $acc$;
    selection-color: $white$;
}}
QLineEdit:hover {{ border: 1px solid $acc2$; }}
QLineEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid $acc$;
    background: $panel$;
}}
QLineEdit:read-only {{ color: $dim$; }}
QLineEdit:disabled {{ background: $panel2$; color: $dim$; }}
QComboBox {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 6px 30px 6px 12px;
    color: $text$;
    min-height: {H_INPUT - 16}px;
    selection-background-color: $acc$;
    selection-color: $white$;
}}
QComboBox:hover, QComboBox:focus, QComboBox:on {{
    border: 1px solid $acc$;
    background: $panel$;
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid $acc2$;
    margin-right: 8px;
}}
QComboBox::down-arrow:on {{ border-top: 6px solid $acc$; }}
QComboBox QAbstractItemView {{
    background: $panel$;
    color: $text$;
    border: 1px solid $acc$;
    border-radius: {R_SMALL}px;
    padding: 6px;
    outline: 0;
    selection-background-color: $acc$;
    selection-color: $white$;
}}
QComboBox QAbstractItemView::item {{
    min-height: 30px;
    padding: 4px 12px;
    border-radius: {R_SMALL}px;
    color: $text$;
}}
QComboBox QAbstractItemView::item:hover {{ background: $hov$; }}
QComboBox QAbstractItemView::item:selected {{
    background: $acc$;
    color: $white$;
}}
QSpinBox {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 6px 10px;
    color: $text$;
    min-height: {H_INPUT - 16}px;
    selection-background-color: $acc$;
}}
QSpinBox:hover, QSpinBox:focus {{ border: 1px solid $acc$; }}

/* ============================================================
   按钮
   ============================================================ */
QPushButton {{
    background: $panel2$;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 8px 18px;
    min-height: {H_BTN - 18}px;
    font-weight: bold;
}}
QPushButton:hover {{ background: $line$; border: 1px solid $acc2$; }}
QPushButton:pressed {{ background: $acc$; color: $white$; border: 1px solid $acc$; }}
QPushButton:disabled {{
    background: $panel2$;
    color: $dim$;
    border: 1px solid $line$;
}}
QPushButton#primary {{
    background: $acc$;
    color: $white$;
    border: 1px solid $acc$;
}}
QPushButton#primary:hover {{ background: $acc2$; border: 1px solid $acc2$; }}
QPushButton#primary:pressed {{ background: $menuSel$; border: 1px solid $menuSel$; }}
QPushButton#primary:disabled {{
    background: $panel2$;
    color: $dim$;
    border: 1px solid $line$;
}}
QPushButton#secondary {{
    background: $panel2$;
    color: $text$;
    border: 1px solid $line$;
}}
QPushButton#secondary:hover {{ background: $line$; border: 1px solid $acc$; }}
/* 窄栏里成对出现的小按钮：减小左右内边距，文字不被省略号截断 */
QPushButton#compact {{
    background: $panel2$;
    color: $text$;
    border: 1px solid $line$;
    padding: 8px 8px;
}}
QPushButton#compact:hover {{ background: $line$; border: 1px solid $acc$; }}
QPushButton#ghost {{
    background: transparent;
    color: $text$;
    border: 1px solid $line$;
}}
QPushButton#ghost:hover {{ background: $panel2$; border: 1px solid $acc$; color: $acc2$; }}
QPushButton#ghost:disabled {{ color: $dim$; border: 1px solid $line$; }}
QPushButton#smallGhost {{
    background: transparent;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_SMALL}px;
    padding: 2px 12px;
    min-height: 20px;
    font-size: 9.5pt;
}}
QPushButton#smallGhost:hover {{
    background: $panel2$;
    border: 1px solid $acc$;
    color: $acc2$;
}}
QPushButton#danger {{
    background: transparent;
    color: $err$;
    border: 1px solid $err$;
}}
QPushButton#danger:hover {{ background: $err$; color: $white$; }}
QPushButton#ghostSmall {{
    background: transparent;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_SMALL}px;
    padding: 4px 10px;
    font-size: 9.5pt;
    font-weight: normal;
}}
QPushButton#ghostSmall:hover {{
    background: $panel2$;
    border: 1px solid $acc$;
    color: $acc2$;
}}
QPushButton#sideBtn, QPushButton#sideBtn2 {{
    background: $panel2$;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    font-weight: bold;
}}
QPushButton#sideBtn:hover {{ background: $acc$; color: $white$; border: 1px solid $acc$; }}
QPushButton#sideBtn2:hover {{ background: $acc2$; color: $white$; border: 1px solid $acc2$; }}

/* ============================================================
   文本区 / 日志
   ============================================================ */
QTextEdit {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 8px;
    color: $text$;
    selection-background-color: $acc$;
    selection-color: $white$;
}}
QTextEdit#log, QTextEdit#rootOutput {{
    background: $log$;
    font-family: {MONO_STACK};
    font-size: 9pt;
}}
QTextEdit#rootOutput {{ color: $text$; }}

/* ============================================================
   进度条
   ============================================================ */
QProgressBar {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: 7px;
    color: $text$;
    text-align: center;
    font-size: 9pt;
    font-weight: bold;
    min-height: 14px;
}}
QProgressBar::chunk {{
    background: $acc$;
    border-radius: 6px;
}}
QProgressBar#batteryBar {{
    min-height: 30px;
    font-size: 14pt;
}}
QProgressBar#batteryBar::chunk {{ background: $ok$; }}
QProgressBar#flashProg::chunk {{ background: $ok$; }}

/* ============================================================
   滚动区 / 滚动条
   ============================================================ */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea#pageScroll {{ background: transparent; border: none; }}
QScrollArea#pageScroll > QWidget > QWidget {{ background: transparent; }}
QScrollArea#fileScroll {{
    background: $log$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
}}
QWidget#fileGrid {{ background: $log$; }}
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px 2px 2px 0;
}}
QScrollBar::handle:vertical {{
    background: $line$;
    min-height: 40px;
    border: 2px solid transparent;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: $acc$; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background: $line$;
    min-width: 40px;
    border: 2px solid transparent;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{ background: $acc$; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
    background: transparent;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ============================================================
   提示 / 菜单 / 对话框 / 其它
   ============================================================ */
QToolTip {{
    background: $panel$;
    color: $text$;
    border: 1px solid $acc$;
    border-radius: {R_SMALL}px;
    padding: 6px 9px;
    font-size: 9pt;
}}
QMenu {{
    background: $panel$;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 24px 7px 14px;
    border-radius: {R_SMALL}px;
    color: $text$;
}}
QMenu::item:selected {{ background: $acc$; color: $white$; }}
QMenu::item:disabled {{ color: $dim$; }}
QMenu::separator {{ height: 1px; background: $line$; margin: 6px 10px; }}
QMessageBox {{ background: $bg$; }}
QMessageBox QLabel {{ color: $text$; font-size: 10pt; }}
QMessageBox QPushButton {{
    min-width: 78px;
    background: $panel2$;
    color: $text$;
    border: 1px solid $line$;
    border-radius: {R_BTN}px;
    padding: 7px 16px;
    font-weight: bold;
}}
QMessageBox QPushButton:hover {{
    background: $acc$;
    color: $white$;
    border: 1px solid $acc$;
}}
QSplitter::handle {{ background: $line$; }}
QSplitter::handle:hover {{ background: $acc$; }}
QAbstractScrollArea::corner {{ background: transparent; }}
QRadioButton {{ color: $text$; spacing: 8px; background: transparent; }}
QGroupBox {{
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    color: $text$;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: $dim$;
}}

/* ============================================================
   文件卡片
   ============================================================ */
QFrame#fileCard {{
    background: $panel$;
    border: 1px solid $line$;
    border-radius: {R_BOX}px;
}}
QFrame#fileCard:hover {{
    background: $panel2$;
    border: 1px solid $acc2$;
}}
QFrame#fileCard[selected="true"] {{
    background: $hov$;
    border: 2px solid $acc$;
}}
QLabel#fileIcon {{ font-size: 20pt; background: transparent; }}
QLabel#fileName {{ color: $text$; font-size: 9pt; background: transparent; }}
QLabel#fileSize {{ color: $dim$; font-size: 8pt; background: transparent; }}
"""


# 未做替换的占位符（自查用）
def missing_tokens(qss: str):
    import re
    return sorted(set(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*\$", qss)))


def build_qss(theme: dict) -> str:
    """主题字典 → 完整 QSS（$token$ 全部替换成主题/派生色）"""
    t = dict(theme)
    t["white"] = "#ffffff"
    acc = theme.get("acc", "#4f8cff")
    t["hov"] = _rgba(acc, 38)      # 悬停：极淡的强调色
    t["hov2"] = _rgba(acc, 16)     # 斑马纹：更淡
    t["accDeep"] = _deep(acc, 0.72)                       # 勾选/强调：加深的强调色
    t["edge"] = _mix(theme.get("dim", acc), theme.get("acc2", acc), 0.38)  # 勾选框边框
    okc = theme.get("ok", "#4ade80")
    t["okDeep"] = _deep(okc, 0.78)                        # 勾选态：实心绿（加深）
    t["okFade"] = _rgba(t["okDeep"], 150)                 # 勾选但禁用：淡绿
    t["okMute"] = _rgba(okc, 170)                         # 勾选但禁用：描边
    t["state_rules"] = _state_rules(theme)
    q = QSS_TEMPLATE
    for k, v in t.items():
        q = q.replace("$" + k + "$", str(v))
    return q


# 常见带状态标记的 QLabel 的 objectName（带前缀的选择器优先级更高）
_STATE_NAMES = ("info", "status", "devStatus", "modeChip", "hint", "desc",
                "infoVal", "infoKey", "label", "cardTitle", "pageTitle")
_STATE_COLORS = (("ok", "ok", ""), ("warn", "warn", ""), ("err", "err", ""),
                 ("dim", "dim", ""), ("text", "text", ""),
                 ("acc", "acc2", ""), ("bold", "text",
                                       "; font-weight: bold"))


def _state_rules(theme: dict) -> str:
    """生成 QLabel[state=...] 规则（把颜色交给主题，换肤自动跟随）"""
    out = []
    for state, key, extra in _STATE_COLORS:
        sels = ['QLabel[state="%s"]' % state]
        sels += ['QLabel#%s[state="%s"]' % (n, state) for n in _STATE_NAMES]
        out.append(", ".join(sels)
                   + " { color: %s%s; }" % (theme.get(key, "#ffffff"), extra))
    return "\n".join(out)
