# -*- coding: utf-8 -*-
"""常量、主题、配置"""
import sys

APP = "一键工具箱"
VER = "v20.0"
AUTHOR = "酷安 @coyuyu"
CREDIT = "Patch 逻辑：4pda·Max_Goblin / GitHub·Shocked-Cat"
SIZE_OK = 4 * 1024 * 1024
MAGIC = bytes.fromhex("41 4E 44 5F 52 4F 4D 49 4E 46 4F 5F 76")
OUT_DIR = "preloader_path"
WIN = sys.platform.startswith("win")

ZIP_MAGICS = (
    b"Rar!",
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd", b"MSCF",
)

THEMES = [
    {"bg":"#0b1020","panel":"#141b33","panel2":"#1a2344","line":"#2a3555",
     "text":"#e8ecf7","dim":"#8892b0","acc":"#4f8cff","acc2":"#6ea8ff",
     "ok":"#4ade80","warn":"#fbbf24","err":"#f87171","log":"#0a0f1e",
     "menu":"#0f1530","menuSel":"#4f8cff","menuTxt":"#c8d3ec"},
    {"bg":"#0f0a1e","panel":"#1c1333","panel2":"#251a44","line":"#3a2a5e",
     "text":"#f0eaff","dim":"#9a88b8","acc":"#a78bfa","acc2":"#c4b5fd",
     "ok":"#4ade80","warn":"#fbbf24","err":"#f87171","log":"#0e0820",
     "menu":"#160f2a","menuSel":"#a78bfa","menuTxt":"#d8caff"},
    {"bg":"#0a1a12","panel":"#132a1e","panel2":"#1a3628","line":"#2a4a38",
     "text":"#e8f5ee","dim":"#7a9a88","acc":"#34d399","acc2":"#5eead4",
     "ok":"#4ade80","warn":"#fbbf24","err":"#f87171","log":"#08160e",
     "menu":"#0d2018","menuSel":"#34d399","menuTxt":"#c8e8d8"},
    {"bg":"#1a0f0f","panel":"#2a1a1a","panel2":"#3a2222","line":"#5a2a2a",
     "text":"#f5e8e8","dim":"#a88888","acc":"#f87171","acc2":"#fca5a5",
     "ok":"#4ade80","warn":"#fbbf24","err":"#f87171","log":"#170c0c",
     "menu":"#1f1212","menuSel":"#f87171","menuTxt":"#e8c8c8"},
    {"bg":"#0a1620","panel":"#13202e","panel2":"#1a2a3c","line":"#28425a",
     "text":"#e0f0ff","dim":"#7a94a8","acc":"#38bdf8","acc2":"#7dd3fc",
     "ok":"#4ade80","warn":"#fbbf24","err":"#f87171","log":"#081018",
     "menu":"#0c1a26","menuSel":"#38bdf8","menuTxt":"#c8e0f0"},
         # ---------- 浅色主题 ----------
    # 6. 浅白蓝（清爽）
    {"bg":"#f5f7fb","panel":"#ffffff","panel2":"#eef2f9","line":"#d4dbe8",
     "text":"#1a2233","dim":"#68758c","acc":"#2563eb","acc2":"#3b82f6",
     "ok":"#16a34a","warn":"#d97706","err":"#dc2626","log":"#e8edf5",
     "menu":"#eef2f9","menuSel":"#2563eb","menuTxt":"#3a4458"},

    # 7. 浅暖米黄
    {"bg":"#faf7f2","panel":"#ffffff","panel2":"#f5efe6","line":"#e0d6c4",
     "text":"#2d2418","dim":"#807262","acc":"#c2410c","acc2":"#ea580c",
     "ok":"#16a34a","warn":"#ca8a04","err":"#dc2626","log":"#f2ebdf",
     "menu":"#f0e8da","menuSel":"#c2410c","menuTxt":"#4a3d2a"},

    # 8. 浅紫
    {"bg":"#f7f5fc","panel":"#ffffff","panel2":"#f0ecfa","line":"#ddd6f0",
     "text":"#241b3d","dim":"#6b6285","acc":"#7c3aed","acc2":"#8b5cf6",
     "ok":"#16a34a","warn":"#d97706","err":"#dc2626","log":"#eee9f7",
     "menu":"#f0ebfa","menuSel":"#7c3aed","menuTxt":"#4a3d68"},
]

FASTBOOTD_PARTS = {"init_boot", "vendor_boot", "super",
                   "system_ext", "vendor", "odm"}

SPECIAL_OPTS = {
    "vbmeta": ["--disable-verity", "--disable-verification"],
    "vbmeta_system": ["--disable-verity", "--disable-verification"],
}

FILE_KINDS = {
    "folder": ("📁", "#38bdf8"),
    "image":  ("🖼️", "#c084fc"),
    "video":  ("🎬", "#f472b6"),
    "audio":  ("🎵", "#4ade80"),
    "apk":    ("📦", "#fb923c"),
    "text":   ("📄", "#fbbf24"),
    "zip":    ("🗜️", "#a78bfa"),
    "img":    ("💿", "#60a5fa"),
    "file":   ("📎", "#94a3b8"),
}

CARD_W, CARD_H = 110, 120
CARD_GAP_X, CARD_GAP_Y = 10, 10
GRID_PADDING = 12