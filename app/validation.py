"""请求载荷校验与规范化。

校验只负责“形状与取值是否合法”，引用是否存在、循环、单位可达性等
语义问题交给计算引擎在展开时处理并给出带引用链的字段路径。
"""

import numbers

from .errors import malformed
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

    dv_decimals = req.get("dv_decimals", DEFAULT_DV_DECIMALS)
    if not isinstance(dv_decimals, int) or isinstance(dv_decimals, bool) or dv_decimals < 0:
        raise malformed("dv_decimals 必须是非负整数", "dv_decimals")

    body = {
        "required_nutrients": nutrients,
        "required_allergens": required_allergens,
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
