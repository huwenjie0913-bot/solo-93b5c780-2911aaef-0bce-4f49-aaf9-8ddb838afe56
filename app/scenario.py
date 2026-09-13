"""试算比较（不落库）：对基准配方按嵌套组件路径应用调整，分别推导基准与候选
标签结果并给出差异。

调整按提交顺序依次生效：后一条调整的路径基于前面调整后的状态解析
（remove 会使同层后续成分下标前移）。所有修改只作用于内存副本，
不写 recipe / computation 表，不累计命中次数，也不影响正式计算缓存。
"""

import copy
import json
import re

from . import engine
from .common import content_hash, round_half_up
from .errors import empty_recipe, invalid_path, unknown_ingredient, unknown_recipe


# ---- 调整应用 ---------------------------------------------------------------

def _recipe_body_hash(row):
    """与 validate_recipe 同形的正文哈希，用于标识试算副本的内容。"""
    return content_hash({
        "name": row["name"],
        "yield_qty": row["yield_qty"],
        "yield_unit": row["yield_unit"],
        "servings": row["servings"],
        "components": row["components"],
    })


def apply_adjustments(conn, ingredient_pack, root_code, root_version, adjustments):
    """按顺序应用调整，返回 (overrides, matched, effective)。

    overrides：{成分下标路径元组: 修改后的配方行副本}，供 expand_tree 使用；
    matched：每条调整实际命中的组件路径（含下标元组，供候选错误归因）；
    effective：补全缺省值后的调整内容（用于候选内容哈希）。
    """
    overrides = {}
    matched = []
    effective = []
    db_cache = {}

    def load(code, version):
        key = (code, version)
        if key not in db_cache:
            row = engine.load_recipe(conn, code, version)
            if row is not None:
                row["components"] = json.loads(row["components"])
            db_cache[key] = row
        return db_cache[key]

    def get_at(idx_path, code, version):
        if idx_path in overrides:
            return overrides[idx_path]
        return load(code, version)

    for i, adj in enumerate(adjustments):
        segs = adj["segments"]
        field = f"adjustments[{i}].path"
        # 逐段解析路径，核对注解并记录祖先链（出现下标路径 + 配方标识）
        ancestors = [((), root_code, root_version)]
        parent_idx = ()
        parent = get_at((), root_code, root_version)
        canonical = "components"
        for depth, seg in enumerate(segs):
            comps = parent["components"]
            idx = seg["index"]
            if idx >= len(comps):
                raise invalid_path(
                    f"调整路径第 {depth + 1} 段下标 [{idx}] 超出该层成分数量"
                    f"（共 {len(comps)} 项）",
                    field,
                    {"segment": depth, "index": idx,
                     "component_count": len(comps)},
                )
            comp = comps[idx]
            for attr in ("kind", "code", "version"):
                if seg[attr] is not None and comp[attr] != seg[attr]:
                    raise invalid_path(
                        f"调整路径第 {depth + 1} 段与实际成分不符：期望 "
                        f"{attr}={seg[attr]!r}，实际为 {comp[attr]!r}",
                        field,
                        {"segment": depth, "attribute": attr,
                         "expected": seg[attr], "actual": comp[attr]},
                    )
            canonical += (f"[{idx}]({comp['kind']}:{comp['code']}"
                          f"@{comp['version']})")
            if depth < len(segs) - 1:
                if comp["kind"] != "recipe":
                    raise invalid_path(
                        f"调整路径第 {depth + 1} 段指向原料成分 "
                        f"{comp['code']!r}，无法继续下钻",
                        field,
                        {"segment": depth, "code": comp["code"]},
                    )
                parent_idx = parent_idx + (idx,)
                nxt = get_at(parent_idx, comp["code"], comp["version"])
                if nxt is None:
                    raise invalid_path(
                        f"调整路径经过的配方 {comp['code']}@{comp['version']} 不存在",
                        field,
                        {"segment": depth, "code": comp["code"],
                         "version": comp["version"]},
                    )
                ancestors.append((parent_idx, comp["code"], comp["version"]))
                parent = nxt

        # 物化祖先链：根到目标父配方逐层复制（已复制过的跳过）
        for anc_idx, anc_code, anc_ver in ancestors:
            if anc_idx not in overrides:
                row = load(anc_code, anc_ver)
                replica = {**row, "components": copy.deepcopy(row["components"])}
                replica["body_hash"] = _recipe_body_hash(replica)
                overrides[anc_idx] = replica

        target_parent = overrides[parent_idx]
        t_idx = segs[-1]["index"]
        comp = target_parent["components"][t_idx]
        op = adj["op"]
        if op == "set_qty":
            comp["qty"] = adj["qty"]
            if adj.get("unit") is not None:
                comp["unit"] = adj["unit"]
            effective.append({"op": op, "path": canonical,
                              "qty": comp["qty"], "unit": comp["unit"]})
        elif op == "remove":
            del target_parent["components"][t_idx]
            if not target_parent["components"]:
                raise empty_recipe(
                    f"移除该成分后配方 {target_parent['code']}@"
                    f"{target_parent['version']} 不再含任何成分",
                    f"adjustments[{i}]",
                    {"recipe_code": target_parent["code"],
                     "recipe_version": target_parent["version"]},
                )
            effective.append({"op": op, "path": canonical})
        else:  # replace
            spec = adj["component"]
            new_comp = {
                "kind": spec["kind"],
                "code": spec["code"],
                "version": spec["version"],
                "qty": spec["qty"] if spec["qty"] is not None else comp["qty"],
                "unit": spec["unit"] if spec["unit"] is not None else comp["unit"],
            }
            if new_comp["kind"] == "ingredient":
                if new_comp["code"] not in ingredient_pack["ingredients"]:
                    raise unknown_ingredient(
                        f"替换引用的原料 {new_comp['code']!r} 在原料资料版本 "
                        f"{ingredient_pack['version']!r} 中不存在",
                        f"adjustments[{i}].component.code",
                        {"code": new_comp["code"],
                         "release": ingredient_pack["version"]},
                    )
            elif load(new_comp["code"], new_comp["version"]) is None:
                raise unknown_recipe(
                    f"替换引用的配方 {new_comp['code']}@{new_comp['version']} 不存在",
                    f"adjustments[{i}].component.code",
                    {"code": new_comp["code"], "version": new_comp["version"]},
                )
            target_parent["components"][t_idx] = new_comp
            effective.append({"op": op, "path": canonical,
                              "component": dict(new_comp)})

        target_parent["body_hash"] = _recipe_body_hash(target_parent)
        matched.append({
            "index": i,
            "op": op,
            "path": canonical,
            "idx_path": parent_idx + (t_idx,),
        })

    return overrides, matched, effective


# ---- 候选计算错误归因 ---------------------------------------------------------

_SEG_IDX_RE = re.compile(r"\[(\d+)\](?:\([^()]*\))?")

# replace 引入的错误，字段后缀细化到 component 之下
_REPLACE_SUFFIX = {".qty": ".component.qty", ".unit": ".component.unit",
                   ".code": ".component.code"}


def _split_field_path(field):
    """把展开期字段路径拆成 (下标路径元组, 后缀)；无法解析返回 None。"""
    if not isinstance(field, str) or not field.startswith("components"):
        return None
    rest = field[len("components"):]
    idxs = []
    while rest.startswith("["):
        m = _SEG_IDX_RE.match(rest)
        if not m:
            return None
        idxs.append(int(m.group(1)))
        rest = rest[m.end():]
    if not idxs or (rest and not rest.startswith(".")):
        return None
    return tuple(idxs), rest


def remap_error(err, matched):
    """候选计算失败时，把错误字段路径归因到引发它的 adjustments[i]。

    命中规则：错误路径的下标前缀包含某条调整的目标下标路径
    （多条命中时取最深的一条）；无法归因时原样返回。
    """
    fields = []
    if err.field:
        fields.append(err.field)
    detail_field = err.details.get("field")
    if detail_field and detail_field not in fields:
        fields.append(detail_field)

    best = None
    for f in fields:
        parsed = _split_field_path(f)
        if parsed is None:
            continue
        idxs, suffix = parsed
        for m in matched:
            target = m["idx_path"]
            if len(target) <= len(idxs) and idxs[:len(target)] == target:
                if best is None or len(target) > best[0]:
                    best = (len(target), m,
                            suffix if len(target) == len(idxs) else "", f)
    if best is None:
        return err

    _, m, suffix, orig_field = best
    if m["op"] == "replace":
        suffix = _REPLACE_SUFFIX.get(suffix, suffix)
    err.field = f"adjustments[{m['index']}]{suffix}"
    err.details = {**err.details, "component_path": orig_field}
    return err


# ---- 差异计算 -----------------------------------------------------------------

def _r(value, ndigits):
    rounded = round_half_up(value, ndigits)
    return rounded + 0.0 if rounded == 0 else rounded  # 去掉 -0.0


def nutrition_diff(rows_b, rows_c, rules_pack):
    """每份营养与 NRV% 差值（按原始值求差后按规则位数舍入）。"""
    dv_decimals = rules_pack["body"].get("dv_decimals", 0)
    map_b = {r["nutrient"]: r for r in rows_b}
    map_c = {r["nutrient"]: r for r in rows_c}
    ordered = [r["nutrient"] for r in rows_b]
    ordered += [r["nutrient"] for r in rows_c if r["nutrient"] not in map_b]
    out = []
    for name in ordered:
        rb, rc = map_b.get(name), map_c.get(name)
        ref = rb or rc
        decimals = ref["decimals"]
        dv = ref["daily_value"]
        raw_b = rb["per_serving_raw"] if rb else 0.0
        raw_c = rc["per_serving_raw"] if rc else 0.0
        pct_b = raw_b / dv * 100.0 if dv else None
        pct_c = raw_c / dv * 100.0 if dv else None
        out.append({
            "nutrient": name,
            "required": ref["required"],
            "baseline": {
                "per_serving": _r(raw_b, decimals),
                "pct_daily_value": _r(pct_b, dv_decimals) if dv else None,
            },
            "candidate": {
                "per_serving": _r(raw_c, decimals),
                "pct_daily_value": _r(pct_c, dv_decimals) if dv else None,
            },
            "delta": {
                "per_serving": _r(raw_c - raw_b, decimals),
                "pct_daily_value":
                    _r(pct_c - pct_b, dv_decimals) if dv else None,
            },
        })
    return out


def _ingredient_side(a):
    if a is None:
        return None
    return {"root_unit_qty": a["root_unit_qty"],
            "proportion_pct": a["proportion_pct"],
            "leaf_count": a["leaf_count"]}


def ingredients_diff(agg_b, agg_c):
    """原料占比变化：added / removed / changed / unchanged。"""
    map_b = {a["code"]: a for a in agg_b}
    map_c = {a["code"]: a for a in agg_c}
    ordered = [a["code"] for a in agg_b]
    ordered += [a["code"] for a in agg_c if a["code"] not in map_b]
    out = []
    for code in ordered:
        ab, ac = map_b.get(code), map_c.get(code)
        if ab is None:
            change = "added"
        elif ac is None:
            change = "removed"
        elif (ab["root_unit_qty"] != ac["root_unit_qty"]
              or ab["proportion_pct"] != ac["proportion_pct"]):
            change = "changed"
        else:
            change = "unchanged"
        pct_b = ab["proportion_pct"] if ab else 0.0
        pct_c = ac["proportion_pct"] if ac else 0.0
        ref = ab or ac
        out.append({
            "code": code,
            "name": ref["name"],
            "unit": ref["unit"],
            "change": change,
            "baseline": _ingredient_side(ab),
            "candidate": _ingredient_side(ac),
            "delta_proportion_pct": _r(pct_c - pct_b, 2),
        })
    return out


_ALLERGEN_RANK = {"may_contain": 1, "contains": 2}


def allergens_diff(all_b, all_c):
    """过敏原变化：added / removed / escalated / de-escalated / unchanged。"""
    map_b = {a["allergen"]: a for a in all_b}
    map_c = {a["allergen"]: a for a in all_c}
    out = []
    for name in sorted(set(map_b) | set(map_c)):
        eb, ec = map_b.get(name), map_c.get(name)
        if eb is None:
            change = "added"
        elif ec is None:
            change = "removed"
        elif eb["status"] == ec["status"]:
            change = "unchanged"
        elif _ALLERGEN_RANK[ec["status"]] > _ALLERGEN_RANK[eb["status"]]:
            change = "escalated"
        else:
            change = "de-escalated"
        out.append({
            "allergen": name,
            "change": change,
            "baseline": ({"status": eb["status"], "sources": eb["sources"]}
                         if eb else None),
            "candidate": ({"status": ec["status"], "sources": ec["sources"]}
                          if ec else None),
        })
    return out


# ---- 响应组装 -----------------------------------------------------------------

def _compact_rows(rows):
    return [
        {k: r[k] for k in ("nutrient", "per_serving", "pct_daily_value",
                           "required")}
        for r in rows
    ]


def build_compare_response(req, ingredient_pack, unit_pack, rules_pack,
                           root, closure_b, fingerprint_b, effective,
                           matched, agg_b, agg_c):
    candidate_hash = content_hash({
        "baseline_fingerprint": fingerprint_b,
        "adjustments": effective,
    })
    return {
        "persisted": False,
        "root": {
            "code": req["recipe_code"],
            "version": req["recipe_version"],
            "name": root["name"],
            "yield": {"qty": root["yield_qty"], "unit": root["yield_unit"]},
            "declared_servings": root["servings"],
            "servings_used": agg_b["servings"],
            "serving_yield": {"qty": agg_b["serving_yield"],
                              "unit": root["yield_unit"]},
        },
        "locked_versions": {
            "ingredient_release": {"version": ingredient_pack["version"],
                                   "body_hash": ingredient_pack["body_hash"]},
            "unit_version": {"version": unit_pack["version"],
                             "body_hash": unit_pack["body_hash"]},
            "label_rule": {"version": rules_pack["version"],
                           "body_hash": rules_pack["body_hash"]},
            "recipes": closure_b,
        },
        "baseline_fingerprint": fingerprint_b,
        "candidate_hash": candidate_hash,
        "matched_paths": [
            {"index": m["index"], "op": m["op"], "path": m["path"]}
            for m in matched
        ],
        "baseline": {
            "nutrition_per_serving": _compact_rows(agg_b["nutrition_rows"]),
            "ingredients_aggregate": agg_b["ingredients_aggregate"],
            "allergens": agg_b["allergens"],
        },
        "candidate": {
            "nutrition_per_serving": _compact_rows(agg_c["nutrition_rows"]),
            "ingredients_aggregate": agg_c["ingredients_aggregate"],
            "allergens": agg_c["allergens"],
        },
        "diff": {
            "nutrition": nutrition_diff(agg_b["nutrition_rows"],
                                        agg_c["nutrition_rows"], rules_pack),
            "ingredients": ingredients_diff(agg_b["ingredients_aggregate"],
                                            agg_c["ingredients_aggregate"]),
            "allergens": allergens_diff(agg_b["allergens"], agg_c["allergens"]),
        },
    }
