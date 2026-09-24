# -*- coding: utf-8 -*-
"""原生 AVB / vbmeta 解析、分析与重建签名引擎

结构与命名对齐 VioletToolBox 的 Avb/NativeAvbRebuilder.cs：
  · 纯 Python 实现（不依赖 avbtool，也不依赖第三方加密库）
  · 负责：解析镜像内嵌 vbmeta → 分析签名类型/公钥指纹/链式分区
          → 修改后重建 hash 描述符并重新签名（RSA PKCS#1 v1.5）
  · 用途：脱机修补 boot/init_boot 后，让镜像通过 AVB 校验（否则会无限重启）
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------- 常量（与 NativeAvbRebuilder.cs 一致） ----------------
BLOCK_SIZE = 4096
MAX_VBMETA_SIZE = 64 * 1024
MAX_FOOTER_SIZE = 4096
FOOTER_SIZE = 64
VBMETA_HEADER_SIZE = 256
AOSP_CHAIN_PARTITIONS = ("boot", "recovery", "vbmeta_system")

FOOTER_MAGIC = b"AVBf"
HEADER_MAGIC = b"AVB0"

TAG_PROPERTY = 0
TAG_HASHTREE = 1
TAG_HASH = 2
TAG_KERNEL_CMDLINE = 3
TAG_CHAIN_PARTITION = 4

TAG_NAMES = {
    TAG_PROPERTY: "Property",
    TAG_HASHTREE: "Hashtree",
    TAG_HASH: "Hash",
    TAG_KERNEL_CMDLINE: "KernelCmdline",
    TAG_CHAIN_PARTITION: "ChainPartition",
}

ALGORITHM_NONE = 0
ALGORITHM_SHA256_RSA2048 = 1
ALGORITHM_SHA256_RSA4096 = 2
ALGORITHM_SHA256_RSA8192 = 3
ALGORITHM_SHA512_RSA2048 = 4
ALGORITHM_SHA512_RSA4096 = 5
ALGORITHM_SHA512_RSA8192 = 6

ALGORITHM_NAMES = {
    ALGORITHM_NONE: "NONE",
    ALGORITHM_SHA256_RSA2048: "SHA256_RSA2048",
    ALGORITHM_SHA256_RSA4096: "SHA256_RSA4096",
    ALGORITHM_SHA256_RSA8192: "SHA256_RSA8192",
    ALGORITHM_SHA512_RSA2048: "SHA512_RSA2048",
    ALGORITHM_SHA512_RSA4096: "SHA512_RSA4096",
    ALGORITHM_SHA512_RSA8192: "SHA512_RSA8192",
}

ALGORITHM_BY_NAME = {v: k for k, v in ALGORITHM_NAMES.items()}

ALGORITHM_PARAMS = {
    ALGORITHM_SHA256_RSA2048: ("sha256", 2048),
    ALGORITHM_SHA256_RSA4096: ("sha256", 4096),
    ALGORITHM_SHA256_RSA8192: ("sha256", 8192),
    ALGORITHM_SHA512_RSA2048: ("sha512", 2048),
    ALGORITHM_SHA512_RSA4096: ("sha512", 4096),
    ALGORITHM_SHA512_RSA8192: ("sha512", 8192),
}

HASH_SIZES = {"sha256": 32, "sha512": 64, "sha1": 20,
              "blake2b-512": 64, "blake2s-256": 32}

FLAG_HASHTREE_DISABLED = 1
FLAG_VERIFICATION_DISABLED = 2


class AvbError(Exception):
    """AVB 处理异常"""


# 部分厂商（实测：本项目自带的 magisk_patched 镜像）用「大端」存 AVB 结构，
# 因此所有读写都要按检测到的端序进行，重建时也要沿用原端序。
def _endian_char(endian: str) -> str:
    return ">" if endian == "big" else "<"


def rd_u32(buf: bytes, off: int, endian: str = "little") -> int:
    return struct.unpack_from(_endian_char(endian) + "I", buf, off)[0]


def rd_u64(buf: bytes, off: int, endian: str = "little") -> int:
    return struct.unpack_from(_endian_char(endian) + "Q", buf, off)[0]


def wr_u32(buf: bytearray, off: int, value: int, endian: str = "little") -> None:
    struct.pack_into(_endian_char(endian) + "I", buf, off, value & 0xFFFFFFFF)


def wr_u64(buf: bytearray, off: int, value: int, endian: str = "little") -> None:
    struct.pack_into(_endian_char(endian) + "Q", buf, off, value & 0xFFFFFFFFFFFFFFFF)


def _align(value: int, size: int) -> int:
    rem = value % size
    return value if rem == 0 else value + size - rem


# ============================================================
# 数据结构
# ============================================================
@dataclass
class AvbFooter:
    version_major: int = 1
    version_minor: int = 0
    original_image_size: int = 0
    vbmeta_offset: int = 0
    vbmeta_size: int = 0
    offset: int = 0


@dataclass
class AvbVbmetaHeader:
    required_major: int = 1
    required_minor: int = 0
    authentication_data_block_size: int = 0
    auxiliary_data_block_size: int = 0
    algorithm_type: int = 0
    hash_offset: int = 0
    hash_size: int = 0
    signature_offset: int = 0
    signature_size: int = 0
    public_key_offset: int = 0
    public_key_size: int = 0
    public_key_metadata_offset: int = 0
    public_key_metadata_size: int = 0
    descriptors_offset: int = 0
    descriptors_size: int = 0
    rollback_index: int = 0
    flags: int = 0
    rollback_index_location: int = 0
    release_string: str = ""


@dataclass
class AvbDescriptor:
    tag: int = 0
    num_bytes_following: int = 0
    raw: bytes = b""
    image_size: int = 0
    hash_algorithm: str = ""
    partition_name: str = ""
    salt: bytes = b""
    digest: bytes = b""
    rollback_index_location: int = 0
    public_key: bytes = b""
    flags: int = 0
    text: str = ""

    @property
    def tag_name(self) -> str:
        return TAG_NAMES.get(self.tag, f"Unknown({self.tag})")


@dataclass
class AvbPublicKeyDetails:
    key_num_bits: int = 0
    sha256_hex: str = ""


@dataclass
class AvbPublicKeyProfile:
    public_key: AvbPublicKeyDetails = field(default_factory=AvbPublicKeyDetails)
    chain_keys: Dict[str, AvbPublicKeyDetails] = field(default_factory=dict)


@dataclass
class AvbImageInfo:
    """解析结果：footer + header + 描述符 + 公钥"""
    path: str = ""
    file_size: int = 0
    footer: Optional[AvbFooter] = None
    header: Optional[AvbVbmetaHeader] = None
    descriptors: List[AvbDescriptor] = field(default_factory=list)
    public_key: bytes = b""
    public_key_metadata: bytes = b""
    salt: bytes = b""
    signature: bytes = b""
    vbmeta_blob: bytes = b""
    vbmeta_hash: bytes = b""
    data_size: int = 0
    endian: str = "little"

    @property
    def is_chained(self) -> bool:
        return any(d.tag == TAG_CHAIN_PARTITION for d in self.descriptors)

    @property
    def hash_descriptors(self) -> List[AvbDescriptor]:
        return [d for d in self.descriptors if d.tag == TAG_HASH]

    @property
    def chain_descriptors(self) -> List[AvbDescriptor]:
        return [d for d in self.descriptors if d.tag == TAG_CHAIN_PARTITION]

    @property
    def partition_names(self) -> List[str]:
        return [d.partition_name for d in self.hash_descriptors if d.partition_name]

    @property
    def signature_type(self) -> str:
        """签名类型：非链式 / 链式 / AOSP 链式"""
        if not self.is_chained:
            return "非链式（standalone / AOSP）"
        names = {d.partition_name for d in self.chain_descriptors}
        if names and names.issubset(set(AOSP_CHAIN_PARTITIONS)):
            return "AOSP 链式（boot / recovery / vbmeta_system）"
        return "链式（厂商自定义链）"

    @property
    def algorithm_name(self) -> str:
        if not self.header:
            return "未知"
        return ALGORITHM_NAMES.get(self.header.algorithm_type,
                                   f"未知({self.header.algorithm_type})")

    @property
    def flags_text(self) -> str:
        if not self.header:
            return "未知"
        f = self.header.flags
        parts = []
        if f & FLAG_HASHTREE_DISABLED:
            parts.append("HASHTREE_DISABLED")
        if f & FLAG_VERIFICATION_DISABLED:
            parts.append("VERIFICATION_DISABLED")
        return " | ".join(parts) if parts else "0（未禁用任何校验）"


# ============================================================
# 纯 Python RSA（PKCS#1 v1.5）—— 不依赖第三方加密库
# ============================================================
# DigestInfo 前缀（DER 编码的 AlgorithmIdentifier + OCTET STRING 头）
_DIGEST_INFO_PREFIX = {
    "sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}


def _der_read(data: bytes, pos: int) -> Tuple[int, int, int, int]:
    """读取一个 DER TLV，返回 (tag, header_len, content_len, content_pos)"""
    if pos >= len(data):
        raise AvbError("DER 越界")
    tag = data[pos]
    pos += 1
    if pos >= len(data):
        raise AvbError("DER 长度越界")
    first = data[pos]
    pos += 1
    if first & 0x80:
        n = first & 0x7F
        if n == 0 or pos + n > len(data):
            raise AvbError("DER 长度字段非法")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    else:
        length = first
    if pos + length > len(data):
        raise AvbError("DER 内容越界")
    return tag, pos, length, pos


def _der_children(data: bytes, pos: int, end: int) -> List[Tuple[int, int, int, int]]:
    out = []
    while pos < end:
        tag, cpos, clen, cstart = _der_read(data, pos)
        out.append((tag, cpos, clen, cstart))
        pos = cpos + clen
    return out


def _der_int(data: bytes, header_len: int, clen: int, cstart: int) -> int:
    return int.from_bytes(data[cstart:cstart + clen], "big")


def parse_private_key_pem(pem_text: str) -> Tuple[int, int, int, int]:
    """解析 RSA 私钥 PEM（支持 PKCS#1 与 PKCS#8），返回 (n, e, d, key_bits)"""
    body = []
    for line in pem_text.splitlines():
        line = line.strip()
        if not line or line.startswith("-----"):
            continue
        body.append(line)
    if not body:
        raise AvbError("PEM 内容为空")
    try:
        der = base64.b64decode("".join(body))
    except Exception as e:
        raise AvbError(f"PEM base64 解码失败：{e}")

    tag, hlen, clen, cstart = _der_read(der, 0)
    if tag != 0x30:
        raise AvbError("不是有效的私钥 DER 结构")

    kids = _der_children(der, cstart, cstart + clen)
    if len(kids) >= 9 and kids[0][0] == 0x02:
        # PKCS#1 RSAPrivateKey
        nums = [_der_int(der, k[1], k[2], k[3]) for k in kids[:9]]
        n, e, d = nums[1], nums[2], nums[3]
    elif len(kids) >= 3 and kids[2][0] == 0x04:
        # PKCS#8 PrivateKeyInfo → 内层再解析一次
        inner = der[kids[2][3]:kids[2][3] + kids[2][2]]
        return parse_private_key_pem(
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + base64.encodebytes(inner).decode()
            + "-----END RSA PRIVATE KEY-----\n")
    else:
        raise AvbError("无法识别的私钥结构（仅支持 PKCS#1 / PKCS#8 RSA）")

    key_bits = n.bit_length()
    return n, e, d, key_bits


def _pkcs1_v15_em(hash_name: str, digest: bytes, key_bytes: int) -> bytes:
    prefix = _DIGEST_INFO_PREFIX.get(hash_name)
    if prefix is None:
        raise AvbError(f"不支持的哈希算法：{hash_name}")
    t = prefix + digest
    ps_len = key_bytes - len(t) - 3
    if ps_len < 8:
        raise AvbError("密钥长度不足以容纳 PKCS#1 v1.5 填充")
    return b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t


def rsa_pkcs1_sign(hash_name: str, digest: bytes, n: int, d: int,
                   key_bits: int) -> bytes:
    """按 PKCS#1 v1.5 用私钥对摘要签名（返回 key_bits/8 字节的大端签名）"""
    key_bytes = key_bits // 8
    em = _pkcs1_v15_em(hash_name, digest, key_bytes)
    m = int.from_bytes(em, "big")
    if m >= n:
        raise AvbError("待签名数据超出模数范围")
    s = pow(m, d, n)
    return s.to_bytes(key_bytes, "big")


def rsa_pkcs1_verify(hash_name: str, digest: bytes, signature: bytes,
                     n: int, e: int, key_bits: int) -> bool:
    key_bytes = key_bits // 8
    if len(signature) != key_bytes:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    m = pow(s, e, n)
    em = m.to_bytes(key_bytes, "big")
    return em == _pkcs1_v15_em(hash_name, digest, key_bytes)


def encode_avb_public_key(n: int, key_bits: int, endian: str = "little") -> bytes:
    """编码为 AVB 公钥结构：key_num_bits | n0inv | modulus | rr（按端序）"""
    key_bytes = key_bits // 8
    if n.bit_length() > key_bits:
        raise AvbError("模数与密钥位数不匹配")
    n0 = n & 0xFFFFFFFF
    if n0 == 0:
        raise AvbError("模数无效")
    inv = pow(n0, -1, 1 << 32)
    n0inv = (-inv) & 0xFFFFFFFF
    rr = pow(2, 2 * key_bits, n)
    head = bytearray(8)
    wr_u32(head, 0, key_bits, endian)
    wr_u32(head, 4, n0inv, endian)
    byteorder = "big" if endian == "big" else "little"
    return (bytes(head)
            + n.to_bytes(key_bytes, byteorder)
            + rr.to_bytes(key_bytes, byteorder))


def decode_avb_public_key(blob: bytes, endian: str = ""):
    """解析 AVB 公钥结构，返回 (key_num_bits, n, n0inv)（自动识别端序）"""
    if len(blob) < 8:
        raise AvbError("AVB 公钥长度无效")
    for en in ((endian,) if endian else ("little", "big")):
        key_bits = rd_u32(blob, 0, en)
        n0inv = rd_u32(blob, 4, en)
        if key_bits <= 0 or key_bits % 8 != 0 or key_bits > 8192:
            continue
        key_bytes = key_bits // 8
        if len(blob) != 8 + key_bytes * 2:
            continue
        byteorder = "big" if en == "big" else "little"
        n = int.from_bytes(blob[8:8 + key_bytes], byteorder)
        if n <= 0 or n.bit_length() != key_bits:
            continue
        return key_bits, n, n0inv
    raise AvbError("AVB 公钥结构或长度无效")


def public_key_details(blob: bytes, endian: str = "") -> AvbPublicKeyDetails:
    """对应 C# 的 CreatePublicKeyDetails（位数 + SHA256 指纹）"""
    key_bits, _n, _n0 = decode_avb_public_key(blob, endian)
    return AvbPublicKeyDetails(key_bits, hashlib.sha256(blob).hexdigest())


# ============================================================
# 解析器（对应 C# 的 ParseImage）
# ============================================================
def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def parse_footer(data: bytes):
    """在镜像尾部查找 AVBf footer；返回 (footer, endian) 或 None（自动识别端序）"""
    if len(data) < MAX_FOOTER_SIZE:
        return None
    search = max(0, len(data) - MAX_FOOTER_SIZE)
    while True:
        idx = data.find(FOOTER_MAGIC, search)
        if idx < 0 or idx + FOOTER_SIZE > len(data):
            return None
        for endian in ("little", "big"):
            major = rd_u32(data, idx + 4, endian)
            minor = rd_u32(data, idx + 8, endian)
            if major != 1 or minor != 0:
                continue
            ois = rd_u64(data, idx + 12, endian)
            vo = rd_u64(data, idx + 20, endian)
            vs = rd_u64(data, idx + 28, endian)
            if 0 < ois <= len(data) and 0 < vs <= MAX_VBMETA_SIZE \
                    and vo + vs <= len(data):
                return AvbFooter(1, 0, ois, vo, vs, idx), endian
        search = idx + 1


def _parse_header_with(blob: bytes, endian: str) -> AvbVbmetaHeader:
    return AvbVbmetaHeader(
        required_major=rd_u32(blob, 4, endian),
        required_minor=rd_u32(blob, 8, endian),
        authentication_data_block_size=rd_u64(blob, 12, endian),
        auxiliary_data_block_size=rd_u64(blob, 20, endian),
        algorithm_type=rd_u32(blob, 28, endian),
        hash_offset=rd_u64(blob, 32, endian), hash_size=rd_u64(blob, 40, endian),
        signature_offset=rd_u64(blob, 48, endian),
        signature_size=rd_u64(blob, 56, endian),
        public_key_offset=rd_u64(blob, 64, endian),
        public_key_size=rd_u64(blob, 72, endian),
        public_key_metadata_offset=rd_u64(blob, 80, endian),
        public_key_metadata_size=rd_u64(blob, 88, endian),
        descriptors_offset=rd_u64(blob, 96, endian),
        descriptors_size=rd_u64(blob, 104, endian),
        rollback_index=rd_u64(blob, 112, endian),
        flags=rd_u32(blob, 120, endian),
        rollback_index_location=rd_u32(blob, 124, endian),
        release_string=_cstr(blob[128:128 + 48]),
    )


def parse_header(blob: bytes, endian: str = ""):
    """解析 vbmeta header；返回 (header, endian)，endian 为空时自动识别"""
    if len(blob) < VBMETA_HEADER_SIZE or blob[:4] != HEADER_MAGIC:
        raise AvbError("未检测到有效的 AVB0 Header")
    for en in ((endian,) if endian else ("little", "big")):
        h = _parse_header_with(blob, en)
        if h.required_major != 1:
            continue
        if h.algorithm_type not in ALGORITHM_NAMES:
            continue
        if VBMETA_HEADER_SIZE + h.authentication_data_block_size > len(blob):
            continue
        if VBMETA_HEADER_SIZE + h.authentication_data_block_size \
                + h.auxiliary_data_block_size > len(blob):
            continue
        return h, en
    raise AvbError("vbmeta header 校验失败（端序或结构异常）")


def parse_descriptors(data: bytes, endian: str = "little") -> List[AvbDescriptor]:
    out: List[AvbDescriptor] = []
    pos = 0
    n = len(data)
    while pos + 16 <= n:
        tag = rd_u64(data, pos, endian)
        num = rd_u64(data, pos + 16 - 8, endian)
        if num > n - pos - 16:
            break
        body = data[pos + 16:pos + 16 + num]
        d = AvbDescriptor(tag=tag, num_bytes_following=num, raw=body)
        try:
            if tag == TAG_HASH and len(body) >= 148:
                d.image_size = rd_u64(body, 0, endian)
                d.hash_algorithm = _cstr(body[8:40])
                d.partition_name = _cstr(body[40:76])
                salt_size = rd_u32(body, 76, endian)
                digest_size = rd_u32(body, 80, endian)
                d.flags = rd_u32(body, 84, endian)
                if salt_size <= 64 and digest_size <= 64:
                    d.salt = body[148:148 + salt_size]
                    d.digest = body[148 + salt_size:148 + salt_size + digest_size]
            elif tag == TAG_HASHTREE and len(body) >= 164:
                d.image_size = rd_u32(body, 4, endian)
                d.hash_algorithm = _cstr(body[68:100])
                name_len = rd_u32(body, 100, endian)
                salt_len = rd_u32(body, 104, endian)
                digest_len = rd_u32(body, 108, endian)
                d.flags = rd_u32(body, 112, endian)
                off = 164
                d.partition_name = _cstr(body[off:off + name_len])
                off += name_len
                d.salt = body[off:off + salt_len]
                d.digest = body[off + salt_len:off + salt_len + digest_len]
            elif tag == TAG_CHAIN_PARTITION and len(body) >= 108:
                d.rollback_index_location = rd_u32(body, 0, endian)
                d.partition_name = _cstr(body[4:40])
                pub_len = rd_u32(body, 40, endian)
                d.flags = rd_u32(body, 44, endian)
                if pub_len <= len(body) - 108:
                    d.public_key = body[108:108 + pub_len]
            elif tag in (TAG_KERNEL_CMDLINE, TAG_PROPERTY):
                d.text = _cstr(body)
        except Exception:
            pass
        out.append(d)
        pos += 16 + ((num + 7) // 8) * 8
    return out


# ============================================================
# 镜像解析入口
# ============================================================
class AvbParser:
    """解析镜像尾部内嵌的 vbmeta（boot/init_boot）或独立 vbmeta 分区镜像"""

    @staticmethod
    def parse_image(path: str) -> Optional[AvbImageInfo]:
        with open(path, "rb") as f:
            data = f.read()
        parsed = parse_footer(data)
        if parsed is None:
            return None
        footer, endian = parsed
        if footer.vbmeta_size == 0:
            return None
        end = footer.vbmeta_offset + footer.vbmeta_size
        if end > len(data) or footer.vbmeta_offset < 0:
            return None

        blob = bytes(data[footer.vbmeta_offset:end])
        header, endian = parse_header(blob, endian)
        auth_size = header.authentication_data_block_size
        aux_off = VBMETA_HEADER_SIZE + auth_size
        auth = blob[VBMETA_HEADER_SIZE:aux_off]
        aux = blob[aux_off:aux_off + header.auxiliary_data_block_size]

        info = AvbImageInfo(
            path=path, file_size=len(data), footer=footer, header=header,
            vbmeta_blob=blob, data_size=footer.original_image_size,
            endian=endian)

        def _slice(buf: bytes, off: int, size: int) -> bytes:
            if off < 0 or size <= 0 or off + size > len(buf):
                return b""
            return buf[off:off + size]

        info.vbmeta_hash = _slice(auth, header.hash_offset, header.hash_size)
        info.signature = _slice(auth, header.signature_offset, header.signature_size)
        info.public_key = _slice(aux, header.public_key_offset, header.public_key_size)
        info.public_key_metadata = _slice(
            aux, header.public_key_metadata_offset, header.public_key_metadata_size)
        desc = _slice(aux, header.descriptors_offset, header.descriptors_size)
        info.descriptors = parse_descriptors(desc, endian)
        for d in info.hash_descriptors:
            if d.salt:
                info.salt = d.salt
                break
        return info

    # ---- 计算某个分区镜像的 AVB 摘要（salt || image）----
    @staticmethod
    def compute_digest(image_path: str, salt: bytes, hash_name: str = "sha256",
                       image_size: Optional[int] = None) -> bytes:
        h = hashlib.new(hash_name)
        if salt:
            h.update(salt)
        remaining = image_size
        with open(image_path, "rb") as f:
            while True:
                if remaining is not None:
                    if remaining <= 0:
                        break
                    chunk = f.read(min(1 << 20, remaining))
                    remaining -= len(chunk)
                else:
                    chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.digest()

    @staticmethod
    def compute_digest_bytes(data: bytes, salt: bytes, hash_name: str = "sha256",
                             image_size: Optional[int] = None) -> bytes:
        h = hashlib.new(hash_name)
        if salt:
            h.update(salt)
        h.update(data if image_size is None else data[:image_size])
        return h.digest()


# ============================================================
# 分析器（对应 C# 的 AnalyzePublicKeys / DetectPartitionName / Verify）
# ============================================================
DEFAULT_PARTITION_NAMES = ("boot", "init_boot", "vendor_boot", "recovery")


class AvbAnalyzer:
    def __init__(self, log_cb=None):
        self._log = log_cb or (lambda _m, _lv="plain": None)

    def analyze_public_keys(self, image_path: str) -> AvbPublicKeyProfile:
        info = AvbParser.parse_image(image_path)
        if info is None:
            raise AvbError("无法解析 vbmeta 镜像，未检测到有效的 AVB0 Header")
        if len(info.public_key) < 8:
            raise AvbError("vbmeta 中没有可分析的 AVB 公钥")
        chain = {}
        for d in info.chain_descriptors:
            if len(d.public_key) >= 8:
                chain[d.partition_name] = public_key_details(d.public_key, info.endian)
        return AvbPublicKeyProfile(public_key_details(info.public_key, info.endian), chain)

    def detect_partition_name(self, image_path: str, allowed_names=None,
                              fallback: str = "boot") -> str:
        """优先用 hash 描述符里的分区名，其次用文件名推断（对应 DetectPartitionName）"""
        allowed = [str(x).lower() for x in (allowed_names or DEFAULT_PARTITION_NAMES)]
        info = AvbParser.parse_image(image_path)
        if info is not None:
            for d in info.hash_descriptors:
                if d.partition_name and d.partition_name.lower() in allowed:
                    return d.partition_name
        stem = os.path.splitext(os.path.basename(image_path))[0].lower()
        for name in sorted(allowed, key=len, reverse=True):
            if stem == name or name in stem:
                return name
        return fallback

    def verify(self, image_path: str) -> Tuple[bool, str]:
        """校验镜像内 hash 描述符与当前内容是否一致"""
        info = AvbParser.parse_image(image_path)
        if info is None:
            return False, "该镜像没有内嵌 vbmeta"
        d = next((x for x in info.hash_descriptors), None)
        if d is None:
            return False, "vbmeta 中没有 hash 描述符"
        hash_name = d.hash_algorithm or "sha256"
        actual = AvbParser.compute_digest(image_path, d.salt, hash_name,
                                          d.image_size or None)
        if actual == d.digest:
            return True, "AVB 摘要一致（镜像未被改动）"
        return False, (f"AVB 摘要不一致：期望 {d.digest.hex()[:16]}… "
                       f"实际 {actual.hex()[:16]}（说明镜像已被修改，需要重建签名）")

    def report(self, image_path: str) -> List[str]:
        """生成分析报告（多行，供 UI 直接打印）"""
        info = AvbParser.parse_image(image_path)
        if info is None:
            return ["该镜像没有内嵌 vbmeta（可能是纯链式分区模式，或非 AVB 镜像）"]
        lines = [
            f"镜像：{os.path.basename(image_path)}（{info.file_size:,} 字节）",
            f"签名类型：{info.signature_type}",
            f"算法：{info.algorithm_name}    校验标志：{info.flags_text}",
            f"vbmeta：偏移 0x{info.footer.vbmeta_offset:X}，大小 {info.footer.vbmeta_size:,} 字节，"
            f"原始镜像数据 {info.data_size:,} 字节",
        ]
        if info.header:
            lines.append(f"rollback_index={info.header.rollback_index}  "
                         f"release_string={info.header.release_string or '(空)'}")
        try:
            if len(info.public_key) >= 8:
                det = public_key_details(info.public_key, info.endian)
                lines.append(f"主公钥：{det.key_num_bits} 位  "
                             f"SHA256={det.sha256_hex[:32]}…")
        except AvbError:
            lines.append("主公钥：无（未签名 / NONE 算法）")
        names = info.partition_names
        if names:
            lines.append("hash 描述符分区：" + "、".join(names))
        for d in info.chain_descriptors:
            try:
                kd = public_key_details(d.public_key, info.endian)
                lines.append(f"  链式分区 {d.partition_name}: {kd.key_num_bits} 位  "
                             f"SHA256={kd.sha256_hex[:24]}…")
            except AvbError:
                lines.append(f"  链式分区 {d.partition_name}: 公钥无效")
        ok, why = self.verify(image_path)
        lines.append(("✅ " if ok else "⚠ ") + why)
        return lines


# ============================================================
# 重建 / 签名（对应 C# 的 EncodeHeader / EncodeFooter / Rebuild）
# ============================================================
def encode_descriptor(tag: int, body: bytes, endian: str = "little") -> bytes:
    total = ((len(body) + 7) // 8) * 8
    head = bytearray(16)
    wr_u64(head, 0, tag, endian)
    wr_u64(head, 8, total, endian)
    return bytes(head) + body + b"\x00" * (total - len(body))


def encode_hash_descriptor(partition_name: str, salt: bytes, digest: bytes,
                           image_size: int, hash_algorithm: str = "sha256",
                           flags: int = 0, endian: str = "little") -> bytes:
    body = bytearray()
    head = bytearray(8)
    wr_u64(head, 0, image_size, endian)
    body += head
    body += hash_algorithm.encode("ascii", "replace")[:31].ljust(32, b"\x00")
    body += partition_name.encode("utf-8")[:35].ljust(36, b"\x00")
    tail = bytearray(12)
    wr_u32(tail, 0, len(salt), endian)
    wr_u32(tail, 4, len(digest), endian)
    wr_u32(tail, 8, flags, endian)
    body += tail
    body += b"\x00" * 60
    body += salt + digest
    return encode_descriptor(TAG_HASH, bytes(body), endian)


def encode_chain_descriptor(partition_name: str, public_key: bytes,
                            rollback_index_location: int = 0,
                            flags: int = 0, endian: str = "little") -> bytes:
    body = bytearray(40)
    wr_u32(body, 0, rollback_index_location, endian)
    body[4:40] = partition_name.encode("utf-8")[:35].ljust(36, b"\x00")
    tail = bytearray(8)
    wr_u32(tail, 0, len(public_key), endian)
    wr_u32(tail, 4, flags, endian)
    body += tail
    body += b"\x00" * 60
    body += public_key
    return encode_descriptor(TAG_CHAIN_PARTITION, bytes(body), endian)


def encode_header(h: AvbVbmetaHeader, endian: str = "little") -> bytes:
    data = bytearray(VBMETA_HEADER_SIZE)
    data[0:4] = HEADER_MAGIC
    wr_u32(data, 4, h.required_major, endian)
    wr_u32(data, 8, h.required_minor, endian)
    wr_u64(data, 12, h.authentication_data_block_size, endian)
    wr_u64(data, 20, h.auxiliary_data_block_size, endian)
    wr_u32(data, 28, h.algorithm_type, endian)
    wr_u64(data, 32, h.hash_offset, endian)
    wr_u64(data, 40, h.hash_size, endian)
    wr_u64(data, 48, h.signature_offset, endian)
    wr_u64(data, 56, h.signature_size, endian)
    wr_u64(data, 64, h.public_key_offset, endian)
    wr_u64(data, 72, h.public_key_size, endian)
    wr_u64(data, 80, h.public_key_metadata_offset, endian)
    wr_u64(data, 88, h.public_key_metadata_size, endian)
    wr_u64(data, 96, h.descriptors_offset, endian)
    wr_u64(data, 104, h.descriptors_size, endian)
    wr_u64(data, 112, h.rollback_index, endian)
    wr_u32(data, 120, h.flags, endian)
    wr_u32(data, 124, h.rollback_index_location, endian)
    release = h.release_string.encode("utf-8", "replace")[:47]
    data[128:128 + len(release)] = release
    return bytes(data)


def encode_footer(f: AvbFooter, endian: str = "little") -> bytes:
    data = bytearray(FOOTER_SIZE)
    data[0:4] = FOOTER_MAGIC
    wr_u32(data, 4, f.version_major, endian)
    wr_u32(data, 8, f.version_minor, endian)
    wr_u64(data, 12, f.original_image_size, endian)
    wr_u64(data, 20, f.vbmeta_offset, endian)
    wr_u64(data, 28, f.vbmeta_size, endian)
    return bytes(data)


@dataclass
class AvbRebuildOptions:
    """重建选项"""
    key_pem: str = ""
    algorithm: int = 0
    partition_name: str = ""
    salt: Optional[bytes] = None
    set_flags: int = 0
    clear_flags: int = 0
    keep_layout: bool = True
    endian: str = ""


class AvbRebuilder:
    """重建镜像内嵌 vbmeta 并（可选）重新签名

    与 VioletToolBox 的做法一致：修改 boot 镜像后重算 hash 描述符并重新签名，
    使镜像能通过 AVB 校验；若没有私钥，也可只置位「禁用校验」标志
    （等同于官方 PATCHVBMETAFLAG）。
    """

    def __init__(self, log_cb=None):
        self._log = log_cb or (lambda _m, _lv="plain": None)

    def rebuild_and_sign(self, image_path: str, options: AvbRebuildOptions,
                         out_path: Optional[str] = None) -> str:
        info = AvbParser.parse_image(image_path)
        if info is None or info.header is None or info.footer is None:
            raise AvbError("该镜像没有内嵌 vbmeta，无法重建签名")

        with open(image_path, "rb") as f:
            raw = f.read()
        original_size = info.footer.original_image_size or info.footer.vbmeta_offset
        if original_size <= 0 or original_size > len(raw):
            raise AvbError("原镜像数据尺寸异常，无法重建")
        base = bytes(raw[:original_size])

        salt = options.salt if options.salt is not None else (info.salt or b"")
        endian = options.endian or info.endian
        algorithm = options.algorithm or info.header.algorithm_type
        if algorithm == ALGORITHM_NONE:
            raise AvbError("原镜像未签名（NONE），请指定签名的 hash 算法")
        hash_name, key_bits = ALGORITHM_PARAMS[algorithm]
        hash_size = HASH_SIZES[hash_name]
        sig_size = key_bits // 8

        part = options.partition_name or (info.partition_names[0]
                                          if info.partition_names else "boot")
        self._log(f"重建 vbmeta：分区={part}，算法={ALGORITHM_NAMES[algorithm]}", "plain")

        # ---- 1. 重建描述符（重算目标分区的 hash 描述符）----
        descs = []
        for d in info.descriptors:
            if d.tag == TAG_HASH and (not options.partition_name
                                     or d.partition_name == part):
                digest = AvbParser.compute_digest_bytes(base, salt, hash_name,
                                                        original_size)
                descs.append(encode_hash_descriptor(
                    d.partition_name or part, salt, digest, original_size,
                    hash_name, d.flags, endian))
            else:
                descs.append(encode_descriptor(d.tag, d.raw, endian))
        desc_blob = b"".join(descs)

        # ---- 2. 公钥（用私钥重新编码；无密钥则沿用原公钥）----
        pubkey = info.public_key
        pub_meta = info.public_key_metadata
        n = d_exp = 0
        if options.key_pem:
            n, e, d_exp, kbits = parse_private_key_pem(options.key_pem)
            if kbits != key_bits:
                raise AvbError(
                    f"私钥位数({kbits}) 与算法({ALGORITHM_NAMES[algorithm]}) 不匹配")
            pubkey = encode_avb_public_key(n, kbits, endian)
            pub_meta = b""

        # ---- 3. 布局：auth 与 aux 分别尽量沿用原偏移（保持产物结构与官方一致）----
        def a8(x: int) -> int:
            return (x + 7) // 8 * 8

        keep_auth = (options.keep_layout
                     and info.header.hash_size == hash_size
                     and info.header.signature_size == sig_size)
        if keep_auth:
            h_off = info.header.hash_offset
            s_off = info.header.signature_offset
            auth_size = max(info.header.authentication_data_block_size,
                            hash_size + sig_size)
        else:
            h_off = 0
            s_off = hash_size
            auth_size = hash_size + sig_size

        keep_aux = (options.keep_layout
                    and len(pubkey) == (info.header.public_key_size or 0)
                    and len(pub_meta) == (info.header.public_key_metadata_size or 0)
                    and len(desc_blob) == (info.header.descriptors_size or 0))
        if keep_aux:
            pk_off = info.header.public_key_offset
            pm_off = info.header.public_key_metadata_offset
            ds_off = info.header.descriptors_offset
            aux_size = max(info.header.auxiliary_data_block_size,
                           a8(ds_off + len(desc_blob)))
        else:
            pk_off = 0
            pm_off = a8(pk_off + len(pubkey)) if pub_meta else 0
            end = (pm_off + len(pub_meta)) if pub_meta else (pk_off + len(pubkey))
            ds_off = a8(end) if (pubkey or pub_meta) else 0
            aux_size = a8(ds_off + len(desc_blob))

        aux = bytearray(aux_size)
        if pubkey:
            aux[pk_off:pk_off + len(pubkey)] = pubkey
        if pub_meta:
            aux[pm_off:pm_off + len(pub_meta)] = pub_meta
        if desc_blob:
            aux[ds_off:ds_off + len(desc_blob)] = desc_blob
        aux = bytes(aux)

        # ---- 4. 头部 + 摘要 + 签名 ----
        flags = (info.header.flags | options.set_flags) & ~options.clear_flags
        header = AvbVbmetaHeader(
            required_major=info.header.required_major,
            required_minor=info.header.required_minor,
            authentication_data_block_size=auth_size,
            auxiliary_data_block_size=len(aux),
            algorithm_type=algorithm,
            hash_offset=h_off, hash_size=hash_size,
            signature_offset=s_off, signature_size=sig_size,
            public_key_offset=pk_off if pubkey else 0,
            public_key_size=len(pubkey),
            public_key_metadata_offset=pm_off if pub_meta else 0,
            public_key_metadata_size=len(pub_meta),
            descriptors_offset=ds_off, descriptors_size=len(desc_blob),
            rollback_index=info.header.rollback_index,
            flags=flags,
            rollback_index_location=info.header.rollback_index_location,
            release_string=info.header.release_string)
        header_bytes = encode_header(header, endian)
        digest = hashlib.new(hash_name, header_bytes + aux).digest()
        auth = bytearray(auth_size)
        auth[h_off:h_off + hash_size] = digest
        if options.key_pem:
            auth[s_off:s_off + sig_size] = rsa_pkcs1_sign(
                hash_name, digest, n, d_exp, key_bits)
        vbmeta_blob = header_bytes + bytes(auth) + aux

        # ---- 5. 组装镜像：原数据 + vbmeta + 填充 + 末尾 footer ----
        # AVB 规范：footer 必须位于镜像最后 64 字节（avbtool 亦如此）
        vbmeta_offset = info.footer.vbmeta_offset
        aligned = _align(original_size, BLOCK_SIZE)
        if not (options.keep_layout and aligned <= vbmeta_offset):
            vbmeta_offset = aligned
        vbmeta_end = vbmeta_offset + len(vbmeta_blob)
        need = vbmeta_end + FOOTER_SIZE
        if options.keep_layout and info.file_size > need:
            need = info.file_size
        total = _align(need, MAX_FOOTER_SIZE)
        footer_offset = total - FOOTER_SIZE

        out = bytearray(total)
        out[0:original_size] = base
        out[vbmeta_offset:vbmeta_end] = vbmeta_blob
        out[footer_offset:footer_offset + FOOTER_SIZE] = encode_footer(
            AvbFooter(1, 0, original_size, vbmeta_offset, len(vbmeta_blob)), endian)

        if not out_path:
            stem, ext = os.path.splitext(image_path)
            out_path = f"{stem}_signed{ext or '.img'}"
        with open(out_path, "wb") as f:
            f.write(bytes(out))
        self._log(f"✓ AVB 重建完成：{os.path.basename(out_path)}"
                  f"（{len(out):,} 字节，flags={flags}，"
                  f"端序={('大端' if endian == 'big' else '小端')}）", "ok")
        return out_path

    def rebuild_disable_verity(self, image_path: str,
                               out_path: Optional[str] = None) -> str:
        """只置位「禁用校验」标志（无需私钥，等同官方 PATCHVBMETAFLAG）"""
        return self.rebuild_and_sign(
            image_path,
            AvbRebuildOptions(
                set_flags=FLAG_HASHTREE_DISABLED | FLAG_VERIFICATION_DISABLED),
            out_path)


