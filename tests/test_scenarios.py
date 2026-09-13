"""试算比较（POST /api/scenarios/compare）端到端测试。

覆盖：三类调整、嵌套路径、出现级覆盖、差异计算、错误归因到
adjustments[i]，以及"不落库、不影响正式计算缓存"的保证。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_conn, init_db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("LABEL_DB_PATH", db_path)
    init_db(get_conn())  # 建表（连接读取环境变量）
    app = create_app(init_database=False)
    with app.test_client() as c:
        yield c


INGREDIENTS = {
    "release_version": "ing-1",
    "ingredients": [
        {"code": "TOMATO", "name": "番茄", "basis_unit": "g",
         "nutrition": {"energy_kcal": 20, "protein": 1.0, "sodium_mg": 5},
         "allergens": {"gluten": "free", "milk": "free"}},
        {"code": "PASTA", "name": "意面", "basis_unit": "g",
         "nutrition": {"energy_kcal": 350, "protein": 13, "sodium_mg": 1},
         "allergens": {"gluten": "contains", "egg": "free", "milk": "free"}},
        {"code": "BASIL", "name": "罗勒", "basis_unit": "g",
         "nutrition": {"energy_kcal": 23, "protein": 3.0, "sodium_mg": 4},
         "allergens": {"gluten": "free", "milk": "free"}},
        {"code": "NUT_PESTO", "name": "坚果青酱", "basis_unit": "g",
         "nutrition": {"energy_kcal": 400, "protein": 5, "sodium_mg": 600},
         "allergens": {"tree_nut": "contains", "milk": "may_contain",
                       "gluten": "free"}},
    ],
}

UNITS = {
    "version": "u-1",
    "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "portion", "to_unit": "g", "factor": 100},
    ],
}

RULES = {
    "version": "r-1",
    "required_nutrients": [
        {"name": "energy_kcal", "daily_value": 2000, "decimals": 0},
        {"name": "protein", "daily_value": 50, "decimals": 1},
        {"name": "sodium_mg", "daily_value": 2000, "decimals": 0},
    ],
    "required_allergens": ["gluten", "milk"],
}

SAUCE = {
    "code": "SAUCE", "version": "1", "name": "番茄酱",
    "yield": {"qty": 1000, "unit": "g"}, "servings": 10,
    "components": [
        {"kind": "ingredient", "code": "TOMATO", "qty": 600, "unit": "g"},
        {"kind": "ingredient", "code": "BASIL", "qty": 0.01, "unit": "kg"},
    ],
}

MEAL = {
    "code": "MEAL", "version": "1", "name": "意面套餐",
    "yield": {"qty": 2000, "unit": "g"}, "servings": 4,
    "components": [
        {"kind": "ingredient", "code": "PASTA", "qty": 0.8, "unit": "kg"},
        {"kind": "recipe", "code": "SAUCE", "version": "1",
         "qty": 10, "unit": "portion"},
    ],
}

BASE_REQ = {"recipe_code": "MEAL", "recipe_version": "1",
            "ingredient_release": "ing-1", "unit_version": "u-1",
            "rule_version": "r-1"}

TOMATO_IN_SAUCE = "components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1)"
BASIL_IN_SAUCE = "components[1](recipe:SAUCE@1)[1](ingredient:BASIL@1)"
PASTA_AT_ROOT = "components[0](ingredient:PASTA@1)"


def seed(client):
    assert client.post("/api/ingredients", json=INGREDIENTS).status_code == 201
    assert client.post("/api/units", json=UNITS).status_code == 201
    assert client.post("/api/rules", json=RULES).status_code == 201
    assert client.post("/api/recipes", json=SAUCE).status_code == 201
    assert client.post("/api/recipes", json=MEAL).status_code == 201


def _db_counts():
    conn = get_conn()
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("recipe", "computation", "computation_recipe")
        }
    finally:
        conn.close()


# ---- 正常路径 -----------------------------------------------------------------

def test_compare_happy_path(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": TOMATO_IN_SAUCE, "qty": 300},
        {"op": "replace", "path": PASTA_AT_ROOT,
         "component": {"kind": "ingredient", "code": "NUT_PESTO",
                       "qty": 0.8, "unit": "kg"}},
        {"op": "remove", "path": BASIL_IN_SAUCE},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    assert body["persisted"] is False
    assert body["baseline_fingerprint"]
    assert body["candidate_hash"]
    assert body["candidate_hash"] != body["baseline_fingerprint"]
    assert body["locked_versions"]["ingredient_release"]["version"] == "ing-1"
    assert body["locked_versions"]["ingredient_release"]["body_hash"]

    # 实际命中的组件路径（规范注解形式回显）
    assert body["matched_paths"] == [
        {"index": 0, "op": "set_qty", "path": TOMATO_IN_SAUCE},
        {"index": 1, "op": "replace", "path": PASTA_AT_ROOT},
        {"index": 2, "op": "remove", "path": BASIL_IN_SAUCE},
    ]

    # 每份营养与 NRV 差值：候选 = NUT_PESTO 800g + TOMATO 300g
    diff = {d["nutrient"]: d for d in body["diff"]["nutrition"]}
    energy = diff["energy_kcal"]
    assert energy["baseline"] == {"per_serving": 731, "pct_daily_value": 37}
    assert energy["candidate"] == {"per_serving": 815, "pct_daily_value": 41}
    assert energy["delta"] == {"per_serving": 84, "pct_daily_value": 4}
    protein = diff["protein"]
    assert protein["candidate"]["per_serving"] == pytest.approx(10.8)
    assert protein["delta"]["per_serving"] == pytest.approx(-16.8)
    assert protein["delta"]["pct_daily_value"] == -34
    sodium = diff["sodium_mg"]
    assert sodium["candidate"]["per_serving"] == 1204
    assert sodium["delta"]["pct_daily_value"] == 60

    # 原料占比变化
    props = {d["code"]: d for d in body["diff"]["ingredients"]}
    assert props["PASTA"]["change"] == "removed"
    assert props["PASTA"]["candidate"] is None
    assert props["NUT_PESTO"]["change"] == "added"
    assert props["NUT_PESTO"]["baseline"] is None
    assert props["NUT_PESTO"]["candidate"]["proportion_pct"] == 40.0
    assert props["TOMATO"]["change"] == "changed"
    assert props["TOMATO"]["baseline"]["proportion_pct"] == 30.0
    assert props["TOMATO"]["candidate"]["proportion_pct"] == 15.0
    assert props["TOMATO"]["delta_proportion_pct"] == -15.0
    assert props["BASIL"]["change"] == "removed"

    # 过敏原新增/移除
    allergens = {d["allergen"]: d for d in body["diff"]["allergens"]}
    assert allergens["gluten"]["change"] == "removed"
    assert allergens["gluten"]["candidate"] is None
    assert allergens["tree_nut"]["change"] == "added"
    assert allergens["tree_nut"]["candidate"]["status"] == "contains"
    assert allergens["tree_nut"]["candidate"]["sources"][0]["code"] == "NUT_PESTO"
    assert allergens["milk"]["change"] == "added"
    assert allergens["milk"]["candidate"]["status"] == "may_contain"

    # 基准/候选两侧完整结果一并返回
    assert body["baseline"]["ingredients_aggregate"]
    assert body["candidate"]["ingredients_aggregate"]
    assert body["root"]["servings_used"] == 4


def test_set_qty_with_unit_conversion_is_noop(client):
    # 0.6 kg == 600 g：候选与基准完全一致
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": TOMATO_IN_SAUCE, "qty": 0.6, "unit": "kg"},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    for d in body["diff"]["nutrition"]:
        assert d["delta"]["per_serving"] == 0
        assert d["delta"]["pct_daily_value"] == 0
    for d in body["diff"]["ingredients"]:
        assert d["change"] == "unchanged"
    for d in body["diff"]["allergens"]:
        assert d["change"] == "unchanged"


def test_path_forms_index_array_and_plain_string(client):
    seed(client)
    for path in ([1, 0], "components[1][0]"):
        resp = client.post("/api/scenarios/compare",
                           json={**BASE_REQ, "adjustments": [
                               {"op": "set_qty", "path": path, "qty": 300}]})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["matched_paths"][0]["path"] == TOMATO_IN_SAUCE


def test_replace_recipe_component_inherits_qty_and_unit(client):
    seed(client)
    pesto = {"code": "PESTO_SAUCE", "version": "1",
             "yield": {"qty": 1000, "unit": "g"}, "servings": 10,
             "components": [{"kind": "ingredient", "code": "NUT_PESTO",
                             "qty": 500, "unit": "g"}]}
    assert client.post("/api/recipes", json=pesto).status_code == 201
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": "components[1](recipe:SAUCE@1)",
         "component": {"kind": "recipe", "code": "PESTO_SAUCE",
                       "version": "1"}},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["matched_paths"][0]["path"] == "components[1](recipe:SAUCE@1)"
    allergens = {d["allergen"]: d for d in body["diff"]["allergens"]}
    assert allergens["tree_nut"]["change"] == "added"
    assert allergens["milk"]["change"] == "added"
    assert allergens["gluten"]["change"] == "unchanged"  # PASTA 未动
    props = {d["code"]: d for d in body["diff"]["ingredients"]}
    # 继承原引用量 10 portion = 1000g → 缩放 1.0 → NUT_PESTO 500g
    assert props["NUT_PESTO"]["candidate"]["root_unit_qty"] == 500
    assert props["TOMATO"]["change"] == "removed"
    assert props["BASIL"]["change"] == "removed"


def test_allergen_level_change(client):
    client.post("/api/ingredients", json={
        "release_version": "ing-1",
        "ingredients": [
            {"code": "A", "basis_unit": "g",
             "nutrition": {"energy_kcal": 100},
             "allergens": {"gluten": "contains", "milk": "free"}},
            {"code": "B", "basis_unit": "g",
             "nutrition": {"energy_kcal": 100},
             "allergens": {"gluten": "may_contain", "milk": "free"}},
        ]})
    client.post("/api/units", json=UNITS)
    client.post("/api/rules", json={
        "version": "r-1",
        "required_nutrients": [
            {"name": "energy_kcal", "daily_value": 2000, "decimals": 0}],
        "required_allergens": ["gluten", "milk"]})
    client.post("/api/recipes", json={
        "code": "R", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [{"kind": "ingredient", "code": "A",
                        "qty": 100, "unit": "g"}]})
    req = {"recipe_code": "R", "ingredient_release": "ing-1",
           "unit_version": "u-1", "rule_version": "r-1"}
    resp = client.post("/api/scenarios/compare", json={**req, "adjustments": [
        {"op": "replace", "path": "components[0](ingredient:A@1)",
         "component": {"kind": "ingredient", "code": "B"}},  # 继承 100 g
    ]})
    assert resp.status_code == 200, resp.get_json()
    changes = {d["allergen"]: d for d in resp.get_json()["diff"]["allergens"]}
    gluten = changes["gluten"]
    assert gluten["change"] == "de-escalated"
    assert gluten["baseline"]["status"] == "contains"
    assert gluten["candidate"]["status"] == "may_contain"


def test_occurrence_scoped_override(client):
    """同一子配方被引用两次时，路径调整只作用于被命中的那次出现。"""
    seed(client)
    double = {"code": "DOUBLE", "version": "1",
              "yield": {"qty": 2000, "unit": "g"}, "servings": 2,
              "components": [
                  {"kind": "recipe", "code": "SAUCE", "version": "1",
                   "qty": 500, "unit": "g"},
                  {"kind": "recipe", "code": "SAUCE", "version": "1",
                   "qty": 500, "unit": "g"}]}
    assert client.post("/api/recipes", json=double).status_code == 201
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "DOUBLE", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1",
        "adjustments": [{"op": "remove",
                         "path": "components[0](recipe:SAUCE@1)"
                                 "[1](ingredient:BASIL@1)"}]})
    assert resp.status_code == 200, resp.get_json()
    props = {d["code"]: d for d in resp.get_json()["diff"]["ingredients"]}
    # 只有第一处出现的 BASIL 被移除：总量减半而不是消失
    assert props["BASIL"]["change"] == "changed"
    assert props["BASIL"]["baseline"]["root_unit_qty"] == 10
    assert props["BASIL"]["candidate"]["root_unit_qty"] == 5
    assert props["BASIL"]["candidate"]["leaf_count"] == 1


def test_servings_override_applies_to_both_sides(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={
        **BASE_REQ, "servings_override": 8, "adjustments": [
            {"op": "set_qty", "path": PASTA_AT_ROOT, "qty": 0.4}]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["root"]["servings_used"] == 8
    energy = {d["nutrient"]: d
              for d in body["diff"]["nutrition"]}["energy_kcal"]
    assert energy["baseline"]["per_serving"] == 365   # 2922.3 / 8
    assert energy["candidate"]["per_serving"] == 190  # 1522.3 / 8
    assert energy["delta"]["per_serving"] == -175


# ---- 父子路径重叠：后执行的父级调整废弃子级覆盖 ---------------------------------

PESTO_SAUCE = {"code": "PESTO_SAUCE", "version": "1",
               "yield": {"qty": 1000, "unit": "g"}, "servings": 10,
               "components": [{"kind": "ingredient", "code": "NUT_PESTO",
                               "qty": 500, "unit": "g"}]}


def test_parent_replace_discards_child_override(client):
    """先改 SAUCE 内的 TOMATO，再把父组件 SAUCE 整体 replace 为 PESTO_SAUCE：
    父级替换废弃该路径下此前保存的子级覆盖，候选仅按替换后的组件树计算。"""
    seed(client)
    assert client.post("/api/recipes", json=PESTO_SAUCE).status_code == 201
    before = _db_counts()
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": TOMATO_IN_SAUCE, "qty": 300},
        {"op": "replace", "path": "components[1](recipe:SAUCE@1)",
         "component": {"kind": "recipe", "code": "PESTO_SAUCE",
                       "version": "1"}},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert [m["op"] for m in body["matched_paths"]] == ["set_qty", "replace"]

    # 候选只含 PASTA 与 NUT_PESTO：TOMATO/BASIL 随 SAUCE 一起被替换掉
    cand_codes = {a["code"]
                  for a in body["candidate"]["ingredients_aggregate"]}
    assert cand_codes == {"PASTA", "NUT_PESTO"}

    props = {d["code"]: d for d in body["diff"]["ingredients"]}
    assert props["TOMATO"]["change"] == "removed"
    assert props["BASIL"]["change"] == "removed"
    assert props["NUT_PESTO"]["change"] == "added"
    # 继承原引用量 10 portion = 1000g → 缩放 1.0 → NUT_PESTO 500g
    assert props["NUT_PESTO"]["candidate"]["root_unit_qty"] == 500
    assert props["PASTA"]["change"] == "unchanged"

    allergens = {d["allergen"]: d for d in body["diff"]["allergens"]}
    assert allergens["tree_nut"]["change"] == "added"
    assert allergens["milk"]["change"] == "added"
    assert allergens["gluten"]["change"] == "unchanged"  # PASTA 未动

    # 营养按替换后的组件树计算：PASTA 800g + NUT_PESTO 500g
    energy = {d["nutrient"]: d
              for d in body["diff"]["nutrition"]}["energy_kcal"]
    assert energy["candidate"]["per_serving"] == 1200  # (2800+2000)/4
    assert energy["delta"]["per_serving"] == 469       # 1200 - 730.575

    # 零持久化保持不变
    assert _db_counts() == before


def test_parent_remove_discards_child_override(client):
    """先改子级再移除父级：remove 同样废弃该路径下的子级覆盖。"""
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": TOMATO_IN_SAUCE, "qty": 300},
        {"op": "remove", "path": "components[1](recipe:SAUCE@1)"},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    cand_codes = {a["code"]
                  for a in body["candidate"]["ingredients_aggregate"]}
    assert cand_codes == {"PASTA"}
    props = {d["code"]: d for d in body["diff"]["ingredients"]}
    assert props["TOMATO"]["change"] == "removed"
    assert props["BASIL"]["change"] == "removed"
    assert props["PASTA"]["change"] == "unchanged"


def test_replace_then_adjust_child_of_replacement(client):
    """先替换父级再改新子树内的成分：后续子级调整作用于替换后的配方。"""
    seed(client)
    assert client.post("/api/recipes", json=PESTO_SAUCE).status_code == 201
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": "components[1](recipe:SAUCE@1)",
         "component": {"kind": "recipe", "code": "PESTO_SAUCE",
                       "version": "1"}},
        {"op": "set_qty",
         "path": "components[1](recipe:PESTO_SAUCE@1)"
                 "[0](ingredient:NUT_PESTO@1)",
         "qty": 250},
    ]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    cand_codes = {a["code"]
                  for a in body["candidate"]["ingredients_aggregate"]}
    assert cand_codes == {"PASTA", "NUT_PESTO"}
    props = {d["code"]: d for d in body["diff"]["ingredients"]}
    assert props["NUT_PESTO"]["candidate"]["root_unit_qty"] == 250
    assert props["TOMATO"]["change"] == "removed"
    assert props["BASIL"]["change"] == "removed"


# ---- 4xx：路径与引用 -----------------------------------------------------------

def test_invalid_path_errors(client):
    seed(client)
    # 下标越界
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove", "path": "components[5](ingredient:PASTA@1)"}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "INVALID_PATH"
    assert err["field"] == "adjustments[0].path"
    assert err["details"]["index"] == 5

    # 注解与实际成分不符（路径失效）
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove", "path": "components[0](ingredient:TOMATO@1)"}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "INVALID_PATH"
    assert err["field"] == "adjustments[0].path"
    assert err["details"]["actual"] == "PASTA"

    # 试图下钻原料成分
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove",
         "path": "components[0](ingredient:PASTA@1)[0](ingredient:X@1)"}]})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "INVALID_PATH"

    # 第二条调整失败时字段定位带正确下标
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": PASTA_AT_ROOT, "qty": 1},
        {"op": "remove", "path": "components[9](ingredient:PASTA@1)"}]})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["field"] == "adjustments[1].path"


def test_replace_unknown_references(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": PASTA_AT_ROOT,
         "component": {"kind": "ingredient", "code": "GHOST"}}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNKNOWN_INGREDIENT"
    assert err["field"] == "adjustments[0].component.code"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": PASTA_AT_ROOT,
         "component": {"kind": "recipe", "code": "SAUCE", "version": "99",
                       "qty": 1, "unit": "kg"}}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNKNOWN_RECIPE"
    assert err["field"] == "adjustments[0].component.code"


def test_set_qty_unknown_unit(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": TOMATO_IN_SAUCE, "qty": 1, "unit": "ml"}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "adjustments[0].unit"
    assert err["details"]["reason"] == "unknown_unit"
    assert err["details"]["component_path"].endswith(".unit")


def test_set_qty_unit_no_conversion_path(client):
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json={"version": "u-vol", "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "l", "to_unit": "ml", "factor": 1000}]})
    client.post("/api/rules", json=RULES)
    client.post("/api/recipes", json={
        "code": "R", "version": "1", "yield": {"qty": 200, "unit": "g"},
        "servings": 1,
        "components": [{"kind": "ingredient", "code": "TOMATO",
                        "qty": 200, "unit": "g"}]})
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-vol", "rule_version": "r-1",
        "adjustments": [{"op": "set_qty",
                         "path": "components[0](ingredient:TOMATO@1)",
                         "qty": 1, "unit": "l"}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "adjustments[0].unit"
    assert err["details"]["reason"] == "no_path"
    assert err["details"]["from_unit"] == "l"
    assert err["details"]["to_unit"] == "g"


def test_remove_last_component_empty_recipe(client):
    seed(client)
    client.post("/api/recipes", json={
        "code": "SINGLE", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [{"kind": "ingredient", "code": "TOMATO",
                        "qty": 100, "unit": "g"}]})
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "SINGLE", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1",
        "adjustments": [{"op": "remove",
                         "path": "components[0](ingredient:TOMATO@1)"}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "EMPTY_RECIPE"
    assert err["field"] == "adjustments[0]"
    assert err["details"]["recipe_code"] == "SINGLE"


def test_remove_empties_nested_subrecipe_occurrence(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove", "path": TOMATO_IN_SAUCE},
        # 上一条移除后 BASIL 下标前移为 0（调整按顺序生效）
        {"op": "remove",
         "path": "components[1](recipe:SAUCE@1)[0](ingredient:BASIL@1)"},
    ]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "EMPTY_RECIPE"
    assert err["field"] == "adjustments[1]"
    assert err["details"]["recipe_code"] == "SAUCE"


def test_replace_introduces_circular_reference(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": PASTA_AT_ROOT,
         "component": {"kind": "recipe", "code": "MEAL", "version": "1",
                       "qty": 1, "unit": "kg"}}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "CIRCULAR_REFERENCE"
    assert err["field"] == "adjustments[0].component.code"
    assert err["details"]["cycle"]


def test_replace_missing_declaration(client):
    client.post("/api/ingredients", json={
        "release_version": "ing-1",
        "ingredients": [
            {"code": "PASTA", "basis_unit": "g",
             "nutrition": {"energy_kcal": 350},
             "allergens": {"gluten": "contains", "milk": "free"}},
            {"code": "PLAIN", "basis_unit": "g",
             "nutrition": {"energy_kcal": 100},
             "allergens": {"gluten": "free"}},  # 缺 milk 声明
        ]})
    client.post("/api/units", json=UNITS)
    client.post("/api/rules", json={
        "version": "r-1",
        "required_nutrients": [
            {"name": "energy_kcal", "daily_value": 2000, "decimals": 0}],
        "required_allergens": ["gluten", "milk"]})
    client.post("/api/recipes", json={
        "code": "R", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [{"kind": "ingredient", "code": "PASTA",
                        "qty": 100, "unit": "g"}]})
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1",
        "adjustments": [{"op": "replace",
                         "path": "components[0](ingredient:PASTA@1)",
                         "component": {"kind": "ingredient", "code": "PLAIN"}}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_DECLARATION"
    assert err["field"] == "adjustments[0].allergens.milk"


def test_baseline_error_keeps_original_field(client):
    # 基准配方自身有单位问题（g 出现量无法换算到 ml 根产量单位）：
    # 错误保持原始字段路径，不归因到 adjustments
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json={"version": "u-vol", "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "l", "to_unit": "ml", "factor": 1000}]})
    client.post("/api/rules", json=RULES)
    client.post("/api/recipes", json={
        "code": "VOLUME_MEAL", "version": "1",
        "yield": {"qty": 1000, "unit": "ml"}, "servings": 4,
        "components": [{"kind": "ingredient", "code": "TOMATO",
                        "qty": 200, "unit": "g"}]})
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "VOLUME_MEAL", "ingredient_release": "ing-1",
        "unit_version": "u-vol", "rule_version": "r-1",
        "adjustments": [{"op": "set_qty",
                         "path": "components[0](ingredient:TOMATO@1)",
                         "qty": 100}]})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "components[0](ingredient:TOMATO@1).unit"


def test_malformed_adjustments(client):
    seed(client)
    resp = client.post("/api/scenarios/compare",
                       json={**BASE_REQ, "adjustments": []})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "add", "path": "components[0]"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].op"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": "components[0]"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].qty"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": "components[0]", "qty": 0}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].qty"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove", "path": "foo[0]"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].path"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "remove", "path": "components"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].path"

    resp = client.post("/api/scenarios/compare", json={**BASE_REQ, "adjustments": [
        {"op": "replace", "path": "components[0]"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "adjustments[0].component"


def test_locked_versions_not_found(client):
    seed(client)
    resp = client.post("/api/scenarios/compare", json={
        **BASE_REQ, "recipe_code": "GHOST",
        "adjustments": [{"op": "remove", "path": "components[0]"}]})
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "NOT_FOUND"

    resp = client.post("/api/scenarios/compare", json={
        **BASE_REQ, "ingredient_release": "nope",
        "adjustments": [{"op": "remove", "path": "components[0]"}]})
    assert resp.status_code == 404


# ---- 不落库与缓存隔离 -----------------------------------------------------------

def test_scenario_does_not_persist_or_touch_cache(client):
    seed(client)
    before = _db_counts()

    scenario_req = {**BASE_REQ, "adjustments": [
        {"op": "set_qty", "path": PASTA_AT_ROOT, "qty": 0.5}]}
    r1 = client.post("/api/scenarios/compare", json=scenario_req)
    assert r1.status_code == 200
    r2 = client.post("/api/scenarios/compare", json=scenario_req)
    assert r2.status_code == 200
    # 试算幂等：同请求同候选哈希
    assert r1.get_json()["candidate_hash"] == r2.get_json()["candidate_hash"]

    # 三张表均无写入，配方历史为空
    assert _db_counts() == before
    assert _db_counts()["computation"] == 0
    assert client.get("/api/recipes/MEAL/1/history").get_json() == []

    # 试算不产生缓存：随后的正式计算仍是 201 新记录，
    # 且基准指纹与正式计算指纹一致（同一基准口径）
    formal = client.post("/api/computations", json=BASE_REQ)
    assert formal.status_code == 201
    cid = formal.get_json()["computation_id"]
    assert r1.get_json()["baseline_fingerprint"] == \
        formal.get_json()["fingerprint"]

    again = client.post("/api/computations", json=BASE_REQ)
    assert again.status_code == 200
    assert again.get_json()["hits"] == 1

    # 试算不累计命中次数
    client.post("/api/scenarios/compare", json=scenario_req)
    client.post("/api/scenarios/compare", json=scenario_req)
    third = client.post("/api/computations", json=BASE_REQ)
    assert third.get_json()["hits"] == 2
    assert client.get(f"/api/computations/{cid}").get_json()["hits"] == 2


def test_candidate_hash_content_sensitive(client):
    seed(client)

    def run(qty):
        resp = client.post("/api/scenarios/compare", json={**BASE_REQ,
                           "adjustments": [
                               {"op": "set_qty", "path": PASTA_AT_ROOT,
                                "qty": qty}]})
        assert resp.status_code == 200
        return resp.get_json()["candidate_hash"]

    assert run(0.5) == run(0.5)
    assert run(0.5) != run(0.6)
