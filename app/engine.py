"""标签推导引擎：加载锁定版本 → 递归展开成分树 → 营养与过敏原推导 → 指纹。"""

import json

from .common import content_hash, round_half_up
from .errors import (
    circular,
    missing_declaration,
    not_found,
    unknown_ingredient,
    unknown_recipe,
)
from .units import UnitGraph


# ---- 版本化资料加载 ---------------------------------------------------------

def load_ingredient_release(conn, release_version):
    row = conn.execute(
        "SELECT * FROM ingredient_release WHERE release_version = ?", (release_version,)
    ).fetchone()
    if row is None:
        raise not_found(
            f"原料资料版本 {release_version!r} 不存在",
            "ingredient_release",
            {"release_version": release_version},
        )
    ingredients = {}
    for ing in conn.execute(
        "SELECT * FROM ingredient WHERE release_version = ?", (release_version,)
    ).fetchall():
        d = dict(ing)
        d["nutrition"] = json.loads(d["nutrition"])
        d["allergens"] = json.loads(d["allergens"])
        ingredients[d["code"]] = d
    return {"version": release_version, "body_hash": row["body_hash"], "ingredients": ingredients}


def load_unit_version(conn, version):
    row = conn.execute("SELECT * FROM unit_version WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise not_found(
            f"单位换算表版本 {version!r} 不存在",
            "unit_version",
            {"version": version},
        )
    units = set()
    edges = []
    for c in conn.execute(
        "SELECT * FROM unit_conversion WHERE unit_version = ?", (version,)
    ).fetchall():
        units.update((c["from_unit"], c["to_unit"]))
        edges.append((c["from_unit"], c["to_unit"], c["factor"]))
    graph = UnitGraph(units, edges)
    graph.check_transitive_consistency()
    return {"version": version, "body_hash": row["body_hash"], "graph": graph}


def load_rules(conn, version):
    row = conn.execute("SELECT * FROM label_rule WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise not_found(
            f"标签规则版本 {version!r} 不存在", "rule_version", {"version": version}
        )
    body = json.loads(row["body"])
    return {"version": version, "body_hash": row["body_hash"], "body": body}


def load_recipe(conn, code, version):
    row = conn.execute(
        "SELECT * FROM recipe WHERE code = ? AND version = ?", (code, version)
    ).fetchone()
    return dict(row) if row else None


# ---- 递归展开 ---------------------------------------------------------------

def _seg(i, comp):
    return f"[{i}]({comp['kind']}:{comp['code']}@{comp['version']})"


def expand_tree(conn, unit_pack, ingredient_pack, root_code, root_version,
                recipe_overrides=None):
    """深度优先展开配方树。

    返回 (tree, leaves, boundaries, recipe_closure, meta)：
      - tree：可导出的成分树（含逐层缩放系数与换算审计路径）
      - leaves：每个原料出现一次的平铺项（含营养贡献）
      - boundaries：跨配方边界的缩放记录（计算依据）
      - recipe_closure：[(code, version, body_hash, depth)]，根配方 depth=0

    recipe_overrides（试算专用）：{成分下标路径元组: 配方行字典}，
    如 {(1,): 修改后的 SAUCE 副本}。命中时以覆盖内容展开该次出现而不查库；
    同一 (code, version) 在其他位置的出现不受影响。
    """
    recipes_cache = {}
    closure = {}
    leaves = []
    boundaries = []
    conversions_used = []

    def get_recipe(code, version, idx_path):
        if recipe_overrides is not None and idx_path in recipe_overrides:
            return recipe_overrides[idx_path]
        key = (code, version)
        if key not in recipes_cache:
            row = load_recipe(conn, code, version)
            if row is None:
                return None
            row["components"] = json.loads(row["components"])
            recipes_cache[key] = row
        return recipes_cache[key]

    root = get_recipe(root_code, root_version, ())
    if root is None:
        raise not_found(
            f"配方 {root_code}@{root_version} 不存在",
            "recipe_version",
            {"code": root_code, "version": root_version},
        )

    graph = unit_pack["graph"]
    ingredients = ingredient_pack["ingredients"]

    def dfs(code, version, ref_qty, ref_unit, chain, stack, depth, is_root,
            idx_path):
        key = (code, version)
        stack_keys = set(stack)
        if key in stack_keys:
            start = next(i for i, k in enumerate(stack) if k == key)
            cyc = [f"{c}@{v}" for c, v in stack[start:]] + [f"{code}@{version}"]
            raise circular(
                f"配方存在循环引用：{' -> '.join(cyc)}",
                {"cycle": cyc, "field": "components" + chain + ".code"},
            )
        recipe = get_recipe(code, version, idx_path)
        if recipe is None:
            raise unknown_recipe(
                f"未知配方引用 {code}@{version}",
                "components" + chain + ".code",
                {"code": code, "version": version},
            )
        closure[(code, version, idx_path)] = (depth, recipe)

        # 引用数量先换算到配方产量单位，再除以产量得到缩放系数
        if is_root:
            in_yield_qty, ref_path_units = ref_qty, [ref_unit]
        else:
            in_yield_qty, ref_path_units = graph.convert(
                ref_qty, ref_unit, recipe["yield_unit"],
                "components" + chain + ".unit",
            )
            conversions_used.append(
                {"field": "components" + chain, "from": ref_unit,
                 "to": recipe["yield_unit"], "path": ref_path_units}
            )
        scale = in_yield_qty / recipe["yield_qty"]
        node = {
            "kind": "recipe",
            "code": code,
            "version": version,
            "name": recipe["name"],
            "path": "components" + chain if chain else "components",
            "referenced_qty": ref_qty,
            "referenced_unit": ref_unit,
            "referenced_in_yield_unit": in_yield_qty,
            "recipe_yield": {"qty": recipe["yield_qty"], "unit": recipe["yield_unit"]},
            "conversion_path": ref_path_units,
            "scale_factor": scale,
            "children": [],
        }
        if not is_root:
            boundaries.append(
                {
                    "path": "components" + chain,
                    "recipe": {"code": code, "version": version},
                    "referenced_qty": ref_qty,
                    "referenced_unit": ref_unit,
                    "conversion_path": ref_path_units,
                    "in_yield_unit_qty": in_yield_qty,
                    "yield_qty": recipe["yield_qty"],
                    "yield_unit": recipe["yield_unit"],
                    "scale_factor": scale,
                }
            )

        stack.append(key)
        for i, comp in enumerate(recipe["components"]):
            seg = _seg(i, comp)
            eff_qty = comp["qty"] * scale
            field = "components" + chain + seg
            if comp["kind"] == "recipe":
                child = dfs(
                    comp["code"], comp["version"], eff_qty, comp["unit"],
                    chain + seg, stack, depth + 1, False, idx_path + (i,),
                )
                node["children"].append(child)
            else:
                ing = ingredients.get(comp["code"])
                if ing is None:
                    raise unknown_ingredient(
                        f"未知原料引用 {comp['code']!r}（原料资料版本 "
                        f"{ingredient_pack['version']!r}）",
                        field + ".code",
                        {"code": comp["code"], "release": ingredient_pack["version"]},
                    )
                # 营养口径：把出现量换算到原料声明基准单位
                basis_qty, basis_path = graph.convert(
                    eff_qty, comp["unit"], ing["basis_unit"], field + ".unit"
                )
                conversions_used.append(
                    {"field": field, "from": comp["unit"], "to": ing["basis_unit"],
                     "path": basis_path}
                )
                factor_to_basis = basis_qty / ing["basis_amount"]
                contribution = {
                    n: v * factor_to_basis for n, v in ing["nutrition"].items()
                }
                # 占比口径：出现量必须能换算到根配方产量单位。
                # 可换算到营养基准但换算不到根单位（如质量 g → 体积 ml）时
                # 属于单位冲突：硬失败，不写计算记录，也不产生 null 占比。
                root_unit_qty, root_path = graph.convert(
                    eff_qty, comp["unit"], root["yield_unit"], field + ".unit"
                )
                conversions_used.append(
                    {"field": field, "from": comp["unit"],
                     "to": root["yield_unit"], "path": root_path}
                )

                leaf = {
                    "kind": "ingredient",
                    "code": ing["code"],
                    "release_version": ingredient_pack["version"],
                    "name": ing["name"],
                    "category": ing["category"],
                    "path": field,
                    "depth": depth + 1,
                    "effective_qty": eff_qty,
                    "effective_unit": comp["unit"],
                    "basis_qty": basis_qty,
                    "basis_unit": ing["basis_unit"],
                    "factor_to_basis": factor_to_basis,
                    "root_unit_qty": root_unit_qty,
                    "nutrition_contribution": contribution,
                    "allergens": ing["allergens"],
                }
                leaves.append(leaf)
                node["children"].append(leaf)
        stack.pop()
        return node

    tree = dfs(root_code, root_version, root["yield_qty"], root["yield_unit"],
               "", [], 0, True, ())
    if recipe_overrides is None:
        # 正式计算：同一 (code, version) 内容唯一，按配方去重（保持既有口径）
        collapsed = {}
        for (c, v, _p), (d, row) in closure.items():
            collapsed[(c, v)] = (d, row)
        closure_rows = [
            {"code": c, "version": v, "body_hash": row["body_hash"], "depth": d}
            for (c, v), (d, row) in sorted(collapsed.items(),
                                           key=lambda kv: (kv[1][0], kv[0]))
        ]
    else:
        # 试算：同一配方的不同出现可能内容不同（被覆盖），按出现路径逐条列出
        closure_rows = [
            {"code": c, "version": v, "body_hash": row["body_hash"], "depth": d,
             "occurrence": list(p)}
            for (c, v, p), (d, row) in sorted(
                closure.items(),
                key=lambda kv: (kv[1][0], kv[0][2], kv[0][0], kv[0][1]))
        ]
    meta = {
        "root": root,
        "conversions_used": _dedupe_conversions(conversions_used),
    }
    return tree, leaves, boundaries, closure_rows, meta


def _dedupe_conversions(used):
    out = {}
    for u in used:
        if u["from"] == u["to"]:
            continue  # 同单位空转换不进入审计
        key = (u["from"], u["to"], tuple(u["path"]))
        out.setdefault(key, u)
    return list(out.values())


# ---- 声明核查（缺失营养素 / 过敏原声明）-------------------------------------

def check_declarations(leaves, rules):
    required_nutrients = [n["name"] for n in rules["body"]["required_nutrients"]]
    required_allergens = rules["body"].get("required_allergens", [])

    missing = {}
    for leaf in leaves:
        for n in required_nutrients:
            if n not in leaf["nutrition_contribution"]:
                missing.setdefault(("nutrition", n), []).append(leaf)
        for a in required_allergens:
            if a not in leaf["allergens"]:
                missing.setdefault(("allergen", a), []).append(leaf)

    if missing:
        kind, name = next(iter(missing.keys()))
        affected = missing[(kind, name)]
        first = affected[0]
        if kind == "nutrition":
            msg = f"原料 {first['code']!r} 缺少必报营养素 {name!r} 的声明"
            field = f"{first['path']}.nutrition.{name}"
        else:
            msg = f"原料 {first['code']!r} 缺少过敏原 {name!r} 的声明（contains/may_contain/free 三选一）"
            field = f"{first['path']}.allergens.{name}"
        raise missing_declaration(
            msg, field,
            {
                "missing": name,
                "kind": kind,
                "occurrences": [
                    {"code": l["code"], "path": l["path"]} for l in affected
                ],
            },
        )


# ---- 营养声明判定（按锁定规则版本逐条比较每份值与阈值）------------------------

def evaluate_claims(nutrition_rows, rules_pack):
    """对规则版本中的每条营养声明给出判定结果。

    比较口径：每份实际值先按该营养素在规则中的位数舍入（与标签展示值一致），
    再与阈值比较；lte 要求 ≤ 阈值，gte 要求 ≥ 阈值。结果携带实际值、阈值、
    单位、差值（实际 − 阈值，按同位数舍入）与文字版判定依据（含规则版本），
    供审核人员核对声明是否达标、采用了哪一版法规阈值、相差多少。
    """
    claims = rules_pack["body"].get("claims", [])
    if not claims:
        return []
    rows = {r["nutrient"]: r for r in nutrition_rows}
    results = []
    for c in claims:
        # 规则校验已保证 nutrient ∈ required_nutrients，每份行必然存在
        row = rows[c["nutrient"]]
        actual = row["per_serving"]
        threshold = c["threshold"]
        if c["direction"] == "lte":
            passed = actual <= threshold
            op = "≤"
        else:
            passed = actual >= threshold
            op = "≥"
        delta = round_half_up(actual - threshold, row["decimals"])
        if delta == 0:
            delta += 0.0  # 去掉 -0.0
        results.append({
            "name": c["name"],
            "nutrient": c["nutrient"],
            "direction": c["direction"],
            "threshold": threshold,
            "unit": c["unit"],
            "actual": actual,
            "actual_raw": row["per_serving_raw"],
            "delta": delta,
            "passed": passed,
            "rule_version": rules_pack["version"],
            "basis": (
                f"每份 {c['nutrient']} 实际值 {actual} {c['unit']}"
                f"（未舍入 {row['per_serving_raw']}，按规则保留 "
                f"{row['decimals']} 位）{op} 阈值 {threshold} {c['unit']}"
                f"（规则版本 {rules_pack['version']}）"
                f" → {'达标' if passed else '不达标'}"
            ),
        })
    return results


# ---- 汇总、份量与占比 -------------------------------------------------------

def aggregate(leaves, root, servings_override, rules, meta):
    # 营养总量（整批产量口径）
    totals = {}
    for leaf in leaves:
        for n, v in leaf["nutrition_contribution"].items():
            totals[n] = totals.get(n, 0.0) + v

    servings = servings_override or root["servings"]
    serving_yield = root["yield_qty"] / servings

    rule_nutrients = {n["name"]: n for n in rules["body"]["required_nutrients"]}
    dv_decimals = rules["body"].get("dv_decimals", 0)

    ordered_names = [n["name"] for n in rules["body"]["required_nutrients"]]
    ordered_names += sorted(n for n in totals if n not in rule_nutrients)

    nutrition_rows = []
    for name in ordered_names:
        raw_total = totals.get(name, 0.0)
        raw_serving = raw_total / servings
        spec = rule_nutrients.get(name)
        decimals = spec["decimals"] if spec else 1
        dv = spec["daily_value"] if spec else None
        pct = round_half_up(raw_serving / dv * 100.0, dv_decimals) if dv else None
        nutrition_rows.append(
            {
                "nutrient": name,
                "per_serving": round_half_up(raw_serving, decimals),
                "per_serving_raw": raw_serving,
                "total_raw": raw_total,
                "decimals": decimals,
                "daily_value": dv,
                "pct_daily_value": pct,
                "required": spec is not None,
            }
        )

    # 原料维度占比（出现量均已换算到根产量单位；换算不到的在展开期即 4xx）
    agg = {}
    agg_order = []
    for leaf in leaves:
        a = agg.get(leaf["code"])
        if a is None:
            a = {
                "code": leaf["code"],
                "release_version": leaf["release_version"],
                "name": leaf["name"],
                "leaf_count": 0,
                "root_unit_qty": 0.0,
                "unit": root["yield_unit"],
            }
            agg[leaf["code"]] = a
            agg_order.append(leaf["code"])
        a["leaf_count"] += 1
        a["root_unit_qty"] += leaf["root_unit_qty"]

    ingredients_aggregate = []
    for code in agg_order:
        a = agg[code]
        qty = a["root_unit_qty"]
        ingredients_aggregate.append(
            {
                **a,
                "root_unit_qty": round_half_up(qty, 4),
                "proportion_pct":
                    round_half_up(qty / root["yield_qty"] * 100.0, 2),
            }
        )

    # 过敏原汇总：保留每项过敏原的全部来源路径
    allergen_map = {}
    rank = {"contains": 0, "may_contain": 1}
    for leaf in leaves:
        for name, status in leaf["allergens"].items():
            if status == "free":
                continue
            entry = allergen_map.setdefault(
                name,
                {"allergen": name, "status": status,
                 "sources": []},
            )
            entry["sources"].append(
                {
                    "code": leaf["code"],
                    "release_version": leaf["release_version"],
                    "name": leaf["name"],
                    "status": status,
                    "path": leaf["path"],
                    "effective_qty": leaf["effective_qty"],
                    "unit": leaf["effective_unit"],
                }
            )
            if rank[status] < rank[entry["status"]]:
                entry["status"] = status
    allergens = sorted(allergen_map.values(), key=lambda e: e["allergen"])
    for entry in allergens:
        entry["sources"].sort(key=lambda s: s["path"])

    return {
        "servings": servings,
        "serving_yield": serving_yield,
        "totals": totals,
        "nutrition_rows": nutrition_rows,
        "ingredients_aggregate": ingredients_aggregate,
        "allergens": allergens,
        "claims": evaluate_claims(nutrition_rows, rules),
    }


# ---- 指纹 -------------------------------------------------------------------

def build_fingerprint(req, root_code, root_version, servings, closure_rows,
                      ingredient_pack, unit_pack, rules_pack):
    payload = {
        "request": {
            "recipe_code": root_code,
            "recipe_version": root_version,
            "servings_override": req["servings_override"],
            "servings_used": servings,
            "ingredient_release": ingredient_pack["version"],
            "unit_version": unit_pack["version"],
            "rule_version": rules_pack["version"],
            "ingredient_list_rules": rules_pack["body"].get(
                "ingredient_list", {}),
        },
        "locked": {
            "ingredient_release_hash": ingredient_pack["body_hash"],
            "unit_version_hash": unit_pack["body_hash"],
            "rule_version_hash": rules_pack["body_hash"],
            "recipes": sorted(
                [c["code"], c["version"], c["body_hash"]] for c in closure_rows
            ),
        },
    }
    return content_hash(payload), payload


# ---- 导出文档（含计算依据）--------------------------------------------------

def build_export(comp_id, fingerprint, cached, created_at, req,
                 ingredient_pack, unit_pack, rules_pack,
                 root_code, root_version, root, tree, leaves, boundaries,
                 closure_rows, agg, meta, label_composition=None):
    summary_nutrition = [
        {
            "nutrient": r["nutrient"],
            "per_serving": r["per_serving"],
            "pct_daily_value": r["pct_daily_value"],
        }
        for r in agg["nutrition_rows"]
    ]
    return {
        "computation_id": comp_id,
        "fingerprint": fingerprint,
        "cached": cached,
        "created_at": created_at,
        "request": req,
        "locked_versions": {
            "ingredient_release": {
                "version": ingredient_pack["version"],
                "body_hash": ingredient_pack["body_hash"],
            },
            "unit_version": {"version": unit_pack["version"],
                             "body_hash": unit_pack["body_hash"]},
            "label_rule": {"version": rules_pack["version"],
                           "body_hash": rules_pack["body_hash"]},
            "recipes": closure_rows,
        },
        "root": {
            "code": root_code,
            "version": root_version,
            "name": root["name"],
            "yield": {"qty": root["yield_qty"], "unit": root["yield_unit"]},
            "declared_servings": root["servings"],
            "servings_used": agg["servings"],
            "serving_yield": {"qty": agg["serving_yield"], "unit": root["yield_unit"]},
        },
        "ingredient_tree": tree,
        "leaves": leaves,
        "boundaries": boundaries,
        "conversion_paths_used": meta["conversions_used"],
        "totals_per_batch_raw": agg["totals"],
        "nutrition_per_serving": agg["nutrition_rows"],
        "ingredients_aggregate": agg["ingredients_aggregate"],
        "allergens": agg["allergens"],
        "claims": agg["claims"],
        "ingredient_list": label_composition,
        "label_rules_applied": rules_pack["body"],
        "calculation_basis": (
            "1) 每个原料出现量 = 配方声明用量 × 沿途各配方 (引用量/配方产量) 缩放系数连乘；"
            "2) 营养贡献 = 出现量换算到基准单位后 ÷ 基准量 × 基准营养值；"
            "3) 每份营养 = 全部出现营养贡献之和 ÷ 份数（可被 servings_override 覆盖）；"
            "4) NRV% = 每份营养 ÷ 标签规则 daily_value × 100；"
            "5) 原料占比 = 出现量换算到根配方产量单位之和 ÷ 根产量 × 100，"
            "出现量无法换算到根产量单位（量纲不通）时整体按 UNIT_CONFLICT 失败；"
            "6) 过敏原按出现路径逐项上卷，contains 优先于 may_contain，free 不上标签；"
            "7) 营养声明 = 按锁定规则版本 claims 逐条判定：每份实际值按规则位数舍入后"
            "与每份阈值比较，lte 要求 ≤ 阈值、gte 要求 ≥ 阈值，"
            "判定依据见各声明的 basis；"
            "8) 配料表编排（ingredient_list）= 各层叶子出现量换算到根产量单位后"
            "递归计算成品占比，同一展示层级同码原料合并并保留全部来源路径后降序；"
            "parenthesize 模式保留复合配料括号结构（低于复合展开阈值时折叠，"
            "括号内仅留强制添加剂），expand 模式完全展开；低于辅料省略阈值的辅料"
            "按规则省略，食品添加剂与 mandatory_additives 不受阈值限制，"
            "省略复合配料时其强制添加剂上提父展示层级，逐项依据见 "
            "ingredient_list.items/omitted 的 reason。"
        ),
        "label_summary": summary_nutrition,
    }
