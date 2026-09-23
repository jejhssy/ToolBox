# -*- coding: utf-8 -*-
"""一键工具箱 —— 启动入口
运行：python main.py
"""
import os, sys
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWin


def main():
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))
    app = QApplication(sys.argv)
    w = MainWin()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()