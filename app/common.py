"""通用工具：规范化 JSON、内容指纹、字段路径、四舍五入。"""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal


def canonical(obj):
    """按键排序、无空白的 JSON 文本，用于幂等比较与指纹。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj):
    return sha256_hex(canonical(obj))


def join_path(*parts):
    """拼接字段路径，None 段会被跳过。"""
    out = []
    for p in parts:
        if p is None or p == "":
            continue
        out.append(str(p))
    return ".".join(out)


def ref_path(parts):
    """配方引用链，如 components[0](SAUCE@2)。"""
    return "components" + "".join(parts) if parts else "components"


def round_half_up(value, ndigits):
    """金融式四舍五入（避免 Python 内置 round 的银行家舍入）。"""
    if value is None:
        return None
    quant = Decimal(1).scaleb(-ndigits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))
