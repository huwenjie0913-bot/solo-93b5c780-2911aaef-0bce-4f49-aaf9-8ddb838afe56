"""端到端测试：用 Flask test client 跑完整提交流程与错误路径。"""

import os
import sys
import tempfile

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


def seed_base(client):
    assert client.post("/api/ingredients", json=INGREDIENTS).status_code == 201
    assert client.post("/api/units", json=UNITS).status_code == 201
    assert client.post("/api/rules", json=RULES).status_code == 201


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


def test_happy_path_nutrition_allergens_and_proportion(client):
    seed_base(client)
    assert client.post("/api/recipes", json=SAUCE).status_code == 201
    assert client.post("/api/recipes", json=MEAL).status_code == 201

    resp = client.post("/api/computations", json={
        "recipe_code": "MEAL", "recipe_version": "1",
        "ingredient_release": "ing-1", "unit_version": "u-1",
        "rule_version": "r-1",
    })
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()

    # 营养总量：energy 2800+120+2.3 = 2922.3；每份 /4 = 730.575
    rows = {r["nutrient"]: r for r in body["nutrition_per_serving"]}
    assert rows["energy_kcal"]["per_serving"] == 731
    assert rows["energy_kcal"]["pct_daily_value"] == 37
    assert rows["protein"]["per_serving"] == pytest.approx(27.6, abs=1e-9)
    assert rows["protein"]["pct_daily_value"] == 55
    assert rows["sodium_mg"]["per_serving"] == 10
    assert rows["sodium_mg"]["pct_daily_value"] == 0

    # 份量换算
    assert body["root"]["servings_used"] == 4
    assert body["root"]["serving_yield"] == {"qty": 500, "unit": "g"}

    # 占比（kg/portion 都换算到根单位 g）
    props = {i["code"]: i for i in body["ingredients_aggregate"]}
    assert props["PASTA"]["root_unit_qty"] == 800
    assert props["PASTA"]["proportion_pct"] == 40.0
    assert props["TOMATO"]["root_unit_qty"] == 600
    assert props["BASIL"]["root_unit_qty"] == 10
    # 同一原料跨多层出现要合并
    assert props["TOMATO"]["leaf_count"] == 1

    # 过敏原保留来源路径（沿引用链）
    allergens = {a["allergen"]: a for a in body["allergens"]}
    assert set(allergens) == {"gluten"}
    gluten = allergens["gluten"]
    assert gluten["status"] == "contains"
    assert gluten["sources"][0]["code"] == "PASTA"
    assert gluten["sources"][0]["path"].startswith(
        "components[0](ingredient:PASTA@1)"
    )
    # egg=free 不上标签
    assert "egg" not in allergens


def test_allergen_path_through_nested_recipe(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    pesto_sauce = dict(SAUCE, code="PESTO_SAUCE", version="1",
                       components=[
                           {"kind": "ingredient", "code": "NUT_PESTO",
                            "qty": 500, "unit": "g"}])
    client.post("/api/recipes", json=pesto_sauce)
    meal = dict(MEAL, components=[
        {"kind": "recipe", "code": "PESTO_SAUCE", "version": "1",
         "qty": 10, "unit": "portion"}])
    client.post("/api/recipes", json=meal)

    body = client.post("/api/computations", json={
        "recipe_code": "MEAL", "recipe_version": "1",
        "ingredient_release": "ing-1", "unit_version": "u-1",
        "rule_version": "r-1",
    }).get_json()
    allergens = {a["allergen"]: a for a in body["allergens"]}
    src = allergens["tree_nut"]["sources"][0]
    assert src["path"] == (
        "components[0](recipe:PESTO_SAUCE@1)"
        "[0](ingredient:NUT_PESTO@1)"
    )
    assert allergens["tree_nut"]["status"] == "contains"
    # may_contain 也保留
    assert allergens["milk"]["status"] == "may_contain"
    # 必报过敏原 milk：NUT_PESTO 有声明，不报错


def test_fingerprint_cache_reuse(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    client.post("/api/recipes", json=MEAL)
    req = {"recipe_code": "MEAL", "recipe_version": "1",
           "ingredient_release": "ing-1", "unit_version": "u-1",
           "rule_version": "r-1"}

    r1 = client.post("/api/computations", json=req)
    assert r1.status_code == 201
    assert r1.get_json()["cached"] is False
    cid = r1.get_json()["computation_id"]

    r2 = client.post("/api/computations", json=req)
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2["computation_id"] == cid  # 指纹复用，同一计算记录

    detail = client.get(f"/api/computations/{cid}").get_json()
    assert detail["hits"] >= 1
    fp = detail["fingerprint"]
    lookup = client.get(f"/api/computations/fingerprint/{fp}")
    assert lookup.get_json()["computation_id"] == cid

    # 份数覆盖产生新指纹 → 新记录
    r3 = client.post("/api/computations", json={**req, "servings_override": 8})
    assert r3.status_code == 201
    assert r3.get_json()["computation_id"] != cid
    assert r3.get_json()["root"]["servings_used"] == 8


def test_recipe_history_includes_subrecipe(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    client.post("/api/recipes", json=MEAL)
    req = {"recipe_code": "MEAL", "ingredient_release": "ing-1",
           "unit_version": "u-1", "rule_version": "r-1"}
    client.post("/api/computations", json=req)

    history = client.get("/api/recipes/SAUCE/1/history").get_json()
    assert len(history) == 1
    assert history[0]["root_code"] == "MEAL"
    assert history[0]["depth"] == 1

    root_history = client.get("/api/recipes/MEAL/1/history").get_json()
    assert root_history[0]["depth"] == 0


def test_export_contains_basis(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    client.post("/api/recipes", json=MEAL)
    cid = client.post("/api/computations", json={
        "recipe_code": "MEAL", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1"}).get_json()["computation_id"]

    doc = client.get(f"/api/computations/{cid}/export").get_json()
    assert doc["computation_id"] == cid
    assert doc["locked_versions"]["ingredient_release"]["version"] == "ing-1"
    assert doc["locked_versions"]["label_rule"]["body_hash"]
    assert len(doc["boundaries"]) == 1  # MEAL 引用 SAUCE 的跨边界缩放
    b = doc["boundaries"][0]
    assert b["scale_factor"] == 1.0
    # 叶子含基准换算与营养贡献
    tomato = next(l for l in doc["leaves"] if l["code"] == "TOMATO")
    assert tomato["basis_qty"] == 600
    assert tomato["nutrition_contribution"]["energy_kcal"] == 120
    assert tomato["path"].endswith("[0](ingredient:TOMATO@1)")
    assert doc["calculation_basis"]
    assert doc["conversion_paths_used"]


# ---- 4xx 错误路径 -----------------------------------------------------------

def test_circular_reference(client):
    seed_base(client)
    a = {"code": "A", "version": "1", "yield": {"qty": 1, "unit": "g"},
         "servings": 1,
         "components": [{"kind": "recipe", "code": "B", "qty": 1, "unit": "g"}]}
    b = {"code": "B", "version": "1", "yield": {"qty": 1, "unit": "g"},
         "servings": 1,
         "components": [{"kind": "recipe", "code": "A", "qty": 1, "unit": "g"}]}
    client.post("/api/recipes", json=a)
    client.post("/api/recipes", json=b)
    resp = client.post("/api/computations", json={
        "recipe_code": "A", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "CIRCULAR_REFERENCE"
    assert err["details"]["cycle"] == ["A@1", "B@1", "A@1"]
    assert err["field"]


def test_unknown_ingredient_with_field_path(client):
    seed_base(client)
    r = {"code": "R", "version": "1", "yield": {"qty": 1, "unit": "g"},
         "servings": 1,
         "components": [{"kind": "ingredient", "code": "GHOST",
                         "qty": 1, "unit": "g"}]}
    client.post("/api/recipes", json=r)
    resp = client.post("/api/computations", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNKNOWN_INGREDIENT"
    assert err["field"] == "components[0](ingredient:GHOST@1).code"
    assert err["details"]["code"] == "GHOST"


def test_unknown_recipe_and_unknown_version(client):
    seed_base(client)
    resp = client.post("/api/computations", json={
        "recipe_code": "NOPE", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1"})
    assert resp.status_code == 404

    client.post("/api/recipes", json=SAUCE)
    resp = client.post("/api/computations", json={
        "recipe_code": "SAUCE", "recipe_version": "99",
        "ingredient_release": "ing-1", "unit_version": "u-1",
        "rule_version": "r-1"})
    assert resp.status_code == 404


def test_unit_conflict_no_conversion_path(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    # SAUCE 产量单位为 g，却用换算表中完全不存在的 cup 引用 → 无换算路径
    r = {"code": "R", "version": "1", "yield": {"qty": 1, "unit": "g"},
         "servings": 1,
         "components": [{"kind": "recipe", "code": "SAUCE",
                         "qty": 1, "unit": "cup"}]}
    client.post("/api/recipes", json=r)
    resp = client.post("/api/computations", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "components[0](recipe:SAUCE@1).unit"
    assert err["details"]["reason"] == "unknown_unit"
    assert err["details"]["unit"] == "cup"


def test_unit_conflict_basis_unreachable(client):
    # 原料以 g 声明，但配方用量用了“已知但无法换算到 g”的单位 ml → 营养无法计算
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json={
        "version": "u-vol",
        "conversions": [
            {"from_unit": "l", "to_unit": "ml", "factor": 1000},
            {"from_unit": "g", "to_unit": "g", "factor": 1},
        ]})
    client.post("/api/rules", json=RULES)
    r = {"code": "R", "version": "1", "yield": {"qty": 1, "unit": "ml"},
         "servings": 1,
         "components": [{"kind": "ingredient", "code": "TOMATO",
                         "qty": 1, "unit": "ml"}]}
    client.post("/api/recipes", json=r)
    resp = client.post("/api/computations", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-vol", "rule_version": "r-1"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "components[0](ingredient:TOMATO@1).unit"
    assert err["details"]["reason"] == "no_path"


def test_unit_table_contradiction_rejected(client):
    bad = {"version": "u-bad", "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "tonne", "to_unit": "kg", "factor": 1000},
        {"from_unit": "tonne", "to_unit": "g", "factor": 999999}]}
    resp = client.post("/api/units", json=bad)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "UNIT_CONFLICT"


def test_missing_allergen_declaration(client):
    seed_base(client)
    # 新规则要求 soy 声明；所有原料均未声明 soy
    client.post("/api/rules", json={**RULES, "version": "r-2",
                                    "required_allergens": ["soy"]})
    client.post("/api/recipes", json=SAUCE)
    resp = client.post("/api/computations", json={
        "recipe_code": "SAUCE", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-2"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_DECLARATION"
    assert err["field"].endswith("allergens.soy")
    assert len(err["details"]["occurrences"]) >= 1


def test_missing_nutrient_declaration(client):
    seed_base(client)
    client.post("/api/rules", json={
        "version": "r-3",
        "required_nutrients": [
            {"name": "fiber", "daily_value": 25, "decimals": 1}]})
    client.post("/api/recipes", json=SAUCE)
    resp = client.post("/api/computations", json={
        "recipe_code": "SAUCE", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-3"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_DECLARATION"
    assert "fiber" in err["field"]


def test_malformed_field_paths(client):
    resp = client.post("/api/ingredients", json={"release_version": "x"})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "ingredients"

    resp = client.post("/api/recipes", json={
        "code": "R", "version": "1",
        "yield": {"qty": 1, "unit": "g"}, "servings": 1,
        "components": [{"kind": "ingredient", "qty": 1, "unit": "g"}]})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "components[0].code"

    resp = client.post("/api/computations", json={"nope": 1})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "recipe_code"

    resp = client.post("/api/recipes", data="not json",
                       content_type="application/json")
    assert resp.status_code == 400


def test_version_immutability_and_idempotent_replay(client):
    seed_base(client)
    assert client.post("/api/recipes", json=SAUCE).status_code == 201
    # 同内容重放：幂等
    again = client.post("/api/recipes", json=SAUCE)
    assert again.status_code == 200
    assert again.get_json()["replayed"] is True
    # 同版本改内容：冲突
    changed = dict(SAUCE, servings=8)
    resp = client.post("/api/recipes", json=changed)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["field"] == "version"


def test_locked_versions_affect_fingerprint(client):
    seed_base(client)
    client.post("/api/recipes", json=SAUCE)
    client.post("/api/recipes", json=MEAL)
    req = {"recipe_code": "MEAL", "ingredient_release": "ing-1",
           "unit_version": "u-1", "rule_version": "r-1"}
    cid1 = client.post("/api/computations", json=req).get_json()["computation_id"]

    # 新原料资料版本（同内容，不同版本号/哈希）→ 锁变了，应产生新记录
    client.post("/api/ingredients",
                json={**INGREDIENTS, "release_version": "ing-2"})
    resp = client.post("/api/computations",
                       json={**req, "ingredient_release": "ing-2"})
    assert resp.status_code == 201
    assert resp.get_json()["computation_id"] != cid1
    # 锁定的旧版本计算仍可取回（版本不可变 → 历史可复算）
    doc = client.get(f"/api/computations/{cid1}/export").get_json()
    assert doc["locked_versions"]["ingredient_release"]["version"] == "ing-1"
