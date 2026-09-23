# -*- coding: utf-8 -*-
"""ADB / Fastboot 命令封装、设备信息读取"""
import re, subprocess, platform, threading
from core.config import WIN


def sh(cmd, timeout=30):
    try:
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
        out = (p.stdout or b"") + (p.stderr or b"")
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return p.returncode, out.decode(enc, errors="replace").strip()
            except Exception:
                continue
        return p.returncode, out.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return -1, f"未找到 {cmd[0]}（请安装并加入 PATH）"
    except subprocess.TimeoutExpired:
        return -2, f"命令超时：{' '.join(cmd)}"
    except Exception as e:
        return -3, f"命令失败：{e}"


def sh_stream(cmd, on_text=None, timeout=1800):
    """实时读取子进程输出（含 \r 进度行），逐行回调 on_text(line)"""
    try:
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, **kw)
    except FileNotFoundError:
        return -1, f"未找到 {cmd[0]}（请安装并加入 PATH）"
    except Exception as e:
        return -3, f"命令失败：{e}"

    flag = {"timeout": False}

    def _watchdog():
        try:
            p.wait(timeout=timeout)
        except Exception:
            flag["timeout"] = True
            try:
                p.kill()
            except Exception:
                pass

    threading.Thread(target=_watchdog, daemon=True).start()

    buf = []
    try:
        for line in p.stdout:
            line = line.rstrip("\r\n")
            buf.append(line)
            if on_text:
                try:
                    on_text(line)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass

    if flag["timeout"]:
        return -2, f"命令超时：{' '.join(cmd)}"
    return (p.returncode if p.returncode is not None else -1), "\n".join(buf)


def adb_dev():
    rc, out = sh(["adb", "devices"])
    if rc != 0:
        return [], out
    ds = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        p = line.split()
        if len(p) >= 2 and p[1] == "device":
            ds.append(p[0])
    return ds, out


def fb_dev():
    rc, out = sh(["fastboot", "devices"])
    if rc != 0:
        return [], out
    return [l.strip().split()[0] for l in out.splitlines() if l.strip()], out


def _host_os():
    try:
        if platform.system() == "Windows":
            v = platform.version() or ""
            if v.startswith("10.0.22") or v.startswith("10.0.23") or v.startswith("10.0.26"):
                return "Windows 11"
            return f"Windows {platform.release()}"
        return f"{platform.system()} {platform.release()}".strip()
    except Exception:
        return "—"


def _pretty_soc_vendor(v):
    if not v:
        return v
    s = str(v).strip()
    low = s.lower()
    if not low or low == "—":
        return s
    if "mediatek" in low or low == "mtk" or low.startswith("mt"):
        return "联发科"
    if low in ("qti", "qualcomm", "qcom") or "qualcomm" in low or "qti" in low:
        return "高通骁龙"
    if "samsung" in low or low == "exynos":
        return "三星"
    if "hisilicon" in low or "kirin" in low:
        return "海思"
    if "unisoc" in low or "spreadtrum" in low:
        return "紫光展锐"
    if "nvidia" in low or "tegra" in low:
        return "英伟达"
    return s


def read_device_info():
    info = {
        "state": "未连接", "mode": "—",
        "serial": "—", "model": "—", "device": "—", "android": "—",
        "unlock": "—", "incremental": "—", "kernel": "—", "build_date": "—",
        "slot": "—", "soc_vendor": "—", "platform": "—", "soc_model": "—",
        "host_os": _host_os(), "selinux": "—",
    }
    ds, _ = adb_dev()
    if ds:
        dev = ds[0]
        info["state"] = "已连接"
        info["mode"] = "系统"

        rc, out = sh(["adb", "-s", dev, "shell", "getprop"], timeout=10)
        props = {}
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"^\[([^\]]+)\]:\s*\[([^\]]*)\]\s*$", line.strip())
                if m:
                    props[m.group(1)] = m.group(2)

        def g(*keys, default="—"):
            for k in keys:
                v = (props.get(k) or "").strip()
                if v:
                    return v
            return default

        info["serial"]      = g("ro.serialno", "ro.boot.serialno")
        info["model"]       = g("ro.product.model", "ro.product.vendor.model", "ro.product.system.model")
        info["device"]      = g("ro.product.device", "ro.product.vendor.device", "ro.product.system.device")
        info["android"]     = g("ro.build.version.release")
        info["incremental"] = g("ro.build.version.incremental")
        info["build_date"]  = g("ro.build.date")
        info["soc_vendor"]  = _pretty_soc_vendor(g("ro.soc.manufacturer", "ro.board.platform"))
        info["platform"]    = g("ro.board.platform", "ro.soc.platform")
        info["soc_model"]   = g("ro.soc.model", "ro.product.board", "ro.hardware")

        slot = g("ro.boot.slot_suffix", default="").strip("_")
        if slot:
            info["slot"] = f"{slot.upper()}槽位"

        locked = g("ro.boot.flash.locked", default="")
        vb     = g("ro.boot.verifiedbootstate", default="")
        if locked == "1":
            info["unlock"] = "已锁定"
        elif locked == "0":
            info["unlock"] = "已解锁"
        elif vb:
            info["unlock"] = {"green": "已锁定", "orange": "已解锁",
                              "yellow": "自定义"}.get(vb.lower(), vb)

        rc2, se = sh(["adb", "-s", dev, "shell", "getenforce"], timeout=5)
        se = (se or "").strip() if rc2 == 0 else ""
        if not se:
            se = g("ro.boot.selinux", default="").strip()
        se_low = se.lower()
        info["selinux"] = {"enforcing": "严格模式", "permissive": "宽容模式",
                           "disabled": "已禁用"}.get(se_low, se or "—")

        rc2, kr = sh(["adb", "-s", dev, "shell", "uname", "-r"], timeout=5)
        if rc2 == 0 and kr.strip():
            k = kr.strip()
            # 内核版本形如：5.10.236-android12-9-00003-gfb24cf99ab97-ab14313284
            # 只保留前两段，如：5.10.236-android12
            parts = k.split("-")
            if len(parts) > 2:
                k = "-".join(parts[:2])
            info["kernel"] = k

        return info, "adb"

    fs, _ = fb_dev()
    if fs:
        info["state"] = "已连接"
        info["mode"] = "Fastboot"
        var_map = {"product": "model", "serialno": "serial"}
        for var, key in var_map.items():
            rc, out = sh(["fastboot", "getvar", var], timeout=3)
            if rc == 0 and out:
                for line in out.splitlines():
                    if ":" in line and not line.lower().startswith("finished"):
                        info[key] = line.split(":", 1)[1].strip()
                        break
        rc, out = sh(["fastboot", "getvar", "current-slot"], timeout=3)
        if rc == 0 and out:
            for line in out.splitlines():
                if ":" in line and not line.lower().startswith("finished"):
                    s = line.split(":", 1)[1].strip().strip("_")
                    if s and s != ":":
                        info["slot"] = f"{s.upper()}槽位"
                    break
        rc, out = sh(["fastboot", "getvar", "unlocked"], timeout=3)
        if rc == 0 and out:
            for line in out.splitlines():
                if ":" in line and not line.lower().startswith("finished"):
                    v = line.split(":", 1)[1].strip().lower()
                    if v in ("yes", "true", "1"):
                        info["unlock"] = "已解锁"
                    elif v in ("no", "false", "0"):
                        info["unlock"] = "已锁定"
                    break
        return info, "fastboot"

    return info, None
    

# ===================== 电量 / 存储 / 内存 =====================
def read_device_stats():
    """通过 ADB 读取电量、存储、内存信息"""
    out = {
        "battery_level": None, "battery_status": "—",
        "storage_used_pct": None, "storage_used": "—", "storage_total": "—",
        "mem_used_pct": None, "mem_used": "—", "mem_total": "—",
    }

    ds, _ = adb_dev()
    if not ds:
        return out
    dev = ds[0]

    def _fmt_size(kb):
        try:
            kb = float(kb)
        except Exception:
            return "—"
        gb = kb / 1024 / 1024
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{kb/1024:.0f} MB"

    # 电量
    try:
        rc, bat = sh(["adb", "-s", dev, "shell", "dumpsys", "battery"], timeout=6)
        if rc == 0 and bat:
            level = status = plugged = None
            for line in bat.splitlines():
                s = line.strip()
                if s.startswith("level:"):
                    try: level = int(s.split(":", 1)[1].strip())
                    except Exception: pass
                elif s.startswith("status:"):
                    try: status = int(s.split(":", 1)[1].strip())
                    except Exception: pass
                elif s.startswith("plugged:"):
                    try: plugged = int(s.split(":", 1)[1].strip())
                    except Exception: pass
            if level is not None:
                out["battery_level"] = max(0, min(100, level))
            status_map = {1: "未知", 2: "充电中", 3: "未充电",
                          4: "未充电", 5: "已充满"}
            txt = status_map.get(status, "—")
            if plugged and plugged != 0 and status == 3:
                txt = "已连接"
            out["battery_status"] = txt
    except Exception:
        pass

    # 存储（/data）
    try:
        rc, df = sh(["adb", "-s", dev, "shell", "df", "/data"], timeout=6)
        if rc == 0 and df:
            for line in df.splitlines():
                parts = line.split()
                if len(parts) >= 4 and "Filesystem" not in line:
                    try:
                        total_kb = int(parts[1])
                        used_kb = int(parts[2])
                    except Exception:
                        continue
                    if total_kb > 0:
                        out["storage_total"] = _fmt_size(total_kb)
                        out["storage_used"] = _fmt_size(used_kb)
                        out["storage_used_pct"] = round(used_kb * 100 / total_kb, 1)
                        break
    except Exception:
        pass

    # 内存
    try:
        rc, mi = sh(["adb", "-s", dev, "shell", "cat", "/proc/meminfo"], timeout=6)
        if rc == 0 and mi:
            total_kb = avail_kb = 0
            for line in mi.splitlines():
                if line.startswith("MemTotal:"):
                    try: total_kb = int(line.split()[1])
                    except Exception: pass
                elif line.startswith("MemAvailable:"):
                    try: avail_kb = int(line.split()[1])
                    except Exception: pass
            if total_kb > 0:
                used_kb = total_kb - avail_kb if avail_kb > 0 else 0
                out["mem_total"] = _fmt_size(total_kb)
                out["mem_used"] = _fmt_size(used_kb)
                if avail_kb > 0:
                    out["mem_used_pct"] = round(used_kb * 100 / total_kb, 1)
    except Exception:
        pass

    return out