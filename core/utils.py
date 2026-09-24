# -*- coding: utf-8 -*-
"""ADB / Fastboot 命令封装、设备信息读取"""
import re, shutil, subprocess, platform, tempfile, threading
from pathlib import Path
from core.config import WIN


# ==================== adb 本地/远端路径的已知坑 ====================
def shq(path):
    """把路径塞进 `adb shell <整串>` 时加引号

    设备端 shell 会按空格拆参数，带空格 / 中文的路径必须整体引起来，
    里面的单引号用 '\\'' 转义（POSIX 写法）。
    """
    return "'" + str(path).replace("'", "'\\''") + "'"


def hsize(n):
    """字节数转人话"""
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return "—"


def ascii_work_dir(final_path, prefix=".tb_tmp"):
    """找一个「路径纯 ASCII 且可写」的目录，用来放 adb 传输的临时结果

    adb 在**本地路径含中文**时建文件会失败（cannot create '…': Not a directory /
    Is a directory），所以先落到纯 ASCII 的临时名，再由本程序改名到最终位置。
    优先取最终路径所在磁盘上离它最近的纯 ASCII 目录 —— 同盘改名是瞬间完成的。
    """
    cands = []
    try:
        cur = Path(str(final_path)).parent
    except Exception:
        cur = None
    while cur is not None:
        cands.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    try:
        cands.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    for c in cands:
        try:
            if not str(c).isascii() or not c.is_dir():
                continue
            probe = c / f"{prefix}_probe"
            with open(probe, "w") as f:
                f.write("")
            probe.unlink()
            return c
        except Exception:
            continue
    try:
        return Path(tempfile.gettempdir())
    except Exception:
        return Path(".")


def has_nonascii_dirs(serial, remote, timeout=90):
    """远端子树里有没有「非 ASCII 目录名」（adb 在 Windows 上建不出这种本地目录）

    返回 None（拿不到清单）/ False（全 ASCII）/ True（有）
    """
    base = (["adb", "-s", serial] if serial else ["adb"])
    try:
        rc, out = sh(base + ["shell", f"find {shq(remote)} -type d -print"],
                     timeout=timeout)
    except Exception:
        return None
    if rc != 0:
        return None
    return any(l.strip() and not l.strip().isascii()
               for l in (out or "").splitlines())


def adb_pull_tree(serial, remote, target, work,
                  on_file=None, on_progress=None, cancel=None, log=None):
    """把远端目录拉到 target（本程序负责建本地目录）

    · 纯 ASCII 的顶层子项：一次 adb pull（快）
    · 含非 ASCII 目录名的子项：逐文件拉（单一文件的中文名 adb 能建，目录名不行）
    返回 (rc, 成功文件数, 失败文件数)，rc：0 全成功 / 1 有失败 / -100 已取消
    """
    base = (["adb", "-s", serial] if serial else ["adb"])
    remote = str(remote).rstrip("/")

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    rc, out = sh(base + ["shell", f"ls -1Ap {shq(remote)}"], timeout=60)
    if rc != 0:
        return rc, 0, 0
    children = []
    for line in (out or "").splitlines():
        n = line.strip().replace("\r", "")
        if not n or n.startswith("ls:") or n.startswith("adb:"):
            continue
        is_dir = n.endswith("/")
        children.append((n[:-1] if is_dir else n, is_dir))
    if not children:
        return 0, 0, 0

    total = len(children)
    ok = fail = 0
    for ci, (name, is_dir) in enumerate(children):
        if cancel is not None and cancel.is_set():
            return -100, ok, fail
        dst = Path(target) / name
        if not is_dir:
            tmp = Path(work) / f".tb_c_{ci}"
            rc2, _o = sh(base + ["pull", f"{remote}/{name}", str(tmp)], timeout=1800)
            if rc2 == 0 and move_into_place(tmp, dst):
                ok += 1
            else:
                fail += 1
                _log(f"  ↑ 跳过：{name}")
        elif not has_nonascii_dirs(serial, f"{remote}/{name}") is True:
            # 子树没有中文目录名 → 一次拉下来（快）
            tmp = Path(work) / f".tb_c_{ci}"
            rc2, _o = sh(base + ["pull", "-p", f"{remote}/{name}", str(tmp)],
                         timeout=3600)
            if rc2 == 0 and move_into_place(tmp, dst):
                ok += 1
            elif tmp.exists():
                move_into_place(tmp, dst)
                fail += 1
                _log(f"  ⚠ 子目录中途出错，已保留已传内容：{name}")
            else:
                fail += 1
                _log(f"  ↑ 跳过：{name}")
        else:
            # 子树里有中文目录名 → 逐文件
            _log(f"  ↳ 「{name}」里有非 ASCII 目录名，改为逐文件拉取")
            rc3, out3 = sh(base + ["shell",
                                   f"find {shq(f'{remote}/{name}')} -type f -print"],
                           timeout=180)
            files = [l.strip() for l in (out3 or "").splitlines() if l.strip()]
            for fi, rf in enumerate(files):
                if cancel is not None and cancel.is_set():
                    return -100, ok, fail
                rel = rf[len(f"{remote}/{name}"):].lstrip("/") or Path(rf).name
                if on_file:
                    try:
                        on_file(ci + 1, total, f"{name}/{rel}")
                    except Exception:
                        pass
                tmp = Path(work) / f".tb_c{ci}_f{fi}"
                rc4, _o = sh(base + ["pull", rf, str(tmp)], timeout=1800)
                if rc4 == 0 and tmp.exists():
                    d2 = dst.joinpath(*rel.split("/"))
                    try:
                        d2.parent.mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                    if move_into_place(tmp, d2):
                        ok += 1
                    else:
                        fail += 1
                else:
                    fail += 1
                    _log(f"  ↑ 跳过：{rf}")
        if on_progress:
            try:
                on_progress(int((ci + 1) * 100 / max(1, total)))
            except Exception:
                pass
    return (0 if fail == 0 else 1), ok, fail


def move_into_place(src, dst):
    """把临时传输结果改名 / 合并到最终位置（跨盘自动退化成复制）"""
    src, dst = Path(str(src)), Path(str(dst))
    try:
        if not src.exists():
            return False
        if not dst.exists():
            try:
                src.replace(dst)
            except OSError:
                shutil.move(str(src), str(dst))
            return True
        if src.is_dir() and dst.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            shutil.rmtree(str(src), ignore_errors=True)
            return True
        i = 1
        while i < 999:
            cand = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
            if not cand.exists():
                src.replace(cand)
                return True
            i += 1
    except Exception:
        pass
    return False



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
    """实时读取子进程输出（\r / \n 都作为分帧符），逐帧回调 on_text(text)"""
    import os
    try:
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, **kw)
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

    lines = []
    pending = bytearray()

    def emit_pending():
        if not pending:
            return
        try:
            s = bytes(pending).decode("utf-8", errors="replace")
        except Exception:
            s = ""
        pending.clear()
        if s:
            lines.append(s)
            if on_text:
                try:
                    on_text(s)
                except Exception:
                    pass

    try:
        while True:
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:
                break
            for b in chunk:
                if b in (10, 13):        # \n 或 \r
                    emit_pending()
                else:
                    pending.append(b)
    except Exception:
        pass
    emit_pending()

    try:
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass

    if flag["timeout"]:
        return -2, f"命令超时：{' '.join(cmd)}"
    return (p.returncode if p.returncode is not None else -1), "\n".join(lines)


def adb_list():
    """列出 adb 里的所有设备，返回 [(serial, state), ...]

    state 可能是：device / recovery / sideload / unauthorized / offline / bootloader
    （adb_dev() 只认 device，识别不出 recovery、sideload 这些特殊状态）
    """
    rc, out = sh(["adb", "devices"])
    if rc != 0:
        return [], out
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        p = line.split()
        if len(p) >= 2:
            rows.append((p[0], p[1]))
    return rows, out


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


# adb 的哪些状态可以直接用 adb shell / pull / push / reboot
# （Recovery 下 adb 服务照样在，命令能跑；Sideload 只有侧载通道，没有 shell）
ADB_SHELL_STATES = ("device", "recovery")


def adb_state():
    """当前 adb 状态：device / recovery / sideload / unauthorized / offline / none"""
    rows, _ = adb_list()
    for st in ("sideload", "recovery", "device", "unauthorized", "offline"):
        if any(s == st for _sn, s in rows):
            return st
    return "none"


def adb_shell_dev():
    """返回能用 adb shell / pull / push / reboot 的序列号（系统 或 Recovery 模式）

    Recovery（TWRP 等）下 adb reboot / adb shell / adb push 都是可用的，
    所以别再只认 device —— 否则手机在 Recovery 里点“重启到系统”会报“未检测到设备”。
    """
    rows, _ = adb_list()
    for st in ADB_SHELL_STATES:
        for sn, s in rows:
            if s == st:
                return sn
    return None


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


def _probe_adb_shell(serial):
    """device 状态下进一步分清是「系统」还是「Recovery」"""
    def g(prop):
        rc, out = sh(["adb", "-s", serial, "shell", "getprop", prop], timeout=8)
        return (out or "").strip().lower() if rc == 0 else ""

    bootmode = g("ro.bootmode") or g("ro.boot.bootmode") or g("ro.boot.mode")
    svc = g("init.svc.recovery")
    booted = g("sys.boot_completed")
    ramdisk = ""
    try:
        rc, out = sh(["adb", "-s", serial, "shell",
                      "test -e /sbin/recovery && echo REC_RAMDISK"], timeout=8)
        if rc == 0 and "REC_RAMDISK" in (out or ""):
            ramdisk = "有"
    except Exception:
        pass

    detail = (f"ro.bootmode={bootmode or '(空)'} · "
              f"init.svc.recovery={svc or '(空)'} · "
              f"sys.boot_completed={booted or '(空)'}")
    if ramdisk:
        detail += " · /sbin/recovery=存在"
    if ("recovery" in bootmode) or (svc == "running") or bool(ramdisk):
        return "recovery", detail
    return "system", detail


def detect_device_mode(fastboot_check=True):
    """判断设备当前处于哪种模式，返回 (mode, serial, detail)

    mode：none / system / recovery / sideload / fastboot / unauthorized / offline
    （adb 1.0.41+ 会直接把 recovery / sideload 写在状态列里；
      老版本只写 device，这里再用 getprop / /sbin/recovery 兜底判断）
    """
    rows, _ = adb_list()
    mode, serial = "none", None
    for st in ("sideload", "recovery", "unauthorized", "offline", "device"):
        for sn, s in rows:
            if s == st:
                mode, serial = st, sn
                break
        if serial:
            break

    if mode == "device":
        m, detail = _probe_adb_shell(serial)
        return m, serial, detail
    if mode == "none" and fastboot_check:
        try:
            fs, _ = fb_dev()
            if fs:
                return "fastboot", fs[0], ""
        except Exception:
            pass
    return mode, serial, ""


# 模式 → 展示名（主页「连接类型」和右上角都用这套叫法）
MODE_LABELS = {
    "none": "未连接", "system": "系统", "recovery": "Recovery",
    "sideload": "Sideload", "fastboot": "Fastboot",
    "unauthorized": "未授权", "offline": "离线",
}

# 模式 → 该模式下能做什么（右上角胶囊的 tooltip 用）
MODE_TIPS = {
    "none": "插好数据线，确认手机已开启 USB 调试，并在弹窗里点“允许”",
    "system": "可以导出相册、推送文件；要刷 OTA 先重启到 Recovery，"
              "在手机里选 “Apply update from ADB”",
    "recovery": "可以 Sideload 刷机 / 导出相册 / 推送文件；"
                "导出前建议先在手机 Recovery 里挂载 /sdcard",
    "sideload": "设备已就绪：在「🛠 Recovery 工具」里选好 OTA zip 直接开始侧载",
    "fastboot": "该模式下 ADB 功能（Sideload / 导出 / 推送）都用不了；"
                "要回系统请去「📦 常规镜像刷入」页点重启",
    "unauthorized": "请在手机屏幕上点“允许 USB 调试”（Recovery 下也会弹）",
    "offline": "重新插拔数据线；或在 CMD 里执行 adb kill-server 后重试",
}


def read_device_info():
    info = {
        "state": "未连接", "mode": "—",
        "serial": "—", "model": "—", "device": "—", "android": "—",
        "unlock": "—", "incremental": "—", "kernel": "—", "build_date": "—",
        "slot": "—", "soc_vendor": "—", "platform": "—", "soc_model": "—",
        "host_os": _host_os(), "selinux": "—",
    }
    mode, serial, _ = detect_device_mode(fastboot_check=False)
    if mode == "sideload":
        info["state"] = "已连接"
        info["mode"] = "Sideload（等待侧载包）"
        info["serial"] = serial or "—"
        return info, "adb"
    if mode in ("unauthorized", "offline"):
        info["state"] = "已连接（未就绪）"
        info["mode"] = MODE_LABELS.get(mode, "—")
        info["serial"] = serial or "—"
        return info, None
    ds = [serial] if (mode in ("system", "recovery") and serial) else []
    if ds:
        dev = ds[0]
        info["state"] = "已连接"
        info["mode"] = "系统" if mode == "system" else "Recovery"

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