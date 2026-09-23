# -*- coding: utf-8 -*-
"""自定义 Card / FileCard 控件"""
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt
from core.config import CARD_W, CARD_H


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")


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