# -*- coding: utf-8 -*-
"""PyQt 信号集合"""
from PyQt6.QtCore import QObject, pyqtSignal


class Sig(QObject):
    log = pyqtSignal(str, str)
    dev = pyqtSignal(str, str)
    prog = pyqtSignal(int)          # 修补进度
    info = pyqtSignal(str, str)
    files = pyqtSignal(str, list, bool)
    patch_ok = pyqtSignal()
    patch_fail = pyqtSignal(str)
    prog2 = pyqtSignal(int)         # 文件传输进度（-1 = 不确定）
    flashprog = pyqtSignal(int)     # 刷入进度
    ui = pyqtSignal(object)           # 后台线程投递 UI 回调
    miflash_done = pyqtSignal()        # MiFlash 刷写结束通知
    stats = pyqtSignal(dict)
    recmode = pyqtSignal(str, str, str)   # 设备模式（mode, serial, detail）