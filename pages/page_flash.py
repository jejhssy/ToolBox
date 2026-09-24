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
from core.widgets import AnimatedProgressBar, set_state
from core.utils import sh, sh_stream, adb_dev, fb_dev, adb_shell_dev


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


# ==================== 扩展功能操作表（常规刷入页） ====================
# 每个操作两种执行方式：
#   · su_steps  : 设备在 ADB 下时用，adb shell su -c "<命令>"（执行前会先校验/获取 root）
#   · fastboot  : 设备在 Fastboot 下时的等价命令（None = 该操作只能走 ADB+root）
MAINT_OPS = {
    "wipe": {
        "title": "清除数据",
        "desc": "删除 /data 下应用与系统数据（相当于恢复出厂）；默认保留内部存储",
        "confirm": "即将清除手机数据（相当于恢复出厂设置）：\n"
                   "· 应用、账号、锁屏密码、系统设置全部丢失\n"
                   "· 操作无法撤销\n",
        "su_steps": [
            ("清除应用与系统数据",
             "find /data -mindepth 1 -maxdepth 1 ! -name media -exec rm -rf {} +"),
        ],
        "su_steps_full": [
            ("清除 /data 全部内容（含内部存储）",
             "find /data -mindepth 1 -exec rm -rf {} +"),
        ],
        "fastboot": ["-w"],
        "fastboot_note": "fastboot -w = 擦除 userdata / metadata / cache"
                         "（内部存储也会一起清空）",
        "reboot": True,
    },
    "frp": {
        "title": "清除 FRP",
        "desc": "清 Google 账号锁：删账号库 + frp 标记，并复位开机向导",
        "confirm": "即将清除 FRP（Google 账号锁 / 恢复出厂保护）：\n"
                   "· Fastboot 模式：擦除 frp 分区（MTK 机型有效）\n"
                   "· ADB + root 模式：删除账号库、frp 标记并复位开机向导\n\n"
                   "部分高通新平台需要配合「清除数据」才彻底。\n",
        "su_steps": [
            ("删除账号库与 frp 标记",
             "rm -rf /data/system/users/0/frp "
             "/data/system/users/0/accounts_ce.db* "
             "/data/system/users/0/accounts_de.db*"),
            ("复位开机向导标记", "settings put secure user_setup_complete 0"),
        ],
        "fastboot": ["erase", "frp"],
        "fastboot_note": "fastboot erase frp",
        "reboot": True,
    },
    "miui_sec": {
        "title": "强开小米安全设置",
        "desc": "绕过小米账号校验，直接打开「USB 调试（安全设置）」",
        "confirm": "即将强制打开小米的 USB 调试相关开关（需 root）：\n"
                   "· 打开开发者选项 / USB 调试\n"
                   "· 写入「USB 调试（安全设置）」开关 persist.security.adbinput=1\n\n"
                   "设置里那个灰色开关重启设置应用后仍会是灰色，但功能已实际生效。\n",
        "su_steps": [
            ("打开开发者选项", "settings put global development_settings_enabled 1"),
            ("打开 USB 调试（Global）", "settings put global adb_enabled 1"),
            ("打开 USB 调试（Secure）", "settings put secure adb_enabled 1"),
            ("写入安全设置开关", "setprop persist.security.adbinput 1"),
            ("回读安全设置开关", "getprop persist.security.adbinput"),
        ],
        "fastboot": None,
        "reboot": False,
    },
    "diag": {
        "title": "打开基带端口",
        "desc": "用 root 打开基带（诊断 DIAG）端口，让基带工具 / AT 串口能连上手机",
        "confirm": "即将用 root 打开基带端口（诊断 DIAG / AT 串口，需 root）：\n"
                   "· setprop sys.usb.config diag,adb（切到诊断模式，adb 会断开重连）\n"
                   "· 持久化 persist.sys.usb.config diag,adb\n"
                   "· 本工具会自动扫一遍电脑上有没有出现诊断端口\n\n"
                   "★ 部分机型执行完不会马上出现端口，需要重启手机"
                   "（或拔插一次数据线）后才生效。\n"
                   "想恢复普通 USB：用下面的「关闭基带端口」。\n",
        # 切换 USB 配置时 adb 会掉线，这几步失败不影响"端口已打开"，
        # 所以标成 soft（只警告不中断），最后以电脑上是否扫到端口为准
        "su_steps": [
            ("打开开发者选项", "settings put global development_settings_enabled 1"),
            ("打开基带端口（DIAG + adb）", "setprop sys.usb.config diag,adb", "soft"),
            ("持久化 USB 配置", "setprop persist.sys.usb.config diag,adb", "soft"),
            ("回读 USB 配置", "getprop sys.usb.config", "soft"),
            ("列出基带相关分区", "ls /dev/block/by-name", "soft"),
        ],
        "verify": "baseband_port",
        "fastboot": None,
        "reboot": False,
    },
    "close_diag": {
        "title": "关闭基带端口",
        "desc": "把 USB 配置改回 MTP + ADB，关掉之前的诊断（DIAG）端口",
        "confirm": "即将关闭基带端口、恢复普通 USB（MTP + ADB，需 root）：\n"
                   "· persist.sys.usb.config 改回 mtp,adb（否则重启后又会回到诊断模式）\n"
                   "· 断开当前配置再切回 mtp,adb\n"
                   "· 扫一遍电脑上的端口，确认诊断端口已经消失\n\n"
                   "★ 部分机型要重启手机端口才会消失。\n",
        "su_steps": [
            ("恢复默认 USB 配置", "setprop persist.sys.usb.config mtp,adb", "soft"),
            ("用 USB 服务切回 MTP", "svc usb setFunctions mtp", "soft"),
            ("断开当前 USB 配置", "setprop sys.usb.config none", "soft"),
            ("恢复普通模式（MTP + ADB）",
             "setprop sys.usb.config mtp,adb", "soft"),
            ("回读 USB 配置", "getprop sys.usb.config", "soft"),
        ],
        "verify": "port_closed",
        "fastboot": None,
        "reboot": False,
    },
}


# Windows 设备名里出现这些 → 基本可以确定是基带 / 诊断（DIAG）口
DIAG_PORT_KEYS = ("diagnostics", "hs-usb", "qualcomm", "qdloader", "9008",
                  "mtk", "preloader", "vcom", "diagnostic", "modem",
                  "baseband", "diag", "诊断", "调制解调器")
# 出现这些 → 至少是多了一个串口（很多机型就显示成通用的"USB 串行设备"）
SERIAL_PORT_KEYS = ("serial", "串行", "com")

# 手机 / Android 相关设备的 PnP 名字特征（排查驱动用）
PHONE_PNP_KEYS = ("android", "adb", "qualcomm", "qcom", "mtk", "mediatek",
                  "preloader", "vcom", "hs-usb", "google", "xiaomi", "redmi",
                  "samsung", "huawei", "oppo", "vivo", "oneplus", "mobile",
                  "modem", "9008", "diag", "安卓", "小米", "手机")

# Windows 设备管理器错误码 → 人话（0 = 正常）
DRIVER_ERR_HINTS = {
    1: "设备配置不正确（重插数据线 / 重装驱动）",
    3: "驱动损坏或内存不足（重装驱动）",
    10: "设备无法启动（重装驱动）",
    12: "资源不足（换 USB 口）",
    14: "需要重启电脑",
    18: "需要重新安装驱动",
    19: "注册表信息损坏（重装驱动）",
    21: "系统正在删除该设备（拔掉重插）",
    22: "设备被禁用（设备管理器里点“启用”或手机重新授权 USB 调试）",
    24: "设备未安装 / 未配置（重装驱动）",
    28: "❌ 驱动程序未安装 ← 最常见的手机缺驱动",
    43: "驱动报告设备故障（卸载设备后重插）",
    45: "设备当前未连接（拔插一次数据线）",
}


# ==================== Bootloader 解锁 / 回锁指令表 ====================
#   unlock / lock 是两个候选指令序列：第一个失败（机型不支持 / 不允许）时自动试下一个
UNLOCK_MODES = {
    "auto": {
        "label": "自动（推荐）",
        "unlock": [["flashing", "unlock"], ["oem", "unlock"]],
        "lock": [["flashing", "lock"], ["oem", "lock"]],
    },
    "standard": {
        "label": "标准 flashing 指令",
        "unlock": [["flashing", "unlock"]],
        "lock": [["flashing", "lock"]],
    },
    "legacy": {
        "label": "旧版 oem 指令",
        "unlock": [["oem", "unlock"]],
        "lock": [["oem", "lock"]],
    },
    "critical": {
        "label": "分区级 critical",
        "unlock": [["flashing", "unlock_critical"]],
        "lock": [["flashing", "lock_critical"]],
    },
}

UNLOCK_TIP = ("解锁（unlock）会清空手机全部数据；小米机型还要先在开发者选项里绑定账号、"
              "取得解锁资格才能成功。回锁（lock）后无法再刷非官方系统。\n"
              "执行时手机会弹出确认界面：按音量键选择 Unlock / Lock，再按电源键确认。")


# ==================== 页面 ====================
class FlashPageMixin:

    def _page_flash(self):
        w = QWidget()
        pageV = QVBoxLayout(w)
        pageV.setContentsMargins(0, 0, 0, 0)
        pageV.setSpacing(10)

        # ============ 左右两栏：左 = 刷入 + MiFlash，右 = 扩展功能 ============
        cols = QHBoxLayout()
        cols.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.setContentsMargins(0, 0, 0, 0)
        # 右栏「竖条」最宽 400px：窗口拉大时多出来的宽度留给左栏
        rightBox = QWidget()
        rightBox.setMaximumWidth(400)
        rightBox.setLayout(right)
        cols.addLayout(left, 3)
        cols.addWidget(rightBox, 2)
        pageV.addLayout(cols, 1)

        # ==================== 左栏 ①：目标分区刷入 ====================
        left.addWidget(self._label("目标分区"))
        self.partCombo = QComboBox()
        self.partCombo.addItems([
            "boot", "init_boot", "recovery", "vendor_boot",
            "dtbo", "vbmeta_system", "super",
            "— 自定义 —",
        ])
        self.partCombo.setFixedHeight(42)
        self.partCombo.currentIndexChanged.connect(self._on_part_changed)
        self.partCombo.setToolTip(
            "boot / init_boot / dtbo / vbmeta / recovery → bootloader（传统 fastboot）\n"
            "init_boot / vendor_boot / super → fastbootd（用户空间）\n"
            "刷错分区会导致无限重启")
        left.addWidget(self.partCombo)

        left.addWidget(self._label("镜像文件"))
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
        self.imgBtn.setMinimumWidth(96)
        self.imgBtn.clicked.connect(self.do_pick)
        frow.addWidget(self.imgBtn)
        left.addLayout(frow)

        self.customPartE = QLineEdit()
        self.customPartE.setPlaceholderText("自定义分区名，例如 misc")
        self.customPartE.setFixedHeight(38)
        self.customPartE.setVisible(False)
        self.customPartE.setToolTip("自定义分区名，例如 misc / param")
        left.addWidget(self.customPartE)

        qm = QHBoxLayout()
        qm.setSpacing(8)
        self.flBtn = QPushButton("⚡  开始刷入")
        self.flBtn.setObjectName("primary")
        self.flBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.flBtn.setFixedHeight(42)
        self.flBtn.setEnabled(False)
        self.flBtn.clicked.connect(self.do_flash)
        qm.addWidget(self.flBtn, 1)
        self.entBtn = QPushButton("📲  进入刷写模式")
        self.entBtn.setObjectName("secondary")
        self.entBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.entBtn.setFixedHeight(42)
        self.entBtn.clicked.connect(self.do_enter)
        qm.addWidget(self.entBtn, 1)
        left.addLayout(qm)

        qm2 = QHBoxLayout()
        qm2.setSpacing(8)
        self.rebootBtn = QPushButton("🔄  重启到系统")
        self.rebootBtn.setObjectName("secondary")
        self.rebootBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rebootBtn.setFixedHeight(42)
        self.rebootBtn.setMinimumWidth(150)
        self.rebootBtn.clicked.connect(lambda: self.do_reboot("system"))
        qm2.addWidget(self.rebootBtn)
        qm2.addStretch(1)
        left.addLayout(qm2)

        # ==================== 右栏：🧩 扩展功能 ====================
        right.addSpacing(2)
        mTitle2 = QLabel("🧩  扩展功能")
        mTitle2.setObjectName("cardTitle")
        right.addWidget(mTitle2)

        # ---- 解锁方式 + 解锁 / 回锁（fastboot，不需要 root）----
        self.unlockModeCb = QComboBox()
        for key, m in UNLOCK_MODES.items():
            self.unlockModeCb.addItem(m["label"], key)
        self.unlockModeCb.setFixedHeight(38)
        self.unlockModeCb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unlockModeCb.setToolTip(
            "选择用哪套指令：\n"
            "· 自动：先试 flashing unlock/lock，不行再试 oem unlock/lock\n"
            "· 标准：fastboot flashing unlock / lock\n"
            "· 旧版：fastboot oem unlock / lock\n"
            "· critical：fastboot flashing unlock_critical / lock_critical\n\n"
            + UNLOCK_TIP)
        right.addWidget(self.unlockModeCb)

        uRow = QHBoxLayout()
        uRow.setSpacing(8)
        self.unlockBtn = QPushButton("🔓  解锁")
        self.relockBtn = QPushButton("🔒  回锁")
        for btn, slot in ((self.unlockBtn, lambda: self.do_unlock("unlock")),
                          (self.relockBtn, lambda: self.do_unlock("lock"))):
            btn.setObjectName("compact")            # 窄栏成对按钮：小内边距，文字不截断
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(38)
            btn.setMinimumWidth(100)
            btn.clicked.connect(slot)
            uRow.addWidget(btn, 1)
        right.addLayout(uRow)
        self.unlockBtn.setToolTip("fastboot flashing unlock / oem unlock（会清空数据）")
        self.relockBtn.setToolTip(
            "fastboot flashing lock / oem lock（回锁后不能再刷非官方系统）")

        self.unlockStateBtn = QPushButton("ℹ  查看解锁状态")
        self.unlockStateBtn.setObjectName("secondary")
        self.unlockStateBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unlockStateBtn.setFixedHeight(38)
        self.unlockStateBtn.setToolTip("读取当前解锁状态：fastboot getvar unlocked")
        self.unlockStateBtn.clicked.connect(self.do_unlock_state)
        right.addWidget(self.unlockStateBtn)

        # ---- root 状态 ----
        self.maintRootLb = QLabel("root 状态：未检测")
        self.maintRootLb.setObjectName("info")
        self.maintRootLb.setWordWrap(True)
        right.addWidget(self.maintRootLb)

        self.maintRootBtn = QPushButton("🔑  检测 / 获取 root")
        self.maintRootBtn.setObjectName("secondary")
        self.maintRootBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.maintRootBtn.setFixedHeight(38)
        self.maintRootBtn.clicked.connect(self.do_maint_root)
        right.addWidget(self.maintRootBtn)

        # ---- 清除数据 / FRP ----
        opRow = QHBoxLayout()
        opRow.setSpacing(8)
        self.maintWipeBtn = QPushButton("🗑  清除数据")
        self.maintFrpBtn = QPushButton("🔓  清除 FRP")
        for btn, key in ((self.maintWipeBtn, "wipe"), (self.maintFrpBtn, "frp")):
            btn.setObjectName("compact")            # 窄栏成对按钮：小内边距，文字不截断
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(40)
            btn.setMinimumWidth(120)
            btn.setToolTip(MAINT_OPS[key]["desc"])
            btn.clicked.connect(lambda _=False, k=key: self.do_maint_op(k))
            opRow.addWidget(btn, 1)
        right.addLayout(opRow)

        # ---- 小米安全设置 / 基带端口 ----
        self.maintSecBtn = QPushButton("🛡  强开小米安全设置")
        self.maintSecBtn.setObjectName("secondary")
        self.maintSecBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.maintSecBtn.setFixedHeight(40)
        self.maintSecBtn.setToolTip(MAINT_OPS["miui_sec"]["desc"])
        self.maintSecBtn.clicked.connect(lambda: self.do_maint_op("miui_sec"))
        right.addWidget(self.maintSecBtn)

        self.maintDiagBtn = QPushButton("📡  打开基带端口")
        self.maintCloseBtn = QPushButton("🔌  关闭基带端口")
        for btn, key in ((self.maintDiagBtn, "diag"), (self.maintCloseBtn, "close_diag")):
            btn.setObjectName("secondary")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(40)
            btn.setMinimumWidth(120)
            btn.setToolTip(MAINT_OPS[key]["desc"])
            btn.clicked.connect(lambda _=False, k=key: self.do_maint_op(k))
        # 窄栏里放同一行会被截字，竖排更稳（顺序和「打开 → 关闭」一致）
        right.addWidget(self.maintDiagBtn)
        right.addWidget(self.maintCloseBtn)

        self.maintWipeMediaCb = QCheckBox("连内部存储一起清（照片 / 文件）")
        self.maintWipeMediaCb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.maintWipeMediaCb.setMinimumWidth(160)
        self.maintWipeMediaCb.setToolTip(
            "勾选后「清除数据」会连 /data/media 一起清（照片、下载、聊天文件全部丢失）；\n"
            "不勾选只清应用与系统数据")
        right.addWidget(self.maintWipeMediaCb)

        self.maintStatusLb = QLabel("")
        self.maintStatusLb.setObjectName("info")
        self.maintStatusLb.setWordWrap(True)
        right.addWidget(self.maintStatusLb)
        right.addStretch(1)

        # ==================== 左栏 ②：MiFlash 一键刷写 ====================
        left.addSpacing(4)
        sep = QFrame()
        sep.setObjectName("hline")
        sep.setFixedHeight(1)
        left.addWidget(sep)
        left.addSpacing(4)

        # ============ MiFlash 一键刷写 ============
        mTitle = QLabel("📦  MiFlash 一键刷写（小米线刷包）")
        mTitle.setObjectName("cardTitle")
        left.addWidget(mTitle)

        mDesc = QLabel("选择解压后的刷机包里的 flash_all.bat（清除全部数据） / "
                       "flash_all_lock.bat（刷写并回锁）")
        mDesc.setObjectName("desc")
        mDesc.setWordWrap(True)
        left.addWidget(mDesc)

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
        self.miflashPickBtn.setMinimumWidth(110)
        self.miflashPickBtn.clicked.connect(self.do_miflash_pick)
        mRow1.addWidget(self.miflashPickBtn)
        left.addLayout(mRow1)

        # 两个选项（竖排，避免窄栏里挤不下）
        self.miflashEraseCb = QCheckBox("清除数据（userdata / cache / metadata）")
        self.miflashEraseCb.setChecked(True)
        self.miflashEraseCb.setEnabled(False)
        self.miflashEraseCb.setCursor(Qt.CursorShape.PointingHandCursor)
        left.addWidget(self.miflashEraseCb)

        self.miflashLockCb = QCheckBox("回锁 Bootloader（重新上锁）")
        self.miflashLockCb.setChecked(False)
        self.miflashLockCb.setEnabled(False)
        self.miflashLockCb.setCursor(Qt.CursorShape.PointingHandCursor)
        left.addWidget(self.miflashLockCb)

        # 脚本信息
        self.miflashInfo = QLabel("尚未选择脚本")
        self.miflashInfo.setObjectName("info")
        self.miflashInfo.setWordWrap(True)
        left.addWidget(self.miflashInfo)

        # 开始按钮
        mRow2 = QHBoxLayout()
        mRow2.setSpacing(10)
        self.miflashStartBtn = QPushButton("🚀  开始 MiFlash 刷写")
        self.miflashStartBtn.setObjectName("primary")
        self.miflashStartBtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.miflashStartBtn.setFixedHeight(44)
        self.miflashStartBtn.setEnabled(False)
        self.miflashStartBtn.clicked.connect(self.do_miflash_start)
        mRow2.addWidget(self.miflashStartBtn, 1)
        left.addLayout(mRow2)

        left.addStretch(1)

        # ============ 进度条 ============
        progRow = QHBoxLayout()
        progRow.setSpacing(10)
        plb = QLabel("刷入进度")
        plb.setObjectName("info")
        progRow.addWidget(plb)
        self.flashStageLabel = QLabel("等待开始")
        self.flashStageLabel.setObjectName("info")
        progRow.addWidget(self.flashStageLabel)
        self.flashProg = AnimatedProgressBar()
        self.flashProg.setFixedHeight(20)
        self.flashProg.setFormat("空闲")
        progRow.addWidget(self.flashProg, 1)
        pageV.addLayout(progRow)

        # 内部状态
        self._miflash_script = None
        self._miflash_cmds = []
        self._miflash_running = False

        # 扩展功能（解锁/回锁 + 清数据/FRP/基带端口）状态
        self._maint_busy = False
        self._maint_root_state = "unknown"      # unknown / root / unauth / none
        self._unlock_busy = False

        self._on_part_changed(0)
        return w

    # ==================== 单分区刷入逻辑（原有） ====================
    def _on_part_changed(self, idx):
        t = self.partCombo.currentText()
        self.customPartE.setVisible(t.startswith("—"))

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
        """刷入进度：-1 = 不确定（滚动动画），否则平滑推进并显示百分比"""
        try:
            if not hasattr(self, "flashProg"):
                return
            if val < 0:
                self.flashProg.set_busy(True, "刷写中…")
                self.flashStageLabel.setText("发送数据中…")
                return
            target = max(0, min(100, int(val)))
            self.flashProg.set_smooth_value(target, f"{target}%")
            if target >= 90:
                stage = "校验中…"
            elif target >= 55:
                stage = "写入中…"
            elif target >= 20:
                stage = "发送中…"
            else:
                stage = "准备中…"
            self.flashStageLabel.setText(stage if target < 100 else "完成")
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

        # 识别镜像类型并提示应刷入的分区（刷错分区会无限重启）
        try:
            from pages.page_patch import _peek_boot_image
            k = _peek_boot_image(f)
            self.lg(f"镜像识别：{k['desc']}", "info")
            if k["kind"] in ("boot", "init_boot"):
                cur = self._current_part()
                if cur in ("boot", "init_boot") and cur != k["kind"]:
                    self.lg(f"⚠ 这是 {k['kind']} 镜像，目标分区建议改为 {k['kind']}；"
                            f"刷错分区会导致手机无限重启", "warn")
                elif cur == k["kind"]:
                    self.lg(f"✓ 目标分区与镜像类型一致（{cur}）", "ok")
            self.lg(f"提示：{self._current_part() or '—'} → "
                    f"{self._target_mode()}；boot/dtbo/vbmeta/recovery 走 bootloader，"
                    f"init_boot/vendor_boot/super 走 fastbootd", "info")
        except Exception:
            self.lg(f"提示：已选镜像 {Path(f).name}", "info")

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

                # ---- 按 fastboot 真实阶段跳进度 ----
                if "sending" in low:
                    # "Sending 'boot' (65536 KB)" → 开始发送
                    self.sig.flashprog.emit(20)
                elif "okay" in low and "sending" not in low:
                    # Sending 完成后的 "OKAY"
                    self.sig.flashprog.emit(55)
                elif "writing" in low:
                    # "Writing 'boot' ..." → 开始写入
                    self.sig.flashprog.emit(80)
                elif "finished" in low:
                    self.sig.flashprog.emit(95)

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
                # Recovery 下 adb 也能发 reboot，别再报“没有 ADB 设备”
                serial = adb_shell_dev()
                if serial:
                    ds = [serial]
                    self.sig.log.emit("设备在 Recovery 模式：用 adb reboot 切换模式", "info")
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
            set_state(self.miflashInfo, "warn")
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
        # 文件名里的 lock 要当成独立单词（排除 unlock / nolock）
        name_lock = (bool(re.search(r"(?<![a-z])lock(?![a-z])", name_low))
                     and "unlock" not in name_low)
        if name_lock:
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
        # 勾选框：按脚本内容自动识别（用户之后仍可手动改）
        auto_erase = n_erase > 0
        auto_lock = n_lock > 0 or name_lock
        self.miflashEraseCb.setEnabled(auto_erase)
        self.miflashEraseCb.setChecked(auto_erase)
        self.miflashLockCb.setEnabled(True)
        self.miflashLockCb.setChecked(auto_lock)
        lines.append(
            "  · 自动识别："
            + ("已勾选「清除数据」" if auto_erase
               else "未勾选「清除数据」（本脚本不清数据）")
            + "，"
            + ("已勾选「回锁」" if auto_lock
               else "未勾选「回锁」（勾上会追加 flashing lock）"))
        self.miflashInfo.setText("\n".join(lines))
        set_state(self.miflashInfo, "text")

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
        if will_lock or any(c["type"] == "lock" for c in self._miflash_cmds):
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
        self.flashProg.set_busy(False)
        self.flashProg.reset()
        self.flashProg.setFormat("0%")

        threading.Thread(
            target=self._miflash_worker,
            args=(fs[0], will_erase, will_lock),
            daemon=True,
        ).start()

    def _miflash_build_todo(self, cmds, will_erase, will_lock):
        """按勾选过滤出要执行的命令（勾了回锁但脚本没有 → 自动追加 flashing lock）"""
        todo = []
        for c in cmds:
            if c["type"] == "erase":
                if will_erase:
                    todo.append(c)
            elif c["type"] == "flash":
                todo.append(c)
            elif c["type"] == "lock":
                if will_lock:
                    todo.append(c)
            # reboot 不在这里执行（后面统一询问）
        if will_lock and not any(c["type"] == "lock" for c in cmds):
            todo.append({"type": "lock", "cmd": "flashing lock"})
        return todo

    def _miflash_worker(self, dev_id, will_erase, will_lock):
        try:
            cmds = self._miflash_cmds
            # 过滤出要执行的命令
            todo = self._miflash_build_todo(cmds, will_erase, will_lock)
            if will_lock and not any(c["type"] == "lock" for c in cmds):
                self.sig.log.emit(
                    "ℹ 脚本内没有回锁命令，已追加「fastboot flashing lock」", "warn")

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

    # ==================== Bootloader 解锁 / 回锁 ====================
    def _unlock_set_status(self, text, state="dim"):
        """解锁/回锁的结果也显示在「扩展功能」共用的那行状态里"""
        try:
            self.maintStatusLb.setText(text)
            set_state(self.maintStatusLb, state)
        except Exception:
            pass

    def _unlock_btns(self):
        return [b for b in (getattr(self, "unlockBtn", None),
                            getattr(self, "relockBtn", None),
                            getattr(self, "unlockStateBtn", None))
                if b is not None]

    def _unlock_set_busy(self, busy, text=None, state="warn"):
        self._unlock_busy = busy
        for b in self._unlock_btns():
            try:
                b.setEnabled(not busy)
            except Exception:
                pass
        try:
            self.unlockModeCb.setEnabled(not busy)
        except Exception:
            pass
        if text is not None:
            self._unlock_set_status(text, state)

    def _unlock_ensure_fastboot(self):
        """确保设备在 Fastboot：只有 ADB 时自动 reboot bootloader 并等它回来"""
        fs, _ = fb_dev()
        if fs:
            return fs[0]
        ds, _ = adb_dev()
        if not ds:
            self.sig.log.emit("❌ 没检测到设备（请连手机 / 先进 Fastboot）", "error")
            return ""
        self.sig.log.emit("设备在系统内，先重启到 Bootloader …", "info")
        rc, out = sh(["adb", "-s", ds[0], "reboot", "bootloader"], timeout=15)
        if out:
            self.sig.log.emit(out, "plain")
        for _ in range(30):
            time.sleep(2)
            fs, _ = fb_dev()
            if fs:
                self.sig.log.emit(f"✓ 已进入 Fastboot：{fs[0]}", "ok")
                self.sig.dev.emit(f"● FB: {fs[0]}", self.theme["ok"])
                return fs[0]
        self.sig.log.emit("⚠ 未检测到 fastboot 设备，请手动检查手机屏幕", "warn")
        return ""

    def _unlock_read_state(self, dev_id):
        """读取解锁状态 → (True 已解锁 / False 未解锁 / None 读不到, 说明)"""
        rc, out = sh(["fastboot", "-s", dev_id, "getvar", "unlocked"], timeout=30)
        m = re.search(r"unlocked:\s*(yes|no)", (out or "").lower())
        if m:
            return m.group(1) == "yes", f"unlocked: {m.group(1)}"
        rc, out2 = sh(["fastboot", "-s", dev_id, "oem", "device-info"], timeout=30)
        if out2:
            self.sig.log.emit(out2, "plain")
        m2 = re.search(r"device unlocked:\s*(true|false)", (out2 or "").lower())
        if m2:
            return m2.group(1) == "true", f"unlocked: {m2.group(1)}"
    def do_unlock(self, action):
        """action = unlock / lock"""
        if getattr(self, "_unlock_busy", False) or \
                getattr(self, "_maint_busy", False):
            return
        if action == "unlock":
            r = QMessageBox.question(
                self, "确认解锁 Bootloader",
                "即将解锁 Bootloader（fastboot unlock）：\n"
                "· 会清空手机全部数据（首次解锁）\n"
                "· 小米机型需要先在开发者选项里绑定账号、取得解锁资格\n"
                "· 手机会弹出确认界面：音量键选 Unlock / Yes，电源键确认\n\n"
                "确定继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        else:
            r = QMessageBox.question(
                self, "确认回锁 Bootloader",
                "即将回锁 Bootloader（fastboot lock）：\n"
                "· 回锁后无法再刷入非官方系统 / 第三方 Recovery\n"
                "· 部分机型回锁会再次清空数据\n"
                "· 手机必须是官方系统，否则可能无法开机\n\n"
                "确定继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            self._unlock_set_status("已取消", "dim")
            return
        self._unlock_set_busy(True, "正在执行…", "warn")
        try:
            self.flashProg.set_busy(
                True, "解锁指令执行中…" if action == "unlock" else "回锁指令执行中…")
            self.flashStageLabel.setText("准备中…")
        except Exception:
            pass
        mode_key = self.unlockModeCb.currentData() or "auto"
        threading.Thread(target=self._unlock_worker, args=(action, mode_key),
                         daemon=True).start()

    def do_unlock_state(self):
        if getattr(self, "_unlock_busy", False) or \
                getattr(self, "_maint_busy", False):
            return
        self._unlock_set_busy(True, "正在读取…", "warn")
        try:
            self.flashProg.set_busy(True, "读取解锁状态…")
            self.flashStageLabel.setText("读取解锁状态…")
        except Exception:
            pass
        threading.Thread(target=self._unlock_worker, args=("state", "auto"),
                         daemon=True).start()

    def _unlock_finish(self, text, state="ok"):
        mark = {"ok": "✅", "warn": "⚠"}.get(state, "❌")
        self._unlock_set_busy(False, f"{mark} {text}", state)
        try:
            self.flashProg.set_busy(False)
            if state == "ok":
                self.flashProg.set_smooth_value(100, "100%")
                self.flashStageLabel.setText("完成")
            else:
                self.flashProg.reset()
                self.flashProg.setFormat("空闲")
                self.flashStageLabel.setText(
                    "完成（有提示）" if state == "warn" else "失败")
        except Exception:
            pass
        self.sig.log.emit(f"{mark} {text}",
                          {"ok": "ok", "warn": "warn"}.get(state, "error"))
    def _unlock_worker(self, action, mode_key):
        """执行解锁 / 回锁 / 读状态（后台线程）"""
        try:
            self.sig.log.emit("=" * 56, "plain")
            name = {"unlock": "解锁 Bootloader", "lock": "回锁 Bootloader",
                    "state": "读取解锁状态"}[action]
            self.sig.log.emit(f"🔓 {name}", "info")
            dev = self._unlock_ensure_fastboot()
            if not dev:
                self.sig.ui.emit(lambda n=name: self._unlock_finish(
                    f"{n}失败：未检测到 Fastboot 设备", "err"))
                return

            if action == "state":
                val, txt = self._unlock_read_state(dev)
                if val is None:
                    self.sig.ui.emit(lambda t=txt: self._unlock_finish(t, "warn"))
                elif val:
                    self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                        f"当前已解锁（{t}）", "ok"))
                else:
                    self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                        f"当前未解锁（{t}）", "warn"))
                return

            cands = UNLOCK_MODES.get(mode_key, UNLOCK_MODES["auto"])[action]
            want = "Unlock" if action == "unlock" else "Lock"
            self.sig.log.emit(
                f"请在手机上确认：音量键选 {want}，再按电源键（部分机型无需确认）",
                "warn")
            last_out = ""
            for i, args in enumerate(cands):
                cmd = ["fastboot", "-s", dev] + list(args)
                self.sig.log.emit(f"$ {' '.join(cmd)}", "info")
                self.sig.ui.emit(lambda a=args: self.flashStageLabel.setText(
                    "执行：fastboot " + " ".join(a)))
                rc, out = sh_stream(
                    cmd, on_text=lambda s: self.sig.log.emit(s.strip(), "plain")
                    if s.strip() else None, timeout=240)
                last_out = out or ""
                low = last_out.lower()
                bad = any(k in low for k in (
                    "failed", "not allowed", "unknown command", "not supported",
                    "errno", "cannot", "no such"))
                if rc == 0 and not bad:
                    val, txt = self._unlock_read_state(dev)
                    if action == "unlock":
                        if val is True:
                            self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                                f"解锁成功（{t}）", "ok"))
                        elif val is False:
                            self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                                f"指令已发送，但设备仍是未解锁（{t}）——"
                                "小米机型需先取得解锁资格", "err"))
                        else:
                            self.sig.ui.emit(lambda: self._unlock_finish(
                                "解锁指令已发送（设备可能正在重启 / 等待手机确认）",
                                "warn"))
                    else:
                        if val is False:
                            self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                                f"回锁成功（{t}）", "ok"))
                        elif val is True:
                            self.sig.ui.emit(lambda t=txt: self._unlock_finish(
                                f"指令已发送，但设备仍是解锁状态（{t}）", "err"))
                        else:
                            self.sig.ui.emit(lambda: self._unlock_finish(
                                "回锁指令已发送（设备可能正在重启）", "warn"))
                    return
                first = next((l.strip() for l in last_out.splitlines()
                              if l.strip()), "无输出")
                if i < len(cands) - 1:
                    self.sig.log.emit(
                        f"⚠ fastboot {' '.join(args)} 不支持 / 被拒绝"
                        f"（{first[:70]}），改试 fastboot {' '.join(cands[i + 1])}",
                        "warn")
                    continue
            first = next((l.strip() for l in last_out.splitlines() if l.strip()),
                         "无输出")
            self.sig.ui.emit(lambda n=name, f=first: self._unlock_finish(
                f"{n}失败：{f[:90]}", "err"))
        except Exception as e:
            self.sig.log.emit(f"❌ 解锁操作异常：{e}", "error")
            self.sig.log.emit(traceback.format_exc(), "error")
            self.sig.ui.emit(lambda: self._unlock_finish("解锁操作异常", "err"))




        return None, "未能回读解锁状态（设备可能正在重启）"


    # ==================== 扩展功能：清除数据 / FRP / MIUI 安全设置 / 基带端口 ====================
    def _maint_log(self, msg, level="plain"):
        try:
            self.lg(msg, level)
        except Exception:
            pass

    def _maint_set_status(self, text, state="dim"):
        try:
            self.maintStatusLb.setText(text)
            set_state(self.maintStatusLb, state)
        except Exception:
            pass

    def _maint_set_root_state(self, state, text):
        """root 状态：root / unauth / none / unknown"""
        self._maint_root_state = state
        try:
            self.maintRootLb.setText(text)
            set_state(self.maintRootLb,
                      {"root": "ok", "unauth": "warn",
                       "none": "err"}.get(state, "dim"))
        except Exception:
            pass

    def _maint_btns(self):
        return [b for b in (getattr(self, "maintWipeBtn", None),
                            getattr(self, "maintFrpBtn", None),
                            getattr(self, "maintSecBtn", None),
                            getattr(self, "maintDiagBtn", None),
                            getattr(self, "maintCloseBtn", None),
                            getattr(self, "maintRootBtn", None)) if b is not None]

    def _maint_set_busy(self, busy, text=None, state="dim"):
        self._maint_busy = busy
        for b in self._maint_btns():
            try:
                b.setEnabled(not busy)
            except Exception:
                pass
        try:
            self.maintWipeMediaCb.setEnabled(not busy)
        except Exception:
            pass
        if text is not None:
            self._maint_set_status(text, state)

    # ---------------- root 检测 / 获取 ----------------
    def _maint_root_probe(self, dev):
        """检测 root，返回 (state, 状态文字, [(命令, 输出), ...])

        顺序：adbd 是否已 root → su -c id → su 0 id → 是否存在 su → adb root
        state：root（uid=0）/ unauth（有 su 但没授权）/ none（没有 root）
        """
        log = []

        def run(cmd, timeout=20):
            rc, out = sh(cmd, timeout=timeout)
            out = out or ""
            log.append((" ".join(cmd), out))
            return rc, out

        rc, out = run(["adb", "-s", dev, "shell", "id"], 15)
        if rc == 0 and "uid=0" in out:
            return "root", "root 状态：✅ 已获取（adbd 已是 root）", log

        for su_argv in (["su", "-c", "id"], ["su", "0", "id"]):
            rc, out = run(["adb", "-s", dev, "shell"] + su_argv, 25)
            if rc == 0 and "uid=0" in out:
                return "root", "root 状态：✅ 已获取（su 授权）", log

        rc, out = run(["adb", "-s", dev, "shell",
                       "ls /system/bin/su /system/xbin/su /sbin/su"], 15)
        # 只看真正存在的路径（ls 报错信息里也含 "su"，不能用 in 判断）
        if any(ln.strip().rstrip("/").endswith("/su") for ln in out.splitlines()):
            return "unauth", "root 状态：⚠ 检测到 su 但未授权（手机上点“允许”）", log

        rc, out = run(["adb", "-s", dev, "root"], 20)
        low = out.lower()
        if rc == 0 and "cannot run as root" not in low and "not allowed" not in low:
            sh(["adb", "-s", dev, "wait-for-device"], timeout=30)
            time.sleep(1.5)
            rc, out = run(["adb", "-s", dev, "shell", "id"], 15)
            if rc == 0 and "uid=0" in out:
                return "root", "root 状态：✅ 已获取（adb root）", log

        return "none", "root 状态：❌ 未 root（su / adb root 都不可用）", log

    def _maint_ensure_root(self, dev, title):
        """需要 root 的操作执行前调用：先检测/尝试获取 root，True = 可以继续"""
        state, text, log = self._maint_root_probe(dev)
        for cmd, out in log:
            self.sig.log.emit(f"$ {cmd}", "plain")
            if out:
                self.sig.log.emit(out, "plain")
        self.sig.ui.emit(lambda s=state, t=text: self._maint_set_root_state(s, t))
        if state == "root":
            self.sig.log.emit("✓ root 权限校验通过（uid=0）", "ok")
            return True
        self.sig.log.emit(f"❌ {title} 需要 root 权限，当前 {text}", "error")
        if state == "unauth":
            msg = ("检测到设备里有 su，但没有授权给 adb。\n\n"
                   "请在手机上打开 Magisk / root 管理器，"
                   "把「adb shell」的请求改成“允许（永久）”，再重新点这个功能。")
        else:
            msg = ("该设备当前没有 root 权限，无法执行这个操作。\n\n"
                   "请先 root（例如刷入用 Magisk 修补过的 boot / init_boot），"
                   "或到「临时提权」页用提权文件拿到临时 root 后再回来。")
        self.sig.ui.emit(lambda m=msg: QMessageBox.warning(self, "需要 root 权限", m))
        return False

    def do_maint_root(self):
        """按钮：检测 / 获取 root（有 su 就直接用，没有就试 adb root）"""
        if getattr(self, "_maint_busy", False):
            return
        self._maint_set_busy(True, "正在检测 root 权限…", "warn")
        try:
            self.flashProg.set_busy(True, "检测 root…")
            self.flashStageLabel.setText("检测 root 权限…")
        except Exception:
            pass
        threading.Thread(target=self._maint_root_worker, daemon=True).start()

    def _maint_auto_probe(self):
        """切到本页时自动检测一次 root（已检测过 / 正在忙就跳过）"""
        if getattr(self, "_maint_busy", False):
            return
        if getattr(self, "_maint_root_state", "unknown") != "unknown":
            return
        try:
            ds, _ = adb_dev()
        except Exception:
            return
        if not ds:
            self._maint_set_root_state("unknown", "root 状态：未连接 ADB 设备")
            return
        self.sig.log.emit("检测设备 root 状态 …", "info")
        self.do_maint_root()


    def _maint_root_worker(self):
        try:
            ds, _ = adb_dev()
            if not ds:
                self.sig.log.emit("❌ 未检测到 ADB 设备（请打开 USB 调试并授权）", "error")
                self.sig.ui.emit(lambda: self._maint_set_root_state(
                    "unknown", "root 状态：未连接 ADB 设备"))
                self.sig.ui.emit(lambda: self._maint_finish(
                    False, "未连接 ADB 设备", "err"))
                return
            dev = ds[0]
            self.sig.log.emit(f"检测 root：{dev} …", "info")
            state, text, log = self._maint_root_probe(dev)
            for cmd, out in log:
                self.sig.log.emit(f"$ {cmd}", "plain")
                if out:
                    self.sig.log.emit(out, "plain")
            self.sig.ui.emit(lambda s=state, t=text: self._maint_set_root_state(s, t))
            self.sig.ui.emit(lambda s=state, t=text: self._maint_finish(
                s == "root", t,
                "ok" if s == "root" else ("warn" if s == "unauth" else "err")))
        except RuntimeError:
            return                       # 窗口已关闭（信号对象已销毁），忽略
        except Exception as e:
            self.sig.log.emit(f"❌ root 检测异常：{e}", "error")
            self.sig.ui.emit(lambda: self._maint_finish(False, "root 检测异常", "err"))


    # ---------------- 四个维护操作 ----------------
    def do_maint_op(self, key):
        op = MAINT_OPS.get(key)
        if not op or getattr(self, "_maint_busy", False):
            return
        extra = ""
        if key == "wipe":
            extra = ("\n· 内部存储（照片 / 文件）也会一起清空"
                     if self.maintWipeMediaCb.isChecked()
                     else "\n· 内部存储（照片 / 文件）会保留")
        r = QMessageBox.question(
            self, f"确认：{op['title']}",
            (op.get("confirm") or f"即将执行：{op['title']}\n") + extra + "\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            self._maint_log(f"已取消：{op['title']}", "warn")
            return
        full = key == "wipe" and self.maintWipeMediaCb.isChecked()
        self._maint_set_busy(True, f"{op['title']}：正在执行…", "warn")
        try:
            self.flashProg.reset()
            self.flashProg.set_busy(True, f"{op['title']}…")
            self.flashStageLabel.setText("准备中…")
        except Exception:
            pass
        threading.Thread(target=self._maint_worker, args=(key, full),
                         daemon=True).start()

    def _maint_stage(self, label, pct):
        try:
            self.flashStageLabel.setText(label)
            self.sig.flashprog.emit(int(pct))
        except Exception:
            pass

    def _maint_finish(self, ok, text, state="ok"):
        """收尾：状态栏 + 进度条 + 日志

        state: ok（成功）/ warn（有提示，如驱动检查发现问题）/ err（失败）
        """
        self._maint_set_busy(False)
        mark = "✅" if (ok and state == "ok") else \
            ("⚠" if state == "warn" else "❌")
        self._maint_set_status(f"{mark} {text}", state)
        try:
            self.flashProg.set_busy(False)
            if state == "err":
                self.flashProg.reset()
                self.flashProg.setFormat("空闲")
                self.flashStageLabel.setText("失败")
            else:
                self.flashProg.set_smooth_value(100, "100%")
                self.flashStageLabel.setText(
                    "完成" if state == "ok" else "完成（有提示）")
        except Exception:
            pass
        self.sig.log.emit(f"{mark} {text}",
                          {"err": "error"}.get(state, state))


    # ---------------- 端口核对（以真实端口出现为准） ----------------
    def _maint_scan_ports(self):
        """列出电脑上的串口设备名（Windows PnP）"""
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "Get-CimInstance Win32_PnPEntity | "
               "Where-Object { $_.Name -match '\\(COM\\d+\\)' } | "
               "ForEach-Object { $_.Name }"]
        rc, out = sh(cmd, timeout=25)
        if rc != 0 or not out:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def _maint_pnp_list(self, only_problem=False):
        """列出 PnP 设备 → [(名称, 错误码, 状态)]；only_problem=True 只看驱动异常的"""
        cond = ("Where-Object { $_.ConfigManagerErrorCode -ne 0 } | "
                if only_problem else "")
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "Get-CimInstance Win32_PnPEntity | " + cond +
               'ForEach-Object { "$($_.Name)|$($_.ConfigManagerErrorCode)|$($_.Status)" }']
        rc, out = sh(cmd, timeout=30)
        if rc != 0 or not out:
            return []
        rows = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln or ln.count("|") < 2:
                continue
            name, code, status = ln.split("|", 2)
            try:
                code = int(code.strip())
            except Exception:
                code = -1
            rows.append((name.strip(), code, status.strip()))
        return rows

    def _maint_check_drivers(self, quiet=False):
        """检查手机 / Android 相关驱动是否正常

        返回 (ok, 结论, 明细行)；明细会写进日志，结论用于状态栏。
        """
        rows = self._maint_pnp_list()
        detail = []
        if not rows:
            txt = "无法读取设备列表（PowerShell 不可用？请手动打开设备管理器查看）"
            if not quiet:
                self.sig.log.emit(f"🔎 驱动检查：{txt}", "warn")
            return False, txt, detail

        problems = [r for r in rows if r[1] not in (0, -1)]
        phones = [r for r in rows
                  if any(k in r[0].lower() for k in PHONE_PNP_KEYS)]
        phone_bad = [r for r in phones if r[1] not in (0, -1)]

        for name, code, status in phones:
            mark = "✓" if code in (0, -1) else "⚠"
            hint = "" if code in (0, -1) else \
                f"  → {DRIVER_ERR_HINTS.get(code, f'错误码 {code}')}"
            detail.append(f"  {mark} {name}（错误码 {code}）{hint}")
        for name, code, status in problems:
            if name not in [p[0] for p in phones]:
                detail.append(f"  ⚠ {name}（错误码 {code}）  → "
                              f"{DRIVER_ERR_HINTS.get(code, '驱动异常')}")

        if not quiet:
            self.sig.log.emit("🔎 驱动检查：", "info")
            if detail:
                for d in detail:
                    self.sig.log.emit(d, "warn" if "⚠" in d else "plain")
            else:
                self.sig.log.emit("  没看到手机 / Android 相关设备", "plain")

        if phone_bad:
            first = phone_bad[0]
            txt = (f"驱动异常：{first[0]}（错误码 {first[1]}，"
                   f"{DRIVER_ERR_HINTS.get(first[1], '需要重装驱动')}）")
            if not quiet:
                self.sig.log.emit(
                    "  → 建议：装一遍通用手机驱动（高通/MTK 驱动包），"
                    "或在设备管理器右键这个设备 → 更新/卸载后重插数据线", "warn")
            return False, txt, detail
        if phones:
            names = "、".join(p[0] for p in phones[:3])
            txt = f"驱动正常（{names}）"
            if not quiet:
                self.sig.log.emit(f"  → {txt}", "ok")
            return True, txt, detail
        if problems:
            txt = f"没识别到手机（有 {len(problems)} 个设备驱动异常，可能是手机）"
            if not quiet:
                self.sig.log.emit(
                    "  → 建议：手机连电脑后确认“USB 调试/文件传输”，"
                    "缺驱动时设备管理器里会显示黄色感叹号（错误码 28）", "warn")
            return False, txt, detail
        txt = "没看到手机 / Android 相关设备（驱动没装 / 数据线只充电 / 手机没连）"
        if not quiet:
            self.sig.log.emit(f"  → {txt}", "warn")
        return False, txt, detail

    def _maint_verify(self, kind, before=None):
        """返回 (ok, 说明)：ok=True 表示电脑上确实看到了端口

        判定顺序：新出现的"诊断类"端口 → 新出现的任意串口 →
        本来就存在的诊断类端口 → 都按成功；完全没变化就判失败。
        """
        if kind == "port_closed":
            return self._maint_verify_closed(before)
        if kind != "baseband_port":
            return True, ""
        before = list(before or [])
        names = []
        for attempt in range(3):                     # USB 枚举要几秒，多扫两次
            names = self._maint_scan_ports()
            strong = [n for n in names
                      if any(k in n.lower() for k in DIAG_PORT_KEYS)]
            strong_new = [n for n in strong if n not in before]
            new = [n for n in names if n not in before]
            if strong_new:
                for n in strong_new:
                    self.sig.log.emit(f"  ✓ 电脑上出现诊断端口：{n}", "ok")
                return True, "端口已打开（" + "、".join(strong_new[:3]) + "）"
            if new:
                for n in new:
                    self.sig.log.emit(f"  ✓ 电脑上多出新端口：{n}", "ok")
                return True, "端口已打开（" + "、".join(new[:3]) + "）"
            if strong and attempt >= 1:
                for n in strong:
                    self.sig.log.emit(f"  ✓ 电脑上存在诊断端口：{n}（操作前就有）", "ok")
                return True, "端口已在（" + "、".join(strong[:3]) + "）"
            if attempt < 2:
                time.sleep(2)
        # 端口没出来 = 失败（顺带在日志里查一遍驱动，界面只提示"未检测到端口"）
        if names:
            self.sig.log.emit("  当前电脑上的串口：" + " / ".join(names[:8]), "plain")
        else:
            self.sig.log.emit("  当前电脑上没有任何串口设备", "plain")
        try:
            self._maint_check_drivers()
        except Exception:
            pass
        return False, ("未检测到端口（电脑上没有出现新的串口）——"
                       "部分机型需要重启手机、或拔插一次数据线后端口才会出现")

    def _maint_verify_closed(self, before=None):
        """关闭端口后的核对：诊断类端口应该消失"""
        names = []
        for attempt in range(3):
            names = self._maint_scan_ports()
            still = [n for n in names
                     if any(k in n.lower() for k in DIAG_PORT_KEYS)]
            if not still:
                self.sig.log.emit("  ✓ 电脑上已看不到诊断端口", "ok")
                return True, "端口已关闭（已切回普通 USB：MTP + ADB）"
            if attempt < 2:
                time.sleep(2)
        for n in still[:4]:
            self.sig.log.emit(f"  ⚠ 仍然存在诊断端口：{n}", "warn")
        return False, ("诊断端口仍然存在：" + "、".join(still[:2]) +
                       "——部分机型需要重启手机（或拔插一次数据线）后才会消失")

    def _maint_run_su(self, dev, steps, title):
        """逐条执行 adb shell su -c "<cmd>"，返回 (ok, 说明)

        步骤支持第 3 个元素 "soft"：失败只警告不中断
        （打开基带端口会切 USB 配置，adb 掉线导致返回码非 0 属正常现象）。
        """
        total = max(1, len(steps))
        gone = False                  # ADB 已确认掉线（端口模式），后面不再反复等
        for i, step in enumerate(steps, 1):
            label, cmd = step[0], step[1]
            soft = len(step) > 2 and step[2] == "soft"
            if gone and soft:
                self.sig.log.emit(
                    f"  ⏭ [{i}/{total}] {label}：ADB 已断开，跳过（不影响端口已打开）",
                    "warn")
                continue
            self.sig.ui.emit(lambda l=label, p=int((i - 1) * 100 / total):
                             self._maint_stage(l, p))
            self.sig.log.emit(f"▸ [{i}/{total}] {label}", "info")
            self.sig.log.emit(f"  $ adb -s {dev} shell su -c \"{cmd}\"", "plain")
            t0 = time.time()
            rc, out = sh(["adb", "-s", dev, "shell", "su", "-c", cmd], timeout=300)
            if rc != 0 and self._maint_adb_lost(out) and not gone:
                # 端口切换 / USB 重新枚举时 adb 会掉一下 → 等它回来重试一次
                self.sig.log.emit("  ℹ ADB 掉线（切 USB 配置的正常现象），等待重连…",
                                  "warn")
                newdev = self._maint_wait_adb(15)
                if newdev:
                    dev = newdev
                    rc, out = sh(["adb", "-s", dev, "shell", "su", "-c", cmd],
                                 timeout=300)
                else:
                    gone = True
            if out:
                self.sig.log.emit(out, "plain")
            if gone and soft:
                self.sig.log.emit(
                    f"  ⚠ {label}：ADB 已断开（正常），端口是否打开下面核对", "warn")
                continue
            if rc != 0:
                if soft:
                    self.sig.log.emit(
                        f"  ⚠ {label}：返回码 {rc}（这一步忽略，端口是否打开下面核对）",
                        "warn")
                    continue
                return False, f"{title}：{label} 失败（返回码 {rc}）"
            self.sig.log.emit(f"  ✓ {label}（{time.time() - t0:.1f}s）", "ok")
            self.sig.log.emit(f"  ✓ {label}（{time.time() - t0:.1f}s）", "ok")
        self.sig.ui.emit(lambda: self._maint_stage("完成", 100))
        return True, f"{title}完成"

    @staticmethod
    def _maint_adb_lost(out):
        """命令输出看着像 adb 掉线了（真实报错形如 device 'ABC123' not found）"""
        low = (out or "").lower()
        return bool(re.search(
            r"not found|device offline|no devices/emulators|error: closed|"
            r"still connecting|unauthorized|transport error|connection reset",
            low))

    def _maint_wait_adb(self, timeout=15):
        """等 adb 设备回来（USB 重新枚举），返回新设备号，超时返回空串"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                ds, _ = adb_dev()
            except Exception:
                ds = []
            if ds:
                self.sig.log.emit(f"  ✓ ADB 已重连：{ds[0]}", "ok")
                return ds[0]
            time.sleep(1.0)
        self.sig.log.emit("  ⚠ ADB 未重连（端口模式下 adb 可能就不回来了，属正常）",
                          "warn")
        return ""

    def _maint_run_fastboot(self, dev_id, op):
        cmd = ["fastboot", "-s", dev_id] + list(op["fastboot"])
        if op.get("fastboot_note"):
            self.sig.log.emit(f"ℹ {op['fastboot_note']}", "info")
        self.sig.log.emit(f"$ {' '.join(cmd)}", "info")
        self.sig.ui.emit(lambda: self._maint_stage("fastboot 执行中…", 40))

        def on_text(line):
            s = (line or "").strip()
            if s:
                self.sig.log.emit(s, "plain")

        rc, out = sh_stream(cmd, on_text=on_text, timeout=900)
        low = (out or "").lower()
        if rc == 0 and "failed" not in low and "error" not in low:
            return True, f"{op['title']}完成"
        return False, f"{op['title']}失败（返回码 {rc}）"


    def _maint_worker(self, key, full_wipe=False):
        """后台执行：能走 fastboot 就走 fastboot，否则 ADB + root"""
        op = MAINT_OPS[key]
        try:
            self.sig.log.emit("=" * 56, "plain")
            self.sig.log.emit(f"🛠 {op['title']}", "info")
            # 需要在电脑上核对端口的操作：先记录操作前的串口列表
            before_ports = []
            if op.get("verify"):
                before_ports = self._maint_scan_ports()
                self.sig.log.emit(
                    "操作前电脑上的串口：" + (" / ".join(before_ports[:6])
                                     if before_ports else "无"), "plain")
            ds, _ = adb_dev()
            fs, _ = fb_dev()

            if op.get("fastboot") and fs and not ds:
                self.sig.log.emit(f"设备在 Fastboot：{fs[0]}（走 fastboot 命令）", "info")
                ok, msg = self._maint_run_fastboot(fs[0], op)
            else:
                if not ds:
                    if fs:
                        self.sig.log.emit(
                            f"❌ {op['title']} 只能用 ADB + root 执行，设备现在在 Fastboot",
                            "error")
                        self.sig.log.emit("   请先让手机进系统并打开 USB 调试", "warn")
                    else:
                        self.sig.log.emit(
                            "❌ 未检测到设备（ADB / Fastboot 都没有）", "error")
                    try:
                        self._maint_check_drivers()
                    except Exception:
                        pass
                    self.sig.ui.emit(lambda m=op["title"]: self._maint_finish(
                        False, f"{m}失败：未检测到设备（请连好手机并打开 USB 调试）",
                        "err"))
                    return
                dev = ds[0]
                self.sig.log.emit(f"设备在 ADB：{dev}（走 root 命令）", "info")
                # 需要 root 的操作：先获取 / 校验 root，再执行
                if not self._maint_ensure_root(dev, op["title"]):
                    self.sig.ui.emit(lambda m=op["title"]: self._maint_finish(
                        False, f"{m}：需要 root 权限", "err"))
                    return
                steps = op["su_steps"]
                if full_wipe and op.get("su_steps_full"):
                    steps = op["su_steps_full"]
                    self.sig.log.emit("⚠ 已勾选「连内部存储一起清」", "warn")
                ok, msg = self._maint_run_su(dev, steps, op["title"])

            state = "ok" if ok else "err"
            # 有 verify 的操作（打开基带端口）：以电脑上真实出现的端口为准
            if ok and op.get("verify"):
                try:
                    vok, vtxt = self._maint_verify(op["verify"], before_ports)
                except Exception as e:
                    vok, vtxt = False, f"端口核对异常：{e}"
                if vok:
                    msg = f"{op['title']}成功：{vtxt}"
                else:
                    ok = False                  # 端口没出来 → 直接判失败
                    state = "err"
                    msg = f"{op['title']}失败：{vtxt}"

            self.sig.ui.emit(lambda o=ok, m=msg, st=state: self._maint_finish(
                o, m, st))
            if ok and op.get("reboot"):
                self.sig.ui.emit(self._maint_ask_reboot)
        except Exception as e:
            self.sig.log.emit(f"❌ {op['title']} 异常：{e}", "error")
            self.sig.log.emit(traceback.format_exc(), "error")
            self.sig.ui.emit(lambda m=op["title"]: self._maint_finish(
                False, f"{m} 异常", "err"))

    def _maint_ask_reboot(self):
        r = QMessageBox.question(
            self, "操作完成", "操作已完成，是否重启到系统？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.do_reboot("system")

