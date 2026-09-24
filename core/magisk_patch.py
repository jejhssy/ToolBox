# -*- coding: utf-8 -*-
"""Magisk 脱机修补核心逻辑（纯逻辑，无 UI）

移植自 VioletToolBox 的 offline patch 思路：
  · 本地资源（面具 APK / 镜像），不联网
  · magiskboot 解包 → 注入 ramdisk → 重打包
  · 官方规则判定该刷哪个分区（find_boot_image）
"""
import hashlib
import os
import subprocess
import shutil
import zipfile
from pathlib import Path

from core.config import OUT_DIR, WIN


# ==================== 工具查找 ====================
def _find_tool(name):
    """查找外部工具：PATH → 项目根 → tools/"""
    found = shutil.which(name)
    if found:
        return found
    here = Path(__file__).resolve().parent.parent
    for d in (here, here / "tools", Path.cwd(), Path.cwd() / "tools"):
        for ext in ("", ".exe"):
            f = d / f"{name}{ext}"
            if f.exists():
                return str(f)
    return None


def _find_apk(keyword):
    """查找本地 APK（项目根 / tools/，不联网）"""
    here = Path(__file__).resolve().parent.parent
    for d in (here, here / "tools", Path.cwd(), Path.cwd() / "tools"):
        if not d.is_dir():
            continue
        for f in d.glob("*.apk"):
            if keyword.lower() in f.name.lower():
                return str(f)
    return None

def _run_in(cmd, cwd=None, timeout=120, env=None):
    try:
        kw = {}
        if WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd,
                           env=env, **kw)
        out = (p.stdout or b"") + (p.stderr or b"")
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return p.returncode, out.decode(enc, errors="replace").strip()
            except Exception:
                continue
        return p.returncode, out.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return -1, f"未找到 {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, f"命令超时：{' '.join(cmd)}"
    except Exception as e:
        return -3, f"命令失败：{e}"


# ==================== 面具文件提取 ====================
# 官方 assets/boot_patch.sh 需要的原始文件（APK 里都是【未压缩】的）：
#   lib/<abi>/libmagiskinit.so -> magiskinit（原样，作为 ramdisk 的 init）
#   lib/<abi>/libmagisk.so     -> magisk     （稍后 compress=xz 成 magisk.xz）
#   lib/<abi>/libinit-ld.so    -> init-ld    （稍后 compress=xz 成 init-ld.xz）
#   assets/stub.apk            -> stub.apk   （稍后 compress=xz 成 stub.xz）
def _extract_magisk_assets(apk_path, work, log_cb):
    """从面具 APK 提取修补所需原始文件；返回 {名字: 路径}，缺关键文件返回 None"""
    result = {}
    try:
        with zipfile.ZipFile(apk_path) as apk:
            names = set(apk.namelist())

            def lib_path(fname):
                for abi in ("arm64-v8a", "armeabi-v7a"):
                    if f"lib/{abi}/{fname}" in names:
                        return f"lib/{abi}/{fname}"
                return None

            # stub.apk：Magisk v26+ 放在 assets/；老版本叫 libstub.so
            stub_src = ("assets/stub.apk" if "assets/stub.apk" in names
                        else lib_path("libstub.so"))
            want = [
                ("magiskinit", lib_path("libmagiskinit.so"), "magiskinit"),
                ("magisk", lib_path("libmagisk.so"), "magisk"),
                ("init-ld", lib_path("libinit-ld.so"), "init-ld"),
                ("stub.apk", stub_src, "stub.apk"),
            ]
            for key, src, dstname in want:
                if not src:
                    continue
                with apk.open(src) as srcf, open(work / dstname, "wb") as dstf:
                    shutil.copyfileobj(srcf, dstf)
                result[key] = work / dstname
                log_cb(f"  ✓ {dstname} ← {src}", "plain")

        if "magiskinit" not in result or "magisk" not in result:
            log_cb("❌ APK 中缺少 magiskinit / magisk，无法修补", "error")
            return None
        if "init-ld" not in result:
            log_cb("⚠ APK 中缺少 init-ld（Magisk v26.4+ 必需，会导致 root 不生效）", "warn")
        if "stub.apk" not in result:
            log_cb("⚠ APK 中缺少 stub.apk（不影响 root，但面具 App 无法自恢复）", "warn")
        return result
    except Exception as e:
        log_cb(f"❌ 解压 APK 失败：{e}", "error")
        return None


# ==================== 镜像类型识别 / 工具能力探测 ====================
# Android 13+（GKI）机型：ramdisk 在 init_boot.img（或部分机型的 vendor_boot.img）里，
# 只刷 boot 分区会出现无限重启，所以必须识别镜像类型并刷到同名分区。
BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"


def _peek_boot_image(path):
    """直接读镜像头部判断类型（不依赖 magiskboot，速度快）

    返回 {"kind", "kernel_sz", "ramdisk_sz", "header_ver", "desc"}
      kind: boot / init_boot / vendor_boot / None
    """
    info = {"kind": None, "kernel_sz": -1, "ramdisk_sz": -1,
            "header_ver": -1, "desc": "无法识别"}
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except Exception as e:
        info["desc"] = f"读取失败：{e}"
        return info

    if head[:8] == VENDOR_BOOT_MAGIC:
        info["kind"] = "vendor_boot"
        info["desc"] = "vendor_boot 镜像（ramdisk 在 vendor_ramdisk/ 内）"
        return info
    if head[:8] != BOOT_MAGIC:
        info["desc"] = "不是 Android boot 镜像（magic 不匹配）"
        return info

    kernel_sz = int.from_bytes(head[8:12], "little")
    ramdisk_sz = int.from_bytes(head[12:16], "little")
    hdr_ver = int.from_bytes(head[40:44], "little")
    if hdr_ver not in (0, 1, 2, 3, 4):
        hdr_ver = -1
    info["kernel_sz"] = kernel_sz
    info["ramdisk_sz"] = ramdisk_sz
    info["header_ver"] = hdr_ver

    if kernel_sz == 0 and ramdisk_sz > 0:
        info["kind"] = "init_boot"
        info["desc"] = (f"init_boot 镜像（无内核，ramdisk {ramdisk_sz} 字节）"
                        f" → 必须刷入 init_boot 分区")
    elif ramdisk_sz == 0:
        info["desc"] = ("该镜像没有 ramdisk（Android 13+ GKI 的 boot.img 正常如此，"
                        "ramdisk 在 init_boot.img / vendor_boot.img 里）")
    else:
        info["kind"] = "boot"
        info["desc"] = (f"boot 镜像（内核 {kernel_sz} 字节，ramdisk {ramdisk_sz} 字节）"
                        f" → 刷入 boot 分区")
    return info


def _tool_supports_vendor_boot(magiskboot_path):
    """探测 magiskboot 是否支持 vendor_boot（v27+ 才会创建 vendor_ramdisk/）"""
    try:
        with open(magiskboot_path, "rb") as f:
            return b"vendor_ramdisk" in f.read()
    except Exception:
        return False


def _apk_supports_vendor_boot(apk_path):
    """探测面具 APK 里的 magiskinit 是否支持 VENDORBOOT（v27+）"""
    try:
        with zipfile.ZipFile(apk_path) as apk:
            names = set(apk.namelist())
            for abi in ("arm64-v8a", "armeabi-v7a"):
                n = f"lib/{abi}/libmagiskinit.so"
                if n in names:
                    return b"VENDORBOOT" in apk.read(n)
    except Exception:
        pass
    return False


def _read_preinit_device(patched_img, magiskboot, log_cb):
    """从「官方 App 修补过的镜像」里读出 PREINITDEVICE（写在 .backup/.magisk）

    PC 端无法探测该值（官方 App 在手机上才能算出来），但如果用户手上有
    同一机型官方 App 修补过的镜像，就可以直接继承，避免开机异常。
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="magisk_ref_"))
    try:
        rc, out = _run_in([magiskboot, "unpack", str(Path(patched_img).resolve())],
                          cwd=str(tmp), timeout=180)
        if rc != 0:
            log_cb(f"⚠ 参考镜像解包失败，跳过 PREINITDEVICE 继承", "warn")
            return ""
        cpio = None
        for cand in (tmp / "ramdisk.cpio",
                     tmp / "vendor_ramdisk" / "init_boot.cpio",
                     tmp / "vendor_ramdisk" / "ramdisk.cpio"):
            if cand.exists() and cand.stat().st_size > 0:
                cpio = cand
                break
        if cpio is None:
            log_cb("⚠ 参考镜像里没有 ramdisk，跳过 PREINITDEVICE 继承", "warn")
            return ""
        rel = str(cpio.relative_to(tmp)).replace("\\", "/")
        rc, out = _run_in([magiskboot, "cpio", rel, "extract .backup/.magisk cfg_ref"],
                          cwd=str(tmp), timeout=120)
        cfg = tmp / "cfg_ref"
        if rc != 0 or not cfg.exists():
            log_cb("⚠ 参考镜像里没有 .backup/.magisk，跳过 PREINITDEVICE 继承", "warn")
            return ""
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, val = line.partition("=")
            if key.strip() == "PREINITDEVICE" and val.strip():
                log_cb(f"✓ 已从参考镜像继承 PREINITDEVICE={val.strip()}", "ok")
                return val.strip()
        log_cb("⚠ 参考镜像里没有 PREINITDEVICE 项", "warn")
        return ""
    except Exception as e:
        log_cb(f"⚠ 读取参考镜像失败：{e}", "warn")
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _magisk_patch_worker(img_path, apk_path, magiskboot, log_cb, prog_cb=None,
                         info=None, ref_img=None, avb=None):
    """按 Magisk 官方 assets/boot_patch.sh 的流程修补 boot 镜像

    返回修补后的镜像路径；失败返回 None。
    """
    import hashlib

    if info is None:
        info = {}
    peek = _peek_boot_image(img_path)
    info.update(peek)
    if peek["kind"] == "vendor_boot" and not (
            _tool_supports_vendor_boot(magiskboot)
            and _apk_supports_vendor_boot(apk_path)):
        log_cb("❌ 这是 vendor_boot 镜像：当前 tools/ 里的 magiskboot.exe / Magisk.apk "
               "版本过旧，不支持 vendor_boot 修补。", "error")
        log_cb("   → 请到 Magisk 官方 GitHub 下载 v27.0 以上版本的 magiskboot.exe 与 "
               "Magisk.apk，覆盖 tools/ 目录后重试", "warn")
        return None
    log_cb(f"镜像识别：{peek['desc']}", "info")

    out_dir = Path(OUT_DIR) / "magisk_patched"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_name = Path(img_path).stem
    patched_path = (out_dir / f"{img_name}_patched.img").resolve()

    work = out_dir / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # ---- 1. 从 APK 提取面具原始文件 ----
    log_cb("解压 APK 提取面具文件…", "info")
    assets = _extract_magisk_assets(apk_path, work, log_cb)
    if not assets:
        return None
    if prog_cb: prog_cb(15)

    # ---- 2. 解包 boot ----
    img_abs = str(Path(img_path).resolve())
    unpack_dir = out_dir / "unpack"
    if unpack_dir.exists():
        shutil.rmtree(unpack_dir, ignore_errors=True)
    unpack_dir.mkdir(parents=True, exist_ok=True)

    log_cb("解包 boot 镜像…", "info")
    rc, out = _run_in([magiskboot, "unpack", img_abs],
                      cwd=str(unpack_dir), timeout=180)
    vendor_boot = False
    if rc == 3:
        vendor_boot = True
    elif rc == 2:
        log_cb("⚠ 这是 ChromeOS 镜像，本工具不支持其签名流程", "warn")
        return None
    elif rc != 0:
        if rc == 1:
            log_cb("❌ 不支持的镜像格式（不是 boot.img / init_boot.img / vendor_boot.img？）",
                   "error")
        else:
            log_cb(f"❌ 解包失败：{out}", "error")
        return None
    log_cb("✓ 检测到 vendor_boot 镜像（ramdisk 在 vendor_ramdisk/ 内）"
           if vendor_boot else "✓ 解包完成", "ok")
    info["vendor_boot"] = vendor_boot

    # magiskboot 打印的头部信息（二次确认镜像类型）
    def _hdr(field):
        for line in out.splitlines():
            if line.strip().startswith(field):
                seg = line.split("[", 1)
                if len(seg) > 1:
                    val = seg[1].split("]", 1)[0].strip()
                    if val.isdigit():
                        return int(val)
        return None

    ksz, rsz = _hdr("KERNEL_SZ"), _hdr("RAMDISK_SZ")
    info["mb_kernel_sz"], info["mb_ramdisk_sz"] = ksz, rsz

    # 找 ramdisk：官方新版 boot_patch.sh 的查找顺序
    ramdisk = None
    for cand in (unpack_dir / "ramdisk.cpio",
                 unpack_dir / "vendor_ramdisk" / "init_boot.cpio",
                 unpack_dir / "vendor_ramdisk" / "ramdisk.cpio"):
        if cand.exists() and cand.stat().st_size > 0:
            ramdisk = cand
            break
    if ramdisk is None:
        log_cb("❌ 这个镜像里没有可用 ramdisk，修补它不会有 root，还可能无法开机。",
               "error")
        log_cb("   · Android 13+（GKI）机型：ramdisk 多在 init_boot.img → 请改选 init_boot.img",
               "warn")
        log_cb("   · 部分 Android 13+ 机型：ramdisk 在 vendor_boot.img → 请改选 vendor_boot.img"
               "（需把 tools/ 更新到 Magisk v27+）", "warn")
        log_cb("   · 确实无 ramdisk 的老机型（旧三星等）请用官方 Magisk App 在手机上修补",
               "warn")
        return None
    rd_arg = str(ramdisk.relative_to(unpack_dir)).replace("\\", "/")
    log_cb(f"✓ ramdisk：{rd_arg}", "ok")

    # 以 magiskboot 的头部信息为准判定镜像类型（决定该刷哪个分区）
    if vendor_boot:
        info["kind"] = "vendor_boot"
    elif ksz is not None:
        info["kind"] = "init_boot" if ksz == 0 else "boot"
    if info.get("kind") and peek["kind"] and info["kind"] != peek["kind"]:
        log_cb(f"⚠ 头部识别({peek['kind']}) 与 magiskboot({info['kind']}) 不一致，"
               f"以 magiskboot 为准", "warn")

    # ---- 3. 压缩 magisk / stub / init-ld 为 xz（官方 compress=xz）----
    log_cb("压缩面具文件为 xz…", "info")
    xz_files = []
    for key, dstname in (("magisk", "magisk.xz"),
                         ("stub.apk", "stub.xz"),
                         ("init-ld", "init-ld.xz")):
        src = assets.get(key)
        if src is None:
            continue
        dst = unpack_dir / dstname
        if dst.exists():
            dst.unlink()
        rc, out = _run_in([magiskboot, "compress=xz", str(src.resolve()), dstname],
                          cwd=str(unpack_dir), timeout=180)
        head = dst.read_bytes()[:6] if dst.exists() else b""
        if rc != 0 or head != b"\xfd7zXZ\x00":
            log_cb(f"❌ {dstname} 压缩失败：{out}", "error")
            return None
        log_cb(f"  ✓ {dstname}（xz，{dst.stat().st_size:,} 字节）", "plain")
        xz_files.append(dstname)
    if "stub.xz" not in xz_files:
        log_cb("⚠ 未生成 stub.xz（不影响 root，但面具 App 无法自恢复）", "warn")
    if prog_cb: prog_cb(45)

    # ---- 4. 判断 ramdisk 状态（官方：magiskboot cpio test）----
    #   返回值按位：0x1 已由 Magisk 修补  0x2 被其它工具改过  0x4 Sony
    env = os.environ.copy()
    env["KEEPVERITY"] = "true"
    env["KEEPFORCEENCRYPT"] = "true"

    rc, out = _run_in([magiskboot, "cpio", rd_arg, "test"],
                      cwd=str(unpack_dir), timeout=180)
    sha1 = ""
    preinit = ""
    if rc & 0x2:
        log_cb("❌ 该镜像被其它工具（非官方 Magisk）修改过，无法安全修补。", "error")
        log_cb("   请先刷回原厂 boot.img / init_boot.img，再重新修补。", "warn")
        return None
    if rc & 0x1:
        log_cb("检测到镜像已被 Magisk 修补过，先恢复原厂 ramdisk …", "warn")
        rc2, out2 = _run_in(
            [magiskboot, "cpio", rd_arg,
             "extract .backup/.magisk config.orig", "restore"],
            cwd=str(unpack_dir), timeout=180)
        cfg_orig = unpack_dir / "config.orig"
        if cfg_orig.exists():
            for line in cfg_orig.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                key, _, val = line.partition("=")
                if key.strip() == "SHA1":
                    sha1 = val.strip()
                elif key.strip() == "PREINITDEVICE":
                    preinit = val.strip()
            try:
                cfg_orig.unlink()
            except Exception:
                pass
        if rc2 != 0:
            log_cb("⚠ 恢复原厂 ramdisk 失败（镜像里没有官方备份）", "warn")
            log_cb("   将在现有 ramdisk 上直接覆盖修补，建议改用原厂镜像重新修补", "warn")
    else:
        log_cb("✓ 原厂镜像（未修补过）", "ok")

    if not sha1:
        sha1 = hashlib.sha1(Path(img_path).read_bytes()).hexdigest()
    log_cb(f"SHA1 = {sha1}", "plain")

    # PC 端探测不到 PREINITDEVICE（官方 App 在手机上才能算出），
    # 若用户提供了官方 App 修补过的参考镜像，就从它继承，避免开机异常。
    if not preinit and ref_img and Path(ref_img).is_file():
        preinit = _read_preinit_device(ref_img, magiskboot, log_cb)
    if not preinit:
        log_cb("⚠ 未能获取 PREINITDEVICE（PC 端无法探测）。"
               "若刷入后无法开机，请改用手机上的官方 Magisk App 修补。", "warn")

    # 官方：cp -af ramdisk.cpio ramdisk.cpio.orig（供 backup 命令使用）
    shutil.copy2(ramdisk, unpack_dir / "ramdisk.cpio.orig")

    # ---- 5. 写 config（字段与官方 boot_patch.sh 一致）----
    config_path = unpack_dir / "config"
    cfg_lines = [
        "KEEPVERITY=true",
        "KEEPFORCEENCRYPT=true",
        "RECOVERYMODE=false",
    ]
    if vendor_boot:
        # 官方新版会写这一项，magiskinit 据此决定从 vendor_ramdisk 启动
        cfg_lines.append("VENDORBOOT=true")
    if preinit:
        cfg_lines.append(f"PREINITDEVICE={preinit}")
    cfg_lines.append(f"SHA1={sha1}")
    # 必须用 LF：官方产物的 config 是 LF，Windows 下若写成 CRLF，
    # magiskinit 读到的值会带 \r（如 "metadata\r"），会导致启动异常。
    with open(config_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(cfg_lines) + "\n")
    log_cb(f"✓ 已写 config（{len(cfg_lines)} 项）", "ok")

    # ---- 6. cpio 注入（命令与顺序对齐官方）----
    # 注意：INFILE 必须用绝对路径，magiskboot 的工作目录是 unpack_dir
    log_cb("注入 Magisk 到 ramdisk…", "info")
    cpio_cmds = [
        "add 0750 init " + str(assets["magiskinit"].resolve()),
        "mkdir 0750 overlay.d",
        "mkdir 0750 overlay.d/sbin",
    ]
    for dstname in xz_files:
        cpio_cmds.append(f"add 0644 overlay.d/sbin/{dstname} {dstname}")
    cpio_cmds += [
        "patch",
        "backup ramdisk.cpio.orig",
        "mkdir 000 .backup",
        "add 000 .backup/.magisk config",
    ]

    rc, out = _run_in([magiskboot, "cpio", rd_arg] + cpio_cmds,
                      cwd=str(unpack_dir), timeout=180, env=env)
    if rc != 0:
        log_cb(f"❌ ramdisk 注入失败：{out}", "error")
        return None
    log_cb("✓ ramdisk 注入完成", "ok")
    if prog_cb: prog_cb(70)

    # ---- 6.1 统一原厂 init 备份格式（官方产物为 .backup/init.xz）----
    # 部分平台的 magiskboot 会把原厂 init 备份成未压缩的 .backup/init，
    # 这里压缩成 .backup/init.xz：既与官方一致，也避免 ramdisk 变大后
    # 某些机型镜像超出 boot 分区而刷不进去。此步骤失败不影响 root。
    bk_raw = work / "backup_init"
    rc, out = _run_in([magiskboot, "cpio", rd_arg,
                       "extract .backup/init " + str(bk_raw.resolve())],
                      cwd=str(unpack_dir), timeout=180)
    if rc == 0 and bk_raw.exists() and bk_raw.stat().st_size > 0:
        bk_xz = work / "init_backup.xz"
        if bk_xz.exists():
            bk_xz.unlink()
        rc2, out2 = _run_in([magiskboot, "compress=xz", str(bk_raw.resolve()),
                             str(bk_xz.resolve())],
                            cwd=str(unpack_dir), timeout=180)
        head = bk_xz.read_bytes()[:6] if bk_xz.exists() else b""
        if rc2 == 0 and head == b"\xfd7zXZ\x00":
            rc3, out3 = _run_in(
                [magiskboot, "cpio", rd_arg,
                 "add 000 .backup/init.xz " + str(bk_xz.resolve()),
                 "rm .backup/init"],
                cwd=str(unpack_dir), timeout=180)
            if rc3 == 0:
                log_cb("  ✓ 原厂 init 已备份为 .backup/init.xz（与官方一致）", "plain")
            else:
                log_cb(f"  ⚠ 整理 .backup/init 失败（不影响 root）：{out3}", "warn")
        else:
            log_cb("  ⚠ 压缩原厂 init 备份失败（不影响 root）", "warn")

    # ---- 7. dtb / extra 里的 fstab 修补（官方 dtb test / patch）----
    for dt in ("dtb", "kernel_dtb", "extra"):
        if not (unpack_dir / dt).exists():
            continue
        rct, outt = _run_in([magiskboot, "dtb", dt, "test"],
                            cwd=str(unpack_dir), timeout=120)
        if rct != 0:
            log_cb(f"⚠ {dt} 已被旧版（不受支持的）Magisk 改过，"
                   "建议改用原厂镜像重新修补", "warn")
        rcp, outp = _run_in([magiskboot, "dtb", dt, "patch"],
                            cwd=str(unpack_dir), timeout=120, env=env)
        if rcp == 0:
            log_cb(f"  ✓ 已修补 {dt} 中的 fstab（去除 verity / avb）", "plain")

    # ---- 8. kernel 三星保护 hexpatch（官方行为）----
    kernel = unpack_dir / "kernel"
    if kernel.exists():
        kernel_patched = False
        hexpatches = (
            # Remove Samsung RKP
            ("49010054011440B93FA00F71E9000054010840B93FA00F7189000054001840B91FA00F7188010054",
             "A1020054011440B93FA00F7140020054010840B93FA00F71E0010054001840B91FA00F7181010054"),
            # Remove Samsung defex
            ("821B8012", "E2FF8F12"),
            # Disable Samsung PROCA
            ("70726F63615F636F6E66696700", "70726F63615F6D616769736B00"),
        )
        for pat, rep in hexpatches:
            rch, _ = _run_in([magiskboot, "hexpatch", "kernel", pat, rep],
                             cwd=str(unpack_dir), timeout=120)
            if rch == 0:
                kernel_patched = True
        if kernel_patched:
            log_cb("  ✓ 已移除三星内核保护（RKP / defex / PROCA）", "plain")
        else:
            # 官方：内核无需改动时删掉解压产物，避免部分机型重打包后 bootloop
            try:
                kernel.unlink()
            except Exception:
                pass

    # ---- 9. 重打包（不指定输出，生成 new-boot.img）----
    log_cb("重打包 boot 镜像…", "info")
    new_boot = unpack_dir / "new-boot.img"
    if new_boot.exists():
        new_boot.unlink()

    rc, out = _run_in([magiskboot, "repack", img_abs],
                      cwd=str(unpack_dir), timeout=180, env=env)
    if rc != 0 or not new_boot.exists():
        log_cb(f"❌ 重打包失败：{out}", "error")
        return None

    shutil.copy2(new_boot, patched_path)

    # ---- 9.1 AVB：重建 vbmeta 摘要并重签名（严格校验机型必须，否则无法开机）----
    if avb and (avb.get("sign") or avb.get("disable")):
        try:
            from core.avb import (AvbParser, AvbRebuilder, AvbRebuildOptions,
                                  FLAG_HASHTREE_DISABLED,
                                  FLAG_VERIFICATION_DISABLED)
            if AvbParser.parse_image(str(patched_path)) is None:
                log_cb("⚠ 该镜像没有内嵌 vbmeta，跳过 AVB 重建", "warn")
            else:
                key_pem = ""
                if avb.get("sign"):
                    key_path = (avb.get("key_path") or "").strip()
                    if not key_path or not Path(key_path).is_file():
                        log_cb("❌ 勾选了「重建签名」但未指定有效的 PEM 私钥", "error")
                        return None
                    key_pem = Path(key_path).read_text(encoding="utf-8",
                                                       errors="replace")
                opts = AvbRebuildOptions(key_pem=key_pem)
                if avb.get("disable"):
                    opts.set_flags = (FLAG_HASHTREE_DISABLED
                                      | FLAG_VERIFICATION_DISABLED)
                log_cb("重建 AVB（vbmeta 摘要"
                       + (" + 重新签名" if key_pem else " + 置位禁用校验标志") + "）…",
                       "info")
                AvbRebuilder(log_cb).rebuild_and_sign(
                    str(patched_path), opts, str(patched_path))
        except Exception as e:
            log_cb(f"❌ AVB 重建失败：{e}", "error")
            return None

    # ---- 10. 清理中间文件（官方：rm -f ramdisk.cpio.orig config *.xz）----
    for name in ("ramdisk.cpio.orig", "config",
                 "magisk.xz", "stub.xz", "init-ld.xz"):
        try:
            (unpack_dir / name).unlink()
        except Exception:
            pass

    log_cb(f"✓ 已生成：{patched_path}", "ok")
    if prog_cb: prog_cb(100)
    return patched_path


# ============================================================
# 对外接口（供 UI 调用）
# ============================================================
def find_magiskboot():
    """查找 magiskboot（PATH → 项目根 → tools/）"""
    return _find_tool("magiskboot")


def find_manager_apk(keyword="magisk"):
    """在本地查找面具 APK（不联网）"""
    return _find_apk(keyword)


def pick_boot_partition(android="", kernel="", partitions=(), is_ab=False):
    """官方 util_functions.sh 的 find_boot_image() 规则 → 该修补/刷入哪个分区

    返回 (target, reason)
    """
    names = set()
    for p in partitions or ():
        names.add(p[:-2] if str(p).endswith(("_a", "_b")) else str(p))
    major = 0
    try:
        major = int(str(kernel).split("-")[0].split(".")[0])
    except Exception:
        pass
    if "init_boot" in names and major >= 5 \
            and "android12-" not in str(kernel) \
            and not str(kernel).startswith("5.4"):
        return "init_boot", "存在 init_boot 分区且内核 ≥5.x（官方规则 → init_boot）"
    if "boot" in names:
        return "boot", "ramdisk 在 boot 分区"
    if "vendor_boot" in names:
        return "vendor_boot", "无 boot 分区，ramdisk 在 vendor_boot（需 Magisk v27+ 工具）"
    if "init_boot" in names:
        return "init_boot", "存在 init_boot 分区"
    return "", "无法判定（未读到分区表）"


# 对外别名
patched_worker = _magisk_patch_worker
peek_boot_image = _peek_boot_image


