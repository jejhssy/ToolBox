# -*- coding: utf-8 -*-
"""纯 Python payload.bin 解析器 / 提取器

无外部依赖：
  · Protobuf 用手写 varint 解析（只识别我们需要的字段）
  · XZ / bzip2 用 Python 标准库 lzma / bz2
  · 支持 Android OTA 的 payload.bin（全量版 v1/v2）

用法：
    parser = PayloadParser("payload.bin")
    for name, size, size_str in parser.get_partition_info():
        print(name, size_str)

    extractor = PayloadExtractor("payload.bin", "out_dir",
                                 cancel_event=threading.Event(),
                                 log_cb=lambda m, lv="plain": print(m),
                                 prog_cb=lambda v: print(v))
    extractor.extract(["boot", "init_boot"])
"""
import bz2
import contextlib
import hashlib
import lzma
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# ============================================================
# Protobuf 线格式解析（只做需要的那部分）
# ============================================================
def _read_varint(data: bytes, pos: int):
    """读取 varint，返回 (value, new_pos)"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _parse_message(data: bytes) -> dict:
    """解析 protobuf 消息体 → {字段号: [值, ...]}
    值类型：
      wire_type 0 → int
      wire_type 1 → int（64位）
      wire_type 2 → bytes（可能是嵌套消息）
      wire_type 5 → int（32位）
    """
    result = {}
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _read_varint(data, pos)
        field_num = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:
            value, pos = _read_varint(data, pos)
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("64-bit 越界")
            value = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > n:
                raise ValueError("length-delimited 越界")
            value = data[pos:pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("32-bit 越界")
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        else:
            raise ValueError(f"未知 wire type {wire_type}")

        result.setdefault(field_num, []).append(value)
    return result


def _first(msg: dict, field_num: int, default=None):
    v = msg.get(field_num)
    return v[0] if v else default


# ============================================================
# 快扫：只取需要的字段，其余按长度跳过（清单可能有上百万个操作）
# ============================================================
def _scan_op_head(data: bytes, start: int, end: int):
    """InstallOperation → (type, data_length)"""
    pos = start
    typ = 0
    dlen = 0
    while pos < end:
        b = data[pos]
        pos += 1
        key = b & 0x7F
        shift = 7
        while b & 0x80:
            b = data[pos]
            pos += 1
            key |= (b & 0x7F) << shift
            shift += 7
        f = key >> 3
        wt = key & 7
        if wt == 0:
            val = 0
            shift = 0
            while True:
                b = data[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if f == 1:
                typ = val
            elif f == 3:
                dlen = val
        elif wt == 2:
            ln = 0
            shift = 0
            while True:
                b = data[pos]
                pos += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            pos += ln
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return typ, dlen


def _scan_op_extent_end(data: bytes, start: int, end: int) -> int:
    """InstallOperation 的 dst_extents(field 6) 里最大的 start_block+num_blocks"""
    pos = start
    best = 0
    while pos < end:
        b = data[pos]
        pos += 1
        key = b & 0x7F
        shift = 7
        while b & 0x80:
            b = data[pos]
            pos += 1
            key |= (b & 0x7F) << shift
            shift += 7
        f = key >> 3
        wt = key & 7
        if wt == 2:
            ln = 0
            shift = 0
            while True:
                b = data[pos]
                pos += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if f == 6:
                sub_end = pos + ln
                p2 = pos
                sb = 0
                nb = 0
                while p2 < sub_end:
                    b2 = data[p2]
                    p2 += 1
                    k2 = b2 & 0x7F
                    s2 = 7
                    while b2 & 0x80:
                        b2 = data[p2]
                        p2 += 1
                        k2 |= (b2 & 0x7F) << s2
                        s2 += 7
                    f2 = k2 >> 3
                    w2 = k2 & 7
                    if w2 == 0:
                        v2 = 0
                        s2 = 0
                        while True:
                            b2 = data[p2]
                            p2 += 1
                            v2 |= (b2 & 0x7F) << s2
                            if not (b2 & 0x80):
                                break
                            s2 += 7
                        if f2 == 1:
                            sb = v2
                        elif f2 == 2:
                            nb = v2
                    elif w2 == 2:
                        l2 = 0
                        s2 = 0
                        while True:
                            b2 = data[p2]
                            p2 += 1
                            l2 |= (b2 & 0x7F) << s2
                            if not (b2 & 0x80):
                                break
                            s2 += 7
                        p2 += l2
                    elif w2 == 5:
                        p2 += 4
                    elif w2 == 1:
                        p2 += 8
                    else:
                        break
                if sb + nb > best:
                    best = sb + nb
            pos += ln
        elif wt == 0:
            shift = 0
            while True:
                b = data[pos]
                pos += 1
                if not (b & 0x80):
                    break
                shift += 7
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            break
    return best


def _scan_partition(data: bytes):
    """一次扫过 PartitionUpdate：名字 / 大小 / hash / operations 的原始位置

    返回 (name, size, hash, cand) where cand = {字段号: [(start, end), ...]}
    """
    pos = 0
    n = len(data)
    name = ""
    size = 0
    phash = b""
    cand = {}
    while pos < n:
        b = data[pos]
        pos += 1
        key = b & 0x7F
        shift = 7
        while b & 0x80:
            b = data[pos]
            pos += 1
            key |= (b & 0x7F) << shift
            shift += 7
        f = key >> 3
        wt = key & 7
        if wt == 2:
            ln = 0
            shift = 0
            while True:
                b = data[pos]
                pos += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            end = pos + ln
            if end > n:
                raise ValueError("length-delimited 越界")
            if f == 1:
                name = data[pos:end].decode("utf-8", "replace")
            elif f == 15:
                info = _parse_message(data[pos:end])
                size = int(_first(info, 1, 0) or 0)
                phash = _first(info, 2, b"") or b""
            elif f in (4, 5, 6, 7, 8, 14):
                cand.setdefault(f, []).append((pos, end))
            pos = end
        elif wt == 0:
            while True:
                b = data[pos]
                pos += 1
                if not (b & 0x80):
                    break
        elif wt == 5:
            pos += 4
        elif wt == 1:
            pos += 8
        else:
            raise ValueError(f"未知 wire type {wt}")
    return name, size, phash, cand


# ============================================================
# 数据结构
# ============================================================
class OpType:
    REPLACE = 0
    REPLACE_BZ = 1
    MOVE = 2
    BSDIFF = 3
    SOURCE_COPY = 4
    SOURCE_BSDIFF = 5
    ZERO = 6
    DISCARD = 7
    REPLACE_XZ = 8
    PUFFDIFF = 9
    BROTLI_BSDIFF = 10

    NAMES = {
        0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF",
        4: "SOURCE_COPY", 5: "SOURCE_BSDIFF", 6: "ZERO", 7: "DISCARD",
        8: "REPLACE_XZ", 9: "PUFFDIFF", 10: "BROTLI_BSDIFF",
        11: "ZUCCHINI", 12: "LZ4DIFF_BSDIFF", 13: "LZ4DIFF_PUFFDIFF",
        14: "ZSTD",
    }

    # 增量类操作（需要旧分区才能还原，本工具不支持）
    INCREMENTAL = {2, 3, 4, 5, 9, 10, 11, 12, 13}


@dataclass
class Extent:
    start_block: int
    num_blocks: int


@dataclass
class InstallOperation:
    type: int
    data_offset: int = 0
    data_length: int = 0
    dst_extents: List[Extent] = field(default_factory=list)
    src_extents: List[Extent] = field(default_factory=list)
    data_sha256_hash: bytes = b""

    @property
    def type_name(self) -> str:
        return OpType.NAMES.get(self.type, f"UNKNOWN({self.type})")


@dataclass
class PartitionUpdate:
    name: str
    size: int = 0
    hash: bytes = b""
    operations: List[InstallOperation] = field(default_factory=list)
    # ---- 延迟解析用：只记住操作在清单里的原始位置，需要时才建对象 ----
    _src: bytes = b""
    _op_field: int = 0
    _op_spans: list = field(default_factory=list)
    _summary: Optional[dict] = None
    _size_from_ops: bool = False
    _extent_end: int = -1

    # ---- 轻量摘要（不建 InstallOperation 对象）----
    def summary(self) -> dict:
        """返回 {"count": 操作数, "types": {类型: 次数}, "data": 压缩数据量}"""
        if self._summary is None:
            counts = {}
            total = 0
            src = self._src
            for s, e in self._op_spans:
                typ, dlen = _scan_op_head(src, s, e)
                counts[typ] = counts.get(typ, 0) + 1
                total += dlen
            self._summary = {"count": len(self._op_spans),
                             "types": counts, "data": total}
        return self._summary

    def op_count(self) -> int:
        return self.summary()["count"]

    def type_counts(self) -> dict:
        return self.summary()["types"]

    def data_total(self) -> int:
        return self.summary()["data"]

    def has_ops(self) -> bool:
        return bool(self._op_spans)

    def extent_end(self) -> int:
        """所有 dst_extents 的最大结束块（用于没记录大小时推算大小）"""
        if self._extent_end < 0:
            end = 0
            src = self._src
            for s, e in self._op_spans:
                end = max(end, _scan_op_extent_end(src, s, e))
            self._extent_end = end
        return self._extent_end


@dataclass
class PayloadManifest:
    block_size: int = 4096
    minor_version: int = 0
    partitions: List[PartitionUpdate] = field(default_factory=list)
    data_offset_base: int = 0    # 数据块在 payload 文件里的起始偏移
    # ---- 可视化用（对齐 VioletToolBox Payload 面板展示的信息）----
    major_version: int = 0
    manifest_size: int = 0
    metadata_signature_size: int = 0
    max_timestamp: int = 0
    security_patch_level: str = ""
    partial_update: bool = False
    dynamic_partition: bool = False
    apex_count: int = 0
    old_image_info: tuple = ()       # (size, hash)
    new_image_info: tuple = ()
    parse_warnings: List[str] = field(default_factory=list)


# ============================================================
# 异常
# ============================================================
class PayloadError(Exception):
    pass


class ExtractionCancelled(Exception):
    pass


# ============================================================
# 大小格式化
# ============================================================
def _human_size(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return "—"
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ============================================================
# 解析器
# ============================================================
class PayloadParser:
    """payload.bin 头部与清单解析器"""
    MAGIC = b"CrAU"

    def __init__(self, path, display_name=None, size=None):
        """path 既可以是路径，也可以是已打开的可读可定位对象

        （例如直接从 zip 里打开的 payload.bin，省掉"先解到磁盘再读回来"）
        """
        self._fh = None
        self.path = None
        if hasattr(path, "read") and hasattr(path, "seek"):
            self._fh = path
            self.display_name = (display_name or Path(
                getattr(path, "name", "payload.bin")).name)
            self.size = int(size or getattr(path, "size", 0) or 0)
        else:
            self.path = Path(path)
            if not self.path.is_file():
                raise PayloadError(f"文件不存在：{path}")
            self.display_name = display_name or self.path.name
            self.size = int(size or 0) or self.path.stat().st_size
        self.manifest: Optional[PayloadManifest] = None
        self._parse_header()

    def _open(self):
        """返回可读文件对象（zip 直读时复用同一句柄）"""
        if self._fh is not None:
            try:
                self._fh.seek(0)
            except Exception:
                pass
            return self._fh
        return open(self.path, "rb")

    def _ctx(self):
        """就地读取用的上下文管理器：zip 直读模式不关闭共享句柄"""
        if self._fh is not None:
            try:
                self._fh.seek(0)
            except Exception:
                pass
            return contextlib.nullcontext(self._fh)
        return open(self.path, "rb")

    # ---- 头部 ----
    def _parse_header(self):
        with self._ctx() as f:
            # 前 20 字节：magic(4) + version(8) + manifest_size(8)
            head = f.read(20)
            if len(head) < 20:
                raise PayloadError("文件太小，不是有效 payload")
            if head[:4] != self.MAGIC:
                raise PayloadError(
                    f"魔数不是 CrAU（实际：{head[:4]!r}），不是 payload.bin")

            version = struct.unpack(">Q", head[4:12])[0]
            manifest_size = struct.unpack(">Q", head[12:20])[0]

            if version >= 2:
                extra = f.read(4)
                if len(extra) < 4:
                    raise PayloadError("头部截断（缺 metadata_signature_size）")
                metadata_sig_size = struct.unpack(">I", extra)[0]
                manifest_offset = 24
            else:
                metadata_sig_size = 0
                manifest_offset = 20

            f.seek(manifest_offset)
            manifest_bytes = f.read(manifest_size)
            if len(manifest_bytes) != manifest_size:
                raise PayloadError("清单数据不完整")

            data_base = manifest_offset + manifest_size + metadata_sig_size

        self.manifest = self._parse_manifest(manifest_bytes, data_base)
        self.manifest.data_offset_base = data_base
        self.manifest.block_size = self.manifest.block_size or 4096
        self.manifest.major_version = version
        self.manifest.manifest_size = manifest_size
        self.manifest.metadata_signature_size = metadata_sig_size

    # ---- 清单 ----
    def _parse_manifest(self, data: bytes, data_base: int) -> PayloadManifest:
        msg = _parse_message(data)
        manifest = PayloadManifest()

        # DeltaArchiveManifest.block_size = 3（默认 4096）
        bs = _first(msg, 3)
        if bs:
            manifest.block_size = int(bs)

        # minor_version = 12
        mv = _first(msg, 12)
        if mv:
            manifest.minor_version = int(mv)

        # 可视化字段
        mt = _first(msg, 14)
        if mt:
            manifest.max_timestamp = int(mt)
        if _first(msg, 15) is not None:
            manifest.dynamic_partition = True      # dynamic_partition_metadata
        if _first(msg, 16):
            manifest.partial_update = True         # partial_update = 16(bool)
        manifest.apex_count = len(msg.get(17, []))  # apex_info
        spl = _first(msg, 20)
        if isinstance(spl, bytes):
            manifest.security_patch_level = spl.decode("utf-8", "replace")
        for field_no, attr in ((10, "old_image_info"), (11, "new_image_info")):
            raw = _first(msg, field_no)
            if raw:
                info = _parse_message(raw)
                setattr(manifest, attr,
                        (int(_first(info, 1, 0)), _first(info, 2, b"")))

        # partitions = 13（repeated PartitionUpdate）
        merged = {}
        for part_bytes in msg.get(13, []):
            try:
                part = self._parse_partition(part_bytes)
            except Exception as e:                      # 单个分区坏掉不影响整体
                manifest.parse_warnings.append(f"有分区解析失败：{e}")
                continue
            old = merged.get(part.name)
            if old is None:
                merged[part.name] = part
                continue
            # 同名分区（个别厂商包会拆成多条）：合并操作，取较大的 size
            old._op_spans.extend(part._op_spans)
            old._summary = None                 # 摘要要重算
            old._extent_end = -1
            old.operations = []                 # 已物化的对象作废，需重新按需解析
            old.size = max(old.size, part.size)
            old.hash = old.hash or part.hash
            manifest.parse_warnings.append(f"分区 {part.name} 有多条记录，已合并")
        manifest.partitions = list(merged.values())

        if not manifest.partitions:
            raise PayloadError("清单中没有任何分区")

        return manifest

    def _parse_partition(self, data: bytes) -> PartitionUpdate:
        """一次轻扫：分区名 / 大小 / 操作的原始位置（操作对象等用到再建）"""
        name, size, phash, cand = _scan_partition(data)
        part = PartitionUpdate(name=name)
        part.size = int(size or 0)
        part.hash = phash if isinstance(phash, bytes) else b""
        part._src = data
        part._op_field, part._op_spans = self._pick_ops_field(data, cand)
        # 没有 new_partition_info 时，用 dst_extents 推算大小（避免显示 0M）
        if not part.size and part._op_spans:
            bs = self.manifest.block_size if self.manifest else 4096
            part.size = part.extent_end() * bs
            part._size_from_ops = True
        return part

    def _pick_ops_field(self, data: bytes, cand: dict):
        """判定哪个字段才是 operations

        官方 AOSP 放在字段 5；部分厂商（OPPO / MTK 等）放在字段 8
        （见 VioletToolBox 的 update_metadata.proto）。用首个元素是否为
        合法 InstallOperation（类型合法且带 dst_extents）来判断。
        """
        fallback = (0, [])
        for field_no in (5, 8, 4, 6, 7, 14):
            spans = cand.get(field_no)
            if not spans:
                continue
            if not fallback[1]:
                fallback = (field_no, spans)
            typ, _ = _scan_op_head(data, spans[0][0], spans[0][1])
            if typ in OpType.NAMES and _scan_op_extent_end(
                    data, spans[0][0], spans[0][1]) > 0:
                return field_no, spans
        return fallback

    def partition_operations(self, part: PartitionUpdate) -> List[InstallOperation]:
        """把某分区的操作解析成对象（只在该分区被提取/查看详情时才做，结果缓存）"""
        if part.operations or not part._op_spans:
            return part.operations
        src = part._src
        part.operations = [self._parse_op_at(src, s, e)
                           for s, e in part._op_spans]
        return part.operations

    def _parse_op_at(self, data: bytes, start: int, end: int) -> InstallOperation:
        """解析单个 InstallOperation（含 extents 与 hash）"""
        pos = start
        op = InstallOperation(type=0)
        while pos < end:
            b = data[pos]
            pos += 1
            key = b & 0x7F
            shift = 7
            while b & 0x80:
                b = data[pos]
                pos += 1
                key |= (b & 0x7F) << shift
                shift += 7
            f = key >> 3
            wt = key & 7
            if wt == 0:
                val = 0
                shift = 0
                while True:
                    b = data[pos]
                    pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                if f == 1:
                    op.type = val
                elif f == 2:
                    op.data_offset = val
                elif f == 3:
                    op.data_length = val
            elif wt == 2:
                ln = 0
                shift = 0
                while True:
                    b = data[pos]
                    pos += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                chunk = data[pos:pos + ln]
                pos += ln
                if f == 4:
                    op.src_extents.append(self._parse_extent(chunk))
                elif f == 6:
                    op.dst_extents.append(self._parse_extent(chunk))
                elif f == 8:
                    op.data_sha256_hash = chunk
            elif wt == 5:
                pos += 4
            elif wt == 1:
                pos += 8
            else:
                break
        return op

    def debug_fields(self, name: str) -> List[str]:
        """诊断：打印某分区消息的原始字段（字段号/长度/子字段），用于排查变体格式"""
        try:
            with self._ctx() as f:
                head = f.read(20)
                version = struct.unpack(">Q", head[4:12])[0]
                manifest_size = struct.unpack(">Q", head[12:20])[0]
                moff = 24 if version >= 2 else 20
                if manifest_size <= 0 or manifest_size > 64 * 1024 * 1024:
                    return [f"诊断失败：清单大小异常（{manifest_size}）"]
                f.seek(moff)
                manifest = f.read(manifest_size)
        except Exception as e:
            return [f"诊断失败：{e}"]

        top = _parse_message(manifest)
        lines = ["[诊断] 清单顶层字段：" +
                 ", ".join(f"{k}×{len(v)}" for k, v in sorted(top.items()))]
        target = None
        for pb in top.get(13, []):
            pm = _parse_message(pb)
            nb = _first(pm, 1, b"")
            if isinstance(nb, bytes) and nb.decode("utf-8", "replace") == name:
                target = pb
                break
        if target is None:
            lines.append(f"[诊断] 没找到分区 {name} 的原始消息")
            return lines
        for k, v in sorted(_parse_message(target).items()):
            kinds = "/".join(sorted(set(type(x).__name__ for x in v)))
            extra = ""
            if all(isinstance(x, bytes) for x in v):
                extra = " 字节长度=" + ",".join(str(len(x)) for x in v[:3])
                try:
                    extra += f" 子字段={sorted(_parse_message(v[0]).keys())}"
                except Exception:
                    extra += " 子字段=<解析失败>"
            lines.append(f"[诊断] 字段 {k}：{len(v)} 项，类型 {kinds}{extra}")
        return lines

    def _parse_operation(self, data: bytes) -> InstallOperation:
        msg = _parse_message(data)
        op = InstallOperation(type=int(_first(msg, 1, 0)))
        op.data_offset = int(_first(msg, 2, 0))
        op.data_length = int(_first(msg, 3, 0))

        for ext_b in msg.get(4, []):
            op.src_extents.append(self._parse_extent(ext_b))
        for ext_b in msg.get(6, []):
            op.dst_extents.append(self._parse_extent(ext_b))

        op.data_sha256_hash = _first(msg, 8, b"")
        return op

    def _parse_extent(self, data: bytes) -> Extent:
        msg = _parse_message(data)
        return Extent(
            start_block=int(_first(msg, 1, 0)),
            num_blocks=int(_first(msg, 2, 0)),
        )

    # ---- 对外查询 ----
    def get_partition_info(self):
        """返回 [(name, size_bytes, size_str), ...]"""
        return [
            (p.name, p.size, _human_size(p.size))
            for p in self.manifest.partitions
        ]

    def get_partition(self, name: str) -> Optional[PartitionUpdate]:
        for p in self.manifest.partitions:
            if p.name == name:
                return p
        return None

    def describe(self) -> str:
        m = self.manifest
        kind = "全量" if m.minor_version == 0 else f"增量(v{m.minor_version})"
        return (f"payload.bin · {kind} · block_size={m.block_size} · "
                f"分区 {len(m.partitions)} 个")

    # ---- 可视化：整体信息 ----
    def info_lines(self) -> List[str]:
        """返回多行概览信息（供 UI 直接展示）"""
        m = self.manifest
        full = (m.minor_version == 0 and not m.partial_update)
        lines = [
            f"文件：{self.display_name}（{_human_size(self.size)}）",
            f"payload 版本：{m.major_version}　清单版本：v{m.minor_version}",
            f"包类型：{'全量包（full）' if full else '增量包（incremental）'}",
            f"block_size：{m.block_size} 字节",
            f"分区数量：{len(m.partitions)}",
            f"清单：{_human_size(m.manifest_size)}　数据区起点：0x{m.data_offset_base:X}",
            f"元数据签名：{'有（' + _human_size(m.metadata_signature_size) + '）' if m.metadata_signature_size else '无'}",
        ]
        if m.max_timestamp:
            try:
                import datetime
                ts = datetime.datetime.fromtimestamp(m.max_timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = str(m.max_timestamp)
            lines.append(f"构建时间戳：{ts}")
        if m.security_patch_level:
            lines.append(f"安全补丁级别：{m.security_patch_level}")
        if m.dynamic_partition:
            lines.append("包含动态分区元数据（super / dynamic partitions）")
        if m.apex_count:
            lines.append(f"包含 APEX：{m.apex_count} 个")
        if m.new_image_info:
            size, h = m.new_image_info
            lines.append(f"新镜像信息：{_human_size(size)}"
                         + (f"  hash={h.hex()[:16]}…" if h else ""))
        total = sum(p.size for p in m.partitions)
        lines.append(f"分区总大小：{_human_size(total)}")
        broken = [p.name for p in m.partitions if not p.has_ops()]
        if total == 0:
            lines.append("⚠ 清单未记录分区大小（部分厂商包没有 new_partition_info），"
                         "大小已按操作范围推算")
        if broken:
            preview = "、".join(broken[:6]) + ("…" if len(broken) > 6 else "")
            lines.append(f"⚠ {len(broken)} 个分区没有操作记录：{preview}"
                         "（双击表格行可查看诊断信息）")
        for w in m.parse_warnings[:5]:
            lines.append(f"⚠ {w}")
        return lines

    # ---- 可视化：分区统计 ----
    def partition_stats(self, part: PartitionUpdate) -> dict:
        """统计某分区的操作分布（走轻量摘要，不建操作对象 → 大包也很快）"""
        raw = part.type_counts()
        counts = {OpType.NAMES.get(t, f"UNKNOWN({t})"): c
                  for t, c in raw.items()}
        return {
            "ops": part.op_count(),
            "types": counts,
            "compressed": part.data_total(),
            "incremental": any(t in OpType.INCREMENTAL for t in raw),
        }

    def partition_note(self, part: PartitionUpdate) -> str:
        """给分区生成一句备注（表格用）"""
        st = self.partition_stats(part)
        if st["incremental"]:
            return "增量（需要旧分区，不支持提取）"
        if not part.has_ops():
            return "⚠ 无操作记录（解析不到，无法提取）"
        names = set(st["types"])
        if names.issubset({"REPLACE"}):
            return "全量·未压缩"
        if "REPLACE_XZ" in names:
            return "全量·XZ"
        if "REPLACE_BZ" in names:
            return "全量·bzip2"
        if "ZSTD" in names:
            return "全量·ZSTD"
        if names.issubset({"ZERO", "DISCARD", "REPLACE"}) and len(names) > 1:
            return "全量·含空块"
        return "全量（" + "/".join(sorted(names))[:24] + "）"

    def partition_detail_lines(self, part: PartitionUpdate) -> List[str]:
        """某分区的详细操作分布（多行）"""
        st = self.partition_stats(part)
        lines = [
            f"分区：{part.name}",
            f"大小：{_human_size(part.size)}（{part.size:,} 字节）",
            f"操作数：{st['ops']}　压缩数据量：{_human_size(st['compressed'])}",
        ]
        if part.hash:
            lines.append(f"目标 hash：{part.hash.hex()}")
        for name, cnt in sorted(st["types"].items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {cnt} 次")
        if st["incremental"]:
            lines.append("⚠ 含增量操作，本工具只能提取全量包")
        if not part.has_ops():
            lines.append("⚠ 清单里这个分区没有操作记录 / 没有大小，无法提取。")
            lines.extend(self.debug_fields(part.name))
        return lines


# ============================================================
# 提取器
# ============================================================
class PayloadExtractor:
    """纯 Python 提取器（全量 payload）"""

    CHUNK = 4 * 1024 * 1024      # 4MB 流式块
    _ZERO_CHUNK = b"\x00" * (1024 * 1024)   # 1MB 零块（ZERO 操作用来分块写）

    def __init__(self,
                 payload_path,
                 output_dir,
                 cancel_event: Optional[threading.Event] = None,
                 log_cb: Optional[Callable] = None,
                 prog_cb: Optional[Callable] = None,
                 display_name=None,
                 size=None,
                 stage_cb: Optional[Callable] = None):
        self.parser = PayloadParser(payload_path, display_name, size)
        self._fh = self.parser._fh          # zip 直读：与解析器共用同一个句柄
        self.path = self.parser.path
        self.stage_cb = stage_cb or (lambda name: None)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event = cancel_event or threading.Event()
        self.log_cb = log_cb or (lambda m, lv="plain": None)
        self.prog_cb = prog_cb or (lambda v: None)
        self.block_size = self.parser.manifest.block_size
        self.data_base = self.parser.manifest.data_offset_base
        self._last_pct = -1.0        # 进度限流用
        self._last_t = 0.0

    # ---- 检查取消 ----
    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise ExtractionCancelled()

    # ---- 主入口 ----
    def extract(self, partition_names: List[str]):
        """提取给定分区，返回 (成功列表, 失败列表[(名字, 原因)])

        单个分区失败不会中断整批（例如清单里缺操作记录的分区），
        最后统一汇报，避免"一个坏分区导致后面全提不出来"。
        """
        total = len(partition_names)
        ok_list, fail_list = [], []
        for idx, name in enumerate(partition_names):
            self._check_cancel()
            base = idx * 100.0 / total
            span = 100.0 / total
            self.log_cb(f"[{idx+1}/{total}] 提取 {name} …", "info")
            try:
                self.stage_cb(name)
            except Exception:
                pass
            self._report(base, force=True)
            try:
                self._extract_partition(
                    name,
                    prog=lambda f, b=base, s=span: self._report(b + s * f))
                self.log_cb(f"✓ 完成 {name}.img", "ok")
                ok_list.append(name)
            except ExtractionCancelled:
                raise
            except Exception as e:
                self.log_cb(f"❌ {name} 提取失败：{e}", "error")
                fail_list.append((name, str(e)))
            self._report(base + span, force=True)
        return ok_list, fail_list

    # ---- 进度上报（限流，避免刷爆事件循环）----
    def _report(self, pct, force=False):
        pct = max(0.0, min(100.0, float(pct)))
        now = time.monotonic()
        if not force:
            if now - self._last_t < 0.05:
                return
            if abs(pct - self._last_pct) < 0.5 and now - self._last_t < 0.12:
                return
        self._last_pct = pct
        self._last_t = now
        try:
            self.prog_cb(int(pct))
        except Exception:
            pass

    # ---- 单分区 ----
    def _extract_partition(self, name: str, prog=None):
        part = self.parser.get_partition(name)
        if not part:
            raise PayloadError(f"未找到分区 {name}")
        if not part.has_ops():
            raise PayloadError(
                f"分区 {name} 在清单里没有操作记录（读取信息时该分区也不显示大小）——"
                "请双击表格里这一行，把日志中的 [诊断] 内容发我")
        ops = self.parser.partition_operations(part)   # 按需解析（只解析要提取的分区）

        out_path = self.output_dir / f"{name}.img"
        self.log_cb(
            f"  {name}: {_human_size(part.size)} · "
            f"{len(ops)} 个操作 → {out_path.name}", "plain")

        # 进度按"数据字节数"加权：大块操作推进得多（同一分区内也能看到进度动）
        work = float(sum(op.data_length for op in ops))
        if work <= 0:
            work = float(max(1, len(ops)))
        done = [0.0]

        def tick(n=0.0):
            if prog:
                prog(min(1.0, (done[0] + n) / work))

        # 预分配文件大小（部分分区可能 size 为 0，就按 extents 累计）
        if part.size > 0:
            with open(out_path, "wb") as f:
                f.truncate(part.size)

        src_f = self._fh if self._fh is not None else open(self.path, "rb")
        dst_f = open(out_path, "r+b" if part.size > 0 else "wb")
        try:
            for op in ops:
                self._check_cancel()
                if op.type == OpType.REPLACE and op.data_length > 0:
                    # 未压缩数据：按拷贝字节实时回报进度
                    self._stream_copy(src_f, op.data_offset, op.data_length,
                                      dst_f, op.dst_extents, tick=tick)
                else:
                    self._process_operation(part.name, op, src_f, dst_f)
                    tick(op.data_length or 1.0)
                done[0] += float(op.data_length or 1)
                tick(0.0)
        finally:
            if self._fh is None:
                src_f.close()
            dst_f.close()
        if prog:
            prog(1.0)

    # ---- 操作分发 ----
    def _process_operation(self, part_name, op, src, dst):
        t = op.type

        if t == OpType.ZERO:
            # 分块写零，避免大分区一次性分配几百 MB
            for ext in op.dst_extents:
                dst.seek(ext.start_block * self.block_size)
                left = ext.num_blocks * self.block_size
                while left > 0:
                    self._check_cancel()
                    chunk = min(left, len(self._ZERO_CHUNK))
                    dst.write(self._ZERO_CHUNK[:chunk])
                    left -= chunk
            return

        if t == OpType.DISCARD:
            return

        if t == OpType.REPLACE:
            # 直接流式复制（不加载到内存）
            self._stream_copy(src, op.data_offset, op.data_length,
                              dst, op.dst_extents)
            return

        if t == OpType.REPLACE_BZ:
            blob = self._read_blob(src, op.data_offset, op.data_length)
            try:
                data = bz2.decompress(blob)
            except Exception as e:
                raise PayloadError(f"bzip2 解压失败：{e}")
            self._verify_sha256(data, op.data_sha256_hash, part_name)
            self._write_extents(dst, op.dst_extents, data)
            return

        if t == OpType.REPLACE_XZ:
            blob = self._read_blob(src, op.data_offset, op.data_length)
            try:
                data = lzma.decompress(blob)
            except lzma.LZMAError:
                # 极少数 payload 用的是裸 LZMA 流
                try:
                    data = lzma.decompress(blob, format=lzma.FORMAT_ALONE)
                except Exception as e:
                    raise PayloadError(f"XZ 解压失败：{e}")
            self._verify_sha256(data, op.data_sha256_hash, part_name)
            self._write_extents(dst, op.dst_extents, data)
            return

        if t in OpType.INCREMENTAL:
            raise PayloadError(
                f"分区 {part_name} 含增量操作（{op.type_name}），"
                "本工具只支持全量 payload")

        raise PayloadError(f"未知操作类型 {t}（分区 {part_name}）")

    # ---- 读取 / 写入 ----
    def _read_blob(self, src, offset, length) -> bytes:
        """从数据块起始偏移读 length 字节"""
        src.seek(self.data_base + offset)
        data = src.read(length)
        if len(data) != length:
            raise PayloadError(
                f"数据块读取不完整（期望 {length}，实际 {len(data)}）")
        return data

    def _write_extents(self, dst, extents: List[Extent], data: bytes):
        """把 data 按 extents 写到 dst 的指定块位置"""
        pos = 0
        for ext in extents:
            dst.seek(ext.start_block * self.block_size)
            chunk_size = ext.num_blocks * self.block_size
            chunk = data[pos:pos + chunk_size]
            dst.write(chunk)
            pos += len(chunk)

    def _stream_copy(self, src, offset, length, dst, extents, tick=None):
        """流式拷贝，避免 REPLACE 大块数据加载到内存；tick(已拷贝字节) 用于进度"""
        src.seek(self.data_base + offset)
        remaining = length
        copied = 0
        for ext in extents:
            dst.seek(ext.start_block * self.block_size)
            to_write = ext.num_blocks * self.block_size
            while to_write > 0 and remaining > 0:
                self._check_cancel()          # 大分区也能及时响应"停止"
                chunk = src.read(min(self.CHUNK, to_write, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                to_write -= len(chunk)
                remaining -= len(chunk)
                if tick:
                    tick(copied)
            if remaining <= 0:
                break

    # ---- 校验 ----
    def _verify_sha256(self, data: bytes, expected: bytes, part_name: str):
        if not expected:
            return
        actual = hashlib.sha256(data).digest()
        if actual != expected:
            self.log_cb(
                f"  ⚠ {part_name} 某操作 SHA256 不匹配（可能无碍）", "warn")