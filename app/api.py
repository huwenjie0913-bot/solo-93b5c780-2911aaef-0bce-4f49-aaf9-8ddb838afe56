"""HTTP 路由：资料/配方版本提交、标签计算、历史追溯、JSON 导出。"""

import json

from flask import Blueprint, jsonify, request

from . import engine
from . import scenario
from .db import get_conn, row_to_dict, utc_now_iso
from .errors import APIError, conflict
from .units import UnitGraph
from .validation import (
    validate_compute,
    validate_ingredients,
    validate_recipe,
    validate_rules,
    validate_scenario_compare,
    validate_units,
)

bp = Blueprint("api", __name__, url_prefix="/api")


def _json_body():
    data = request.get_json(silent=True)
    if data is None:
        raise APIError("MALFORMED", "请求体缺失或不是合法 JSON", status_code=400)
    return data


def _idempotent_get_or_conflict(conn, table, id_cols, id_values, version_field,
                                body_hash, fetch_sql):
    """同版本已存在：内容相同则幂等重放，内容变化则 409 不可变冲突。"""
    row = conn.execute(fetch_sql, id_values).fetchone()
    if row is not None:
        if row["body_hash"] == body_hash:
            return row, True
        raise conflict(
            f"{version_field} 已存在且内容不同；版本化资料不可覆盖，请使用新版本号",
            version_field,
            {"existing_body_hash": row["body_hash"], "new_body_hash": body_hash},
        )
    return None, False


# ---- 原料资料 ---------------------------------------------------------------

@bp.post("/ingredients")
def post_ingredients():
    data = validate_ingredients(_json_body())
    conn = get_conn()
    try:
        row, replayed = _idempotent_get_or_conflict(
            conn, "ingredient_release", ("release_version",),
            (data["release_version"],), "release_version", data["body_hash"],
            "SELECT * FROM ingredient_release WHERE release_version = ?",
        )
        if replayed:
            return jsonify({
                "release_version": data["release_version"],
                "body_hash": data["body_hash"],
                "replayed": True,
            }), 200
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO ingredient_release(release_version, body_hash, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (data["release_version"], data["body_hash"],
             json.dumps(data["body"], ensure_ascii=False), now),
        )
        for ing in data["ingredients"]:
            conn.execute(
                "INSERT INTO ingredient(release_version, code, name, nutrition,"
                " basis_amount, basis_unit, allergens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (data["release_version"], ing["code"], ing["name"],
                 json.dumps(ing["nutrition"], ensure_ascii=False),
                 ing["basis_amount"], ing["basis_unit"],
                 json.dumps(ing["allergens"], ensure_ascii=False)),
            )
        conn.commit()
        return jsonify({
            "release_version": data["release_version"],
            "body_hash": data["body_hash"],
            "ingredient_count": len(data["ingredients"]),
            "created_at": now,
        }), 201
    finally:
        conn.close()


@bp.get("/ingredients")
def list_ingredient_releases():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT release_version, body_hash, created_at FROM ingredient_release"
            " ORDER BY id"
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])
    finally:
        conn.close()


@bp.get("/ingredients/<release_version>")
def get_ingredient_release(release_version):
    conn = get_conn()
    try:
        pack = engine.load_ingredient_release(conn, release_version)
        return jsonify(
            {"release_version": release_version, "body_hash": pack["body_hash"],
             "ingredients": list(pack["ingredients"].values())}
        )
    finally:
        conn.close()


# ---- 单位换算表 -------------------------------------------------------------

@bp.post("/units")
def post_units():
    data = validate_units(_json_body())
    # 提交即做因子矛盾检查（含传递闭包）。换算表自身矛盾属于声明冲突 → 409。
    from .errors import APIError
    try:
        graph = UnitGraph(data["units"], data["edges"])
        graph.check_transitive_consistency()
    except APIError as err:
        if err.code == "UNIT_CONFLICT":
            err.status_code = 409
        raise

    conn = get_conn()
    try:
        row, replayed = _idempotent_get_or_conflict(
            conn, "unit_version", ("version",), (data["version"],),
            "version", data["body_hash"],
            "SELECT * FROM unit_version WHERE version = ?",
        )
        if replayed:
            return jsonify({"version": data["version"],
                            "body_hash": data["body_hash"], "replayed": True}), 200
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO unit_version(version, body_hash, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (data["version"], data["body_hash"],
             json.dumps(data["body"], ensure_ascii=False), now),
        )
        for c in data["conversions"]:
            conn.execute(
                "INSERT INTO unit_conversion(unit_version, from_unit, to_unit, factor)"
                " VALUES (?, ?, ?, ?)",
                (data["version"], c["from_unit"], c["to_unit"], c["factor"]),
            )
        conn.commit()
        return jsonify({"version": data["version"], "body_hash": data["body_hash"],
                        "conversion_count": len(data["conversions"]),
                        "created_at": now}), 201
    finally:
        conn.close()


@bp.get("/units")
def list_unit_versions():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT version, body_hash, created_at FROM unit_version ORDER BY id"
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])
    finally:
        conn.close()


@bp.get("/units/<version>")
def get_unit_version(version):
    conn = get_conn()
    try:
        pack = engine.load_unit_version(conn, version)
        return jsonify({"version": version, "body_hash": pack["body_hash"],
                        "conversions": json.loads(
                            conn.execute(
                                "SELECT body FROM unit_version WHERE version=?",
                                (version,)).fetchone()["body"])["conversions"]})
    finally:
        conn.close()


# ---- 标签规则 ---------------------------------------------------------------

@bp.post("/rules")
def post_rules():
    data = validate_rules(_json_body())
    conn = get_conn()
    try:
        row, replayed = _idempotent_get_or_conflict(
            conn, "label_rule", ("version",), (data["version"],),
            "version", data["body_hash"],
            "SELECT * FROM label_rule WHERE version = ?",
        )
        if replayed:
            return jsonify({"version": data["version"],
                            "body_hash": data["body_hash"], "replayed": True}), 200
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO label_rule(version, body_hash, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (data["version"], data["body_hash"],
             json.dumps(data["body"], ensure_ascii=False), now),
        )
        conn.commit()
        return jsonify({"version": data["version"], "body_hash": data["body_hash"],
                        "created_at": now}), 201
    finally:
        conn.close()


@bp.get("/rules")
def list_rule_versions():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT version, body_hash, created_at FROM label_rule ORDER BY id"
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])
    finally:
        conn.close()


@bp.get("/rules/<version>")
def get_rule_version(version):
    conn = get_conn()
    try:
        pack = engine.load_rules(conn, version)
        return jsonify({"version": version, "body_hash": pack["body_hash"],
                        **pack["body"]})
    finally:
        conn.close()


# ---- 配方 -------------------------------------------------------------------

@bp.post("/recipes")
def post_recipe():
    data = validate_recipe(_json_body())
    conn = get_conn()
    try:
        row, replayed = _idempotent_get_or_conflict(
            conn, "recipe", ("code", "version"),
            (data["code"], data["version"]), "version", data["body_hash"],
            "SELECT * FROM recipe WHERE code = ? AND version = ?",
        )
        if replayed:
            return jsonify({"code": data["code"], "version": data["version"],
                            "body_hash": data["body_hash"], "replayed": True}), 200
        now = utc_now_iso()
        conn.execute(
            "INSERT INTO recipe(code, version, body_hash, name, yield_qty,"
            " yield_unit, servings, components, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["code"], data["version"], data["body_hash"], data["name"],
             data["yield_qty"], data["yield_unit"], data["servings"],
             json.dumps(data["components"], ensure_ascii=False), now),
        )
        conn.commit()
        return jsonify({"code": data["code"], "version": data["version"],
                        "body_hash": data["body_hash"], "created_at": now}), 201
    finally:
        conn.close()


@bp.get("/recipes")
def list_recipes():
    code = request.args.get("code")
    conn = get_conn()
    try:
        if code:
            rows = conn.execute(
                "SELECT code, version, body_hash, name, created_at FROM recipe"
                " WHERE code = ? ORDER BY id", (code,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT code, version, body_hash, name, created_at FROM recipe"
                " ORDER BY id"
            ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])
    finally:
        conn.close()


@bp.get("/recipes/<code>/<version>")
def get_recipe(code, version):
    conn = get_conn()
    try:
        row = engine.load_recipe(conn, code, version)
        if row is None:
            from .errors import not_found
            raise not_found(f"配方 {code}@{version} 不存在", "recipe_version")
        row["components"] = json.loads(row["components"])
        row.pop("id", None)
        return jsonify(row)
    finally:
        conn.close()


# ---- 标签计算 ---------------------------------------------------------------

@bp.post("/computations")
def compute():
    req = validate_compute(_json_body())
    conn = get_conn()
    try:
        ingredient_pack = engine.load_ingredient_release(conn, req["ingredient_release"])
        unit_pack = engine.load_unit_version(conn, req["unit_version"])
        rules_pack = engine.load_rules(conn, req["rule_version"])

        tree, leaves, boundaries, closure_rows, meta = engine.expand_tree(
            conn, unit_pack, ingredient_pack,
            req["recipe_code"], req["recipe_version"],
        )
        engine.check_declarations(leaves, rules_pack)
        root = meta["root"]
        agg = engine.aggregate(
            leaves, root, req["servings_override"], rules_pack, meta
        )
        fingerprint, fp_payload = engine.build_fingerprint(
            req, req["recipe_code"], req["recipe_version"], agg["servings"],
            closure_rows, ingredient_pack, unit_pack, rules_pack,
        )

        # 请求指纹复用
        existing = conn.execute(
            "SELECT * FROM computation WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing is not None:
            conn.execute("UPDATE computation SET hits = hits + 1 WHERE id = ?",
                         (existing["id"],))
            conn.commit()
            cached_body = json.loads(existing["result"])
            cached_body["computation_id"] = existing["id"]
            cached_body["cached"] = True
            cached_body["hits"] = existing["hits"] + 1
            cached_body["export_url"] = f"/api/computations/{existing['id']}/export"
            return jsonify(cached_body)

        now = utc_now_iso()
        compact = {
            "fingerprint": fingerprint,
            "cached": False,
            "created_at": now,
            "root": {
                "code": req["recipe_code"],
                "version": req["recipe_version"],
                "name": root["name"],
                "yield": {"qty": root["yield_qty"], "unit": root["yield_unit"]},
                "declared_servings": root["servings"],
                "servings_used": agg["servings"],
                "serving_yield": {
                    "qty": agg["serving_yield"], "unit": root["yield_unit"]},
            },
            "locked_versions": {
                "ingredient_release": req["ingredient_release"],
                "unit_version": req["unit_version"],
                "rule_version": req["rule_version"],
                "recipes": [
                    {"code": c["code"], "version": c["version"], "depth": c["depth"]}
                    for c in closure_rows
                ],
            },
            "nutrition_per_serving": [
                {k: r[k] for k in (
                    "nutrient", "per_serving", "pct_daily_value", "required")}
                for r in agg["nutrition_rows"]
            ],
            "ingredients_aggregate": agg["ingredients_aggregate"],
            "allergens": agg["allergens"],
        }

        cur = conn.execute(
            "INSERT INTO computation(fingerprint, request, result, export_doc,"
            " recipe_code, recipe_version, created_at, hits)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (fingerprint, json.dumps(fp_payload, ensure_ascii=False),
             json.dumps(compact, ensure_ascii=False), "",  # 导出文档在取回 id 后补写
             req["recipe_code"], req["recipe_version"], now),
        )
        comp_id = cur.lastrowid
        export_doc = engine.build_export(
            comp_id, fingerprint, False, now, req, ingredient_pack, unit_pack,
            rules_pack, req["recipe_code"], req["recipe_version"], root, tree,
            leaves, boundaries, closure_rows, agg, meta,
        )
        conn.execute("UPDATE computation SET export_doc = ? WHERE id = ?",
                     (json.dumps(export_doc, ensure_ascii=False), comp_id))
        for c in closure_rows:
            conn.execute(
                "INSERT INTO computation_recipe(computation_id, recipe_code,"
                " recipe_version, depth) VALUES (?, ?, ?, ?)",
                (comp_id, c["code"], c["version"], c["depth"]),
            )
        conn.commit()
        compact["computation_id"] = comp_id
        compact["export_url"] = f"/api/computations/{comp_id}/export"
        return jsonify(compact), 201
    finally:
        conn.close()


@bp.get("/computations/<int:comp_id>")
def get_computation(comp_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM computation WHERE id = ?",
                           (comp_id,)).fetchone()
        if row is None:
            from .errors import not_found
            raise not_found(f"计算记录 {comp_id} 不存在", "computation_id")
        body = json.loads(row["result"])
        body["computation_id"] = comp_id
        body["hits"] = row["hits"]
        body["export_url"] = f"/api/computations/{comp_id}/export"
        body["cached"] = True
        return jsonify(body)
    finally:
        conn.close()


@bp.get("/computations/fingerprint/<fingerprint>")
def get_by_fingerprint(fingerprint):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM computation WHERE fingerprint = ?",
                           (fingerprint,)).fetchone()
        if row is None:
            from .errors import not_found
            raise not_found("该请求指纹尚无缓存计算结果", "fingerprint")
        return jsonify({"fingerprint": fingerprint, "computation_id": row["id"],
                        "url": f"/api/computations/{row['id']}"})
    finally:
        conn.close()


@bp.get("/computations/<int:comp_id>/export")
def export_computation(comp_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM computation WHERE id = ?",
                           (comp_id,)).fetchone()
        if row is None:
            from .errors import not_found
            raise not_found(f"计算记录 {comp_id} 不存在", "computation_id")
        return jsonify(json.loads(row["export_doc"]))
    finally:
        conn.close()


# ---- 试算比较（不落库）-------------------------------------------------------

@bp.post("/scenarios/compare")
def scenario_compare():
    """临时换料试算：对基准配方应用调整项，分别推导基准与候选标签并比较。

    全程只读：不写 recipe / computation 表，不累计命中次数，
    也不影响正式计算的指纹缓存。
    """
    req = validate_scenario_compare(_json_body())
    conn = get_conn()
    try:
        ingredient_pack = engine.load_ingredient_release(
            conn, req["ingredient_release"])
        unit_pack = engine.load_unit_version(conn, req["unit_version"])
        rules_pack = engine.load_rules(conn, req["rule_version"])

        # 基准：与正式计算完全同口径（纯读取）
        _, leaves_b, _, closure_b, meta_b = engine.expand_tree(
            conn, unit_pack, ingredient_pack,
            req["recipe_code"], req["recipe_version"],
        )
        engine.check_declarations(leaves_b, rules_pack)
        agg_b = engine.aggregate(leaves_b, meta_b["root"],
                                 req["servings_override"], rules_pack, meta_b)
        fingerprint_b, _ = engine.build_fingerprint(
            req, req["recipe_code"], req["recipe_version"], agg_b["servings"],
            closure_b, ingredient_pack, unit_pack, rules_pack,
        )

        # 应用调整（内存副本；路径失效/替换引用不存在在此报 4xx）
        overrides, matched, effective = scenario.apply_adjustments(
            conn, ingredient_pack, req["recipe_code"], req["recipe_version"],
            req["adjustments"],
        )

        # 候选：同口径展开 + 声明校验；失败归因到 adjustments[i]
        try:
            _, leaves_c, _, _, meta_c = engine.expand_tree(
                conn, unit_pack, ingredient_pack,
                req["recipe_code"], req["recipe_version"],
                recipe_overrides=overrides,
            )
            engine.check_declarations(leaves_c, rules_pack)
        except APIError as err:
            raise scenario.remap_error(err, matched) from err
        agg_c = engine.aggregate(leaves_c, meta_c["root"],
                                 req["servings_override"], rules_pack, meta_c)

        return jsonify(scenario.build_compare_response(
            req, ingredient_pack, unit_pack, rules_pack, meta_b["root"],
            closure_b, fingerprint_b, effective, matched, agg_b, agg_c,
        )), 200
    finally:
        conn.close()


@bp.get("/recipes/<code>/<version>/history")
def recipe_history(code, version):
    """按配方追溯计算历史（含作为子配方参与的计算）。"""
    conn = get_conn()
    try:
        exists = engine.load_recipe(conn, code, version)
        if exists is None:
            from .errors import not_found
            raise not_found(f"配方 {code}@{version} 不存在", "recipe_version")
        rows = conn.execute(
            "SELECT c.id AS computation_id, c.fingerprint, c.created_at, c.hits,"
            "       c.recipe_code AS root_code, c.recipe_version AS root_version,"
            "       cr.depth"
            " FROM computation_recipe cr JOIN computation c ON c.id = cr.computation_id"
            " WHERE cr.recipe_code = ? AND cr.recipe_version = ?"
            " ORDER BY c.id",
            (code, version),
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])
    finally:
        conn.close()
