"""请求载荷校验与规范化。

校验只负责“形状与取值是否合法”，引用是否存在、循环、单位可达性等
语义问题交给计算引擎在展开时处理并给出带引用链的字段路径。
"""

import numbers
import re

from .errors import claim_conflict, malformed
from .common import content_hash

VALID_ALLERGEN_STATUS = ("contains", "may_contain", "free")


# ---- 基础类型检查 -----------------------------------------------------------

def _is_num(v):
    return isinstance(v, numbers.Real) and not isinstance(v, bool)


def _is_nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def _require(obj, key, path, typ=None):
    if not isinstance(obj, dict) or key not in obj:
        raise malformed(f"缺少必填字段 {key!r}", f"{path}.{key}".lstrip("."))
    v = obj[key]
    if typ == "str" and not _is_nonempty_str(v):
        raise malformed(f"字段 {key!r} 必须是非空字符串", f"{path}.{key}".lstrip("."))
    if typ == "num" and not _is_num(v):
        raise malformed(f"字段 {key!r} 必须是数字", f"{path}.{key}".lstrip("."))
    if typ == "dict" and not isinstance(v, dict):
        raise malformed(f"字段 {key!r} 必须是对象", f"{path}.{key}".lstrip("."))
    if typ == "list" and not isinstance(v, list):
        raise malformed(f"字段 {key!r} 必须是数组", f"{path}.{key}".lstrip("."))
    if typ == "pos" and (not _is_num(v) or v <= 0):
        raise malformed(f"字段 {key!r} 必须是正数", f"{path}.{key}".lstrip("."))
    return v


def _require_body(req):
    if not isinstance(req, dict):
        raise malformed("请求体必须是 JSON 对象", None)
    return req


# ---- 原料资料 ---------------------------------------------------------------

def validate_ingredients(req):
    req = _require_body(req)
    version = _require(req, "release_version", "", "str")
    items = _require(req, "ingredients", "", "list")
    if not items:
        raise malformed("ingredients 至少包含一项", "ingredients")

    norm = []
    seen = set()
    for i, ing in enumerate(items):
        p = f"ingredients[{i}]"
        if not isinstance(ing, dict):
            raise malformed("原料项必须是对象", p)
        code = _require(ing, "code", p, "str")
        if code in seen:
            raise malformed(f"原料 {code} 在同一批次中重复", f"{p}.code")
        seen.add(code)

        nutrition = _require(ing, "nutrition", p, "dict")
        for k, v in nutrition.items():
            if not _is_nonempty_str(k):
                raise malformed("营养素名称必须是非空字符串", f"{p}.nutrition")
            if not _is_num(v) or v < 0:
                raise malformed(f"营养素 {k!r} 的值必须是非负数字", f"{p}.nutrition.{k}")

        basis_amount = ing.get("basis_amount", 100)
        if not _is_num(basis_amount) or basis_amount <= 0:
            raise malformed("basis_amount 必须是正数", f"{p}.basis_amount")
        basis_unit = _require(ing, "basis_unit", p, "str")

        allergens_in = ing.get("allergens", {})
        if not isinstance(allergens_in, dict):
            raise malformed("allergens 必须是对象", f"{p}.allergens")
        allergens = {}
        for name, status in allergens_in.items():
            if not _is_nonempty_str(name):
                raise malformed("过敏原名称必须是非空字符串", f"{p}.allergens")
            if status not in VALID_ALLERGEN_STATUS:
                raise malformed(
                    f"过敏原 {name!r} 状态必须是 {VALID_ALLERGEN_STATUS} 之一",
                    f"{p}.allergens.{name}",
                )
            allergens[name] = status

        norm.append(
            {
                "code": code,
                "name": ing.get("name", code),
                "nutrition": nutrition,
                "basis_amount": float(basis_amount),
                "basis_unit": basis_unit,
                "allergens": allergens,
            }
        )

    body = {"ingredients": norm}
    return {
        "release_version": version,
        "body": body,
        "body_hash": content_hash(body),
        "ingredients": norm,
    }


# ---- 单位换算表 -------------------------------------------------------------

def validate_units(req):
    req = _require_body(req)
    version = _require(req, "version", "", "str")
    convs = _require(req, "conversions", "", "list")
    units = set()
    edges = []
    seen_pairs = {}
    norm = []
    for i, c in enumerate(convs):
        p = f"conversions[{i}]"
        if not isinstance(c, dict):
            raise malformed("换算项必须是对象", p)
        f = _require(c, "from_unit", p, "str")
        t = _require(c, "to_unit", p, "str")
        factor = _require(c, "factor", p, "num")
        if factor <= 0:
            raise malformed("factor 必须是正数", f"{p}.factor")
        if f == t and not abs(factor - 1.0) < 1e-12:
            raise malformed(f"单位 {f} 到自身的 factor 必须为 1", f"{p}.factor")
        key = (f, t)
        rev = (t, f)
        if key in seen_pairs:
            raise malformed(f"{f} -> {t} 在同一版本中重复声明", p)
        # 同批次正反同时声明时也检查一致性（由 UnitGraph 闭包检查兜底）
        seen_pairs[key] = float(factor)
        if rev in seen_pairs and abs(seen_pairs[rev] * factor - 1.0) > 1e-9:
            from .errors import conflict

            raise conflict(
                f"{f} -> {t} 与反向声明矛盾：factor 乘积应约等于 1",
                p,
                {"forward": factor, "reverse": seen_pairs[rev]},
            )
        units.update((f, t))
        edges.append((f, t, float(factor)))
        norm.append({"from_unit": f, "to_unit": t, "factor": float(factor)})

    body = {"conversions": norm, "units": sorted(units)}
    return {
        "version": version,
        "body": body,
        "body_hash": content_hash(body),
        "units": units,
        "edges": edges,
        "conversions": norm,
    }


# ---- 标签规则 ---------------------------------------------------------------

DEFAULT_NUTRIENT_DECIMALS = 1
DEFAULT_DV_DECIMALS = 0

VALID_CLAIM_DIRECTIONS = ("lte", "gte")


def _validate_claims(req, required_names):
    """营养声明规则：声明名称、适用营养素、每份阈值、比较方向与单位。

    声明的判定依赖每份营养值，因此适用营养素必须已在本版本的
    required_nutrients 中声明（缺失营养素 → 400）；direction 仅接受
    lte/gte（无效方向 → 400）；同名声明定义矛盾（阈值冲突等 → 409）。
    """
    claims_in = req.get("claims", [])
    if not isinstance(claims_in, list):
        raise malformed("claims 必须是数组", "claims")

    claims = []
    seen = {}
    for i, c in enumerate(claims_in):
        p = f"claims[{i}]"
        if not isinstance(c, dict):
            raise malformed("声明规则必须是对象", p)
        name = _require(c, "name", p, "str")
        nutrient = _require(c, "nutrient", p, "str")
        threshold = _require(c, "threshold", p, "num")
        if threshold < 0:
            raise malformed("threshold 必须是非负数字", f"{p}.threshold")
        direction = _require(c, "direction", p, "str")
        if direction not in VALID_CLAIM_DIRECTIONS:
            raise malformed(
                f"direction 必须是 {VALID_CLAIM_DIRECTIONS} 之一"
                "（lte=不超过阈值，gte=不低于阈值）",
                f"{p}.direction",
            )
        unit = _require(c, "unit", p, "str")
        if nutrient not in required_names:
            raise malformed(
                f"声明引用的营养素 {nutrient!r} 未在本版本的 "
                "required_nutrients 中声明，无法按每份值判定",
                f"{p}.nutrient",
                {"nutrient": nutrient,
                 "required_nutrients": sorted(required_names)},
            )
        claim = {
            "name": name,
            "nutrient": nutrient,
            "threshold": float(threshold),
            "direction": direction,
            "unit": unit,
        }
        if name in seen:
            prev = seen[name]
            if prev != claim:
                for attr in ("threshold", "nutrient", "direction", "unit"):
                    if prev[attr] != claim[attr]:
                        raise claim_conflict(
                            f"声明 {name!r} 在同一规则版本中存在冲突定义："
                            f"{attr} 既为 {prev[attr]!r} 又为 {claim[attr]!r}",
                            f"{p}.{attr}",
                            {"claim": name, "attribute": attr,
                             "existing": prev[attr], "new": claim[attr]},
                        )
            raise malformed(f"声明 {name!r} 在同一规则版本中重复", f"{p}.name")
        seen[name] = claim
        claims.append(claim)
    return claims


def validate_rules(req):
    req = _require_body(req)
    version = _require(req, "version", "", "str")
    nutrients_in = _require(req, "required_nutrients", "", "list")

    nutrients = []
    seen = set()
    for i, n in enumerate(nutrients_in):
        p = f"required_nutrients[{i}]"
        if not isinstance(n, dict):
            raise malformed("营养素规则必须是对象", p)
        name = _require(n, "name", p, "str")
        if name in seen:
            raise malformed(f"营养素 {name!r} 规则重复", f"{p}.name")
        seen.add(name)
        dv = n.get("daily_value")
        if dv is not None and (not _is_num(dv) or dv <= 0):
            raise malformed("daily_value 必须是正数", f"{p}.daily_value")
        decimals = n.get("decimals", DEFAULT_NUTRIENT_DECIMALS)
        if not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
            raise malformed("decimals 必须是非负整数", f"{p}.decimals")
        nutrients.append(
            {
                "name": name,
                "daily_value": float(dv) if dv is not None else None,
                "decimals": decimals,
            }
        )

    required_allergens = req.get("required_allergens", [])
    if not isinstance(required_allergens, list) or not all(
        _is_nonempty_str(a) for a in required_allergens
    ):
        raise malformed("required_allergens 必须是字符串数组", "required_allergens")

    claims = _validate_claims(req, seen)

    dv_decimals = req.get("dv_decimals", DEFAULT_DV_DECIMALS)
    if not isinstance(dv_decimals, int) or isinstance(dv_decimals, bool) or dv_decimals < 0:
        raise malformed("dv_decimals 必须是非负整数", "dv_decimals")

    body = {
        "required_nutrients": nutrients,
        "required_allergens": required_allergens,
        "claims": claims,
        "dv_decimals": dv_decimals,
    }
    return {"version": version, "body": body, "body_hash": content_hash(body)}


# ---- 配方 -------------------------------------------------------------------

VALID_KINDS = ("ingredient", "recipe")


def validate_recipe(req):
    req = _require_body(req)
    code = _require(req, "code", "", "str")
    version = _require(req, "version", "", "str")
    _require(req, "yield", "", "dict")
    yq = _require(req["yield"], "qty", "yield", "pos")
    yu = _require(req["yield"], "unit", "yield", "str")
    servings = _require(req, "servings", "", "pos")

    components = _require(req, "components", "", "list")
    if not components:
        raise malformed("配方至少包含一个成分", "components")

    norm_comps = []
    for i, c in enumerate(components):
        p = f"components[{i}]"
        if not isinstance(c, dict):
            raise malformed("成分项必须是对象", p)
        kind = _require(c, "kind", p, "str")
        if kind not in VALID_KINDS:
            raise malformed(f"kind 必须是 {VALID_KINDS} 之一", f"{p}.kind")
        ref_code = _require(c, "code", p, "str")
        ref_version = c.get("version", "1")
        if not _is_nonempty_str(ref_version):
            raise malformed("成分 version 必须是非空字符串（缺省为 '1'）", f"{p}.version")
        qty = _require(c, "qty", p, "pos")
        unit = _require(c, "unit", p, "str")
        norm_comps.append(
            {
                "kind": kind,
                "code": ref_code,
                "version": ref_version,
                "qty": float(qty),
                "unit": unit,
            }
        )

    body = {
        "name": req.get("name", code),
        "yield_qty": float(yq),
        "yield_unit": yu,
        "servings": float(servings),
        "components": norm_comps,
    }
    return {
        "code": code,
        "version": version,
        "body": body,
        "body_hash": content_hash(body),
        "name": req.get("name", code),
        "yield_qty": float(yq),
        "yield_unit": yu,
        "servings": float(servings),
        "components": norm_comps,
    }


# ---- 计算请求 ---------------------------------------------------------------

def validate_compute(req):
    req = _require_body(req)
    recipe_code = _require(req, "recipe_code", "", "str")
    recipe_version = req.get("recipe_version", "1")
    if not _is_nonempty_str(recipe_version):
        raise malformed("recipe_version 必须是非空字符串", "recipe_version")
    ingredient_release = _require(req, "ingredient_release", "", "str")
    unit_version = _require(req, "unit_version", "", "str")
    rule_version = _require(req, "rule_version", "", "str")

    servings_override = req.get("servings_override")
    if servings_override is not None and (
        not _is_num(servings_override) or servings_override <= 0
    ):
        raise malformed("servings_override 必须是正数", "servings_override")

    return {
        "recipe_code": recipe_code,
        "recipe_version": recipe_version,
        "ingredient_release": ingredient_release,
        "unit_version": unit_version,
        "rule_version": rule_version,
        "servings_override": float(servings_override) if servings_override else None,
    }


# ---- 试算比较（scenarios/compare）--------------------------------------------

VALID_OPS = ("set_qty", "replace", "remove")

_PATH_SEG_RE = re.compile(
    r"\[(\d+)\](?:\((ingredient|recipe):([^@()]+)@([^()]+)\))?"
)


def parse_component_path(raw, field):
    """把嵌套组件路径解析为 [{index, kind, code, version}, ...]。

    支持三种写法（与计算结果/错误中的字段路径同格式）：
      - 规范注解串 ``components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1)``
      - 简写下标串 ``components[1][0]``
      - 下标数组 ``[1, 0]``
    注解（kind/code@version）在应用调整时会与实际成分逐一核对，不符即路径失效。
    """
    if isinstance(raw, list):
        segments = []
        for v in raw:
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise malformed("路径数组元素必须是非负整数", field)
            segments.append({"index": v, "kind": None, "code": None,
                             "version": None})
        return segments
    if not isinstance(raw, str) or not raw.startswith("components"):
        raise malformed("路径必须以 components 开头（或为非负整数数组）", field)
    rest = raw[len("components"):]
    segments = []
    while rest:
        m = _PATH_SEG_RE.match(rest)
        if not m:
            raise malformed(f"路径片段无法解析：{rest!r}", field)
        segments.append({"index": int(m.group(1)), "kind": m.group(2),
                         "code": m.group(3), "version": m.group(4)})
        rest = rest[m.end():]
    return segments


def validate_scenario_compare(req):
    """试算请求：基准配方 + 锁定版本（同计算请求）+ 调整项数组。"""
    req = _require_body(req)
    base = validate_compute(req)
    items = _require(req, "adjustments", "", "list")
    if not items:
        raise malformed("adjustments 至少包含一项", "adjustments")

    adjustments = []
    for i, a in enumerate(items):
        p = f"adjustments[{i}]"
        if not isinstance(a, dict):
            raise malformed("调整项必须是对象", p)
        op = _require(a, "op", p, "str")
        if op not in VALID_OPS:
            raise malformed(f"op 必须是 {VALID_OPS} 之一", f"{p}.op")
        segments = parse_component_path(_require(a, "path", p), f"{p}.path")
        if not segments:
            raise malformed("路径必须至少定位一个成分", f"{p}.path")
        adj = {"op": op, "segments": segments}
        if op == "set_qty":
            adj["qty"] = float(_require(a, "qty", p, "pos"))
            unit = a.get("unit")
            if unit is not None:
                if not _is_nonempty_str(unit):
                    raise malformed("unit 必须是非空字符串", f"{p}.unit")
                adj["unit"] = unit
        elif op == "replace":
            spec = _require(a, "component", p, "dict")
            kind = _require(spec, "kind", f"{p}.component", "str")
            if kind not in VALID_KINDS:
                raise malformed(f"kind 必须是 {VALID_KINDS} 之一",
                                f"{p}.component.kind")
            code = _require(spec, "code", f"{p}.component", "str")
            version = spec.get("version", "1")
            if not _is_nonempty_str(version):
                raise malformed("component.version 必须是非空字符串（缺省为 '1'）",
                                f"{p}.component.version")
            qty = spec.get("qty")
            if qty is not None and (not _is_num(qty) or qty <= 0):
                raise malformed("component.qty 必须是正数（缺省继承被替换成分）",
                                f"{p}.component.qty")
            unit = spec.get("unit")
            if unit is not None and not _is_nonempty_str(unit):
                raise malformed("component.unit 必须是非空字符串（缺省继承被替换成分）",
                                f"{p}.component.unit")
            adj["component"] = {
                "kind": kind, "code": code, "version": version,
                "qty": float(qty) if qty is not None else None,
                "unit": unit,
            }
        adjustments.append(adj)

    return {**base, "adjustments": adjustments}
