"""营养声明（claims）端到端测试。

覆盖：规则版本定义声明（名称/营养素/每份阈值/方向/单位）、计算逐条判定并
写入摘要与导出文档、随计算记录查询、阈值版本化与不可变性，以及缺失营养素、
无效方向、阈值冲突的统一 4xx。
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
         "allergens": {"gluten": "contains", "milk": "free"}},
        {"code": "BASIL", "name": "罗勒", "basis_unit": "g",
         "nutrition": {"energy_kcal": 23, "protein": 3.0, "sodium_mg": 4},
         "allergens": {"gluten": "free", "milk": "free"}},
    ],
}

UNITS = {
    "version": "u-1",
    "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "portion", "to_unit": "g", "factor": 100},
    ],
}

RULES_WITH_CLAIMS = {
    "version": "r-claims",
    "required_nutrients": [
        {"name": "energy_kcal", "daily_value": 2000, "decimals": 0},
        {"name": "protein", "daily_value": 50, "decimals": 1},
        {"name": "sodium_mg", "daily_value": 2000, "decimals": 0},
    ],
    "required_allergens": ["gluten", "milk"],
    "claims": [
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 120, "direction": "lte", "unit": "mg"},
        {"name": "high_protein", "nutrient": "protein",
         "threshold": 25, "direction": "gte", "unit": "g"},
        {"name": "low_energy", "nutrient": "energy_kcal",
         "threshold": 500, "direction": "lte", "unit": "kcal"},
    ],
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

COMPUTE_REQ = {"recipe_code": "MEAL", "recipe_version": "1",
               "ingredient_release": "ing-1", "unit_version": "u-1",
               "rule_version": "r-claims"}


def seed(client, rules=None):
    assert client.post("/api/ingredients", json=INGREDIENTS).status_code == 201
    assert client.post("/api/units", json=UNITS).status_code == 201
    assert client.post("/api/rules",
                       json=rules or RULES_WITH_CLAIMS).status_code == 201
    assert client.post("/api/recipes", json=SAUCE).status_code == 201
    assert client.post("/api/recipes", json=MEAL).status_code == 201


# ---- 正常路径：判定、摘要、导出、查询 ------------------------------------------

def test_claims_evaluated_and_queryable_with_computation(client):
    seed(client)
    resp = client.post("/api/computations", json=COMPUTE_REQ)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()

    # MEAL 每份：energy 731 kcal / protein 27.6 g / sodium 10 mg
    claims = {c["name"]: c for c in body["claims"]}
    assert set(claims) == {"low_sodium", "high_protein", "low_energy"}

    low_sodium = claims["low_sodium"]
    assert low_sodium["passed"] is True            # 10 ≤ 120
    assert low_sodium["actual"] == 10
    assert low_sodium["threshold"] == 120
    assert low_sodium["unit"] == "mg"
    assert low_sodium["direction"] == "lte"
    assert low_sodium["delta"] == -110             # 实际 − 阈值
    assert low_sodium["rule_version"] == "r-claims"
    assert "≤" in low_sodium["basis"] and "120" in low_sodium["basis"]

    high_protein = claims["high_protein"]
    assert high_protein["passed"] is True          # 27.6 ≥ 25
    assert high_protein["actual"] == pytest.approx(27.6)
    assert high_protein["actual_raw"] == pytest.approx(27.575)
    assert high_protein["delta"] == pytest.approx(2.6)

    low_energy = claims["low_energy"]
    assert low_energy["passed"] is False           # 731 > 500
    assert low_energy["actual"] == 731
    assert low_energy["delta"] == 231
    assert "不达标" in low_energy["basis"]

    # 随计算记录查询：GET 返回落库摘要中的同一份声明结果
    cid = body["computation_id"]
    detail = client.get(f"/api/computations/{cid}").get_json()
    assert detail["claims"] == body["claims"]

    # 指纹命中（200）同样返回声明结果
    again = client.post("/api/computations", json=COMPUTE_REQ)
    assert again.status_code == 200
    assert again.get_json()["claims"] == body["claims"]


def test_claims_in_export_document(client):
    seed(client)
    cid = client.post("/api/computations", json=COMPUTE_REQ) \
        .get_json()["computation_id"]
    doc = client.get(f"/api/computations/{cid}/export").get_json()

    claims = {c["name"]: c for c in doc["claims"]}
    assert claims["low_sodium"]["passed"] is True
    assert claims["low_sodium"]["rule_version"] == "r-claims"
    assert claims["low_sodium"]["basis"]
    # 导出文档含锁定规则全文（含 claims 定义）与文字版判定口径
    applied = doc["label_rules_applied"]
    assert applied["claims"][0]["name"] == "low_sodium"
    assert applied["claims"][0]["threshold"] == 120
    assert "营养声明" in doc["calculation_basis"]


def test_claims_empty_when_rule_version_has_none(client):
    rules = {"version": "r-plain",
             "required_nutrients": [
                 {"name": "energy_kcal", "daily_value": 2000, "decimals": 0}],
             "required_allergens": ["gluten", "milk"]}
    seed(client, rules)
    resp = client.post("/api/computations",
                       json={**COMPUTE_REQ, "rule_version": "r-plain"})
    assert resp.status_code == 201
    assert resp.get_json()["claims"] == []
    cid = resp.get_json()["computation_id"]
    doc = client.get(f"/api/computations/{cid}/export").get_json()
    assert doc["claims"] == []


def test_claim_threshold_versioning_and_immutability(client):
    """同一配方在不同规则版本下判定不同；旧版本计算永远可按旧阈值复算。"""
    seed(client)
    strict = dict(RULES_WITH_CLAIMS, version="r-strict")
    strict["claims"] = [
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 5, "direction": "lte", "unit": "mg"},  # 更严：10 > 5
    ]
    assert client.post("/api/rules", json=strict).status_code == 201

    r1 = client.post("/api/computations", json=COMPUTE_REQ)
    assert r1.status_code == 201
    cid1 = r1.get_json()["computation_id"]
    r2 = client.post("/api/computations",
                     json={**COMPUTE_REQ, "rule_version": "r-strict"})
    assert r2.status_code == 201
    cid2 = r2.get_json()["computation_id"]
    assert cid1 != cid2  # 锁定规则哈希不同 → 不同指纹、不同记录

    claims1 = {c["name"]: c
               for c in client.get(f"/api/computations/{cid1}")
               .get_json()["claims"]}
    claims2 = {c["name"]: c
               for c in client.get(f"/api/computations/{cid2}")
               .get_json()["claims"]}
    assert claims1["low_sodium"]["passed"] is True   # 阈值 120：达标
    assert claims1["low_sodium"]["threshold"] == 120
    assert claims2["low_sodium"]["passed"] is False  # 阈值 5：不达标
    assert claims2["low_sodium"]["threshold"] == 5
    assert claims2["low_sodium"]["delta"] == 5       # 10 − 5
    assert claims2["low_sodium"]["rule_version"] == "r-strict"

    # 同版本号改阈值 → 409 不可变冲突；同内容重提 → 200 幂等
    changed = dict(RULES_WITH_CLAIMS)
    changed["claims"] = strict["claims"]
    resp = client.post("/api/rules", json=changed)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "CONFLICT"
    replay = client.post("/api/rules", json=RULES_WITH_CLAIMS)
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True


def test_claim_missing_nutrient_declaration_at_compute(client):
    """声明营养素属于必报项，但某叶子原料未声明 → 沿用 MISSING_DECLARATION。"""
    client.post("/api/ingredients", json={
        "release_version": "ing-1",
        "ingredients": [
            {"code": "PASTA", "basis_unit": "g",
             "nutrition": {"energy_kcal": 350, "protein": 13},  # 缺 sodium_mg
             "allergens": {"gluten": "contains", "milk": "free"}},
        ]})
    client.post("/api/units", json=UNITS)
    assert client.post("/api/rules", json=RULES_WITH_CLAIMS).status_code == 201
    client.post("/api/recipes", json={
        "code": "R", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [{"kind": "ingredient", "code": "PASTA",
                        "qty": 100, "unit": "g"}]})
    resp = client.post("/api/computations", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-claims"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_DECLARATION"
    assert err["field"].endswith("nutrition.sodium_mg")


# ---- 4xx：规则提交期 -----------------------------------------------------------

def test_claim_nutrient_not_required_rejected(client):
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json=UNITS)
    bad = dict(RULES_WITH_CLAIMS)
    bad["claims"] = [{"name": "low_fat", "nutrient": "fat_g",
                      "threshold": 3, "direction": "lte", "unit": "g"}]
    resp = client.post("/api/rules", json=bad)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MALFORMED"
    assert err["field"] == "claims[0].nutrient"
    assert err["details"]["nutrient"] == "fat_g"


def test_claim_invalid_direction_rejected(client):
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json=UNITS)
    bad = dict(RULES_WITH_CLAIMS)
    bad["claims"] = [{"name": "low_sodium", "nutrient": "sodium_mg",
                      "threshold": 120, "direction": "less_than", "unit": "mg"}]
    resp = client.post("/api/rules", json=bad)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MALFORMED"
    assert err["field"] == "claims[0].direction"

    # 阈值必须是非负数字
    bad2 = dict(RULES_WITH_CLAIMS)
    bad2["claims"] = [{"name": "low_sodium", "nutrient": "sodium_mg",
                       "threshold": -1, "direction": "lte", "unit": "mg"}]
    resp = client.post("/api/rules", json=bad2)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "claims[0].threshold"

    # claims 必须是数组
    resp = client.post("/api/rules",
                       json={**RULES_WITH_CLAIMS, "claims": "low_sodium"})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "claims"


def test_claim_threshold_conflict_rejected(client):
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json=UNITS)
    conflicted = dict(RULES_WITH_CLAIMS)
    conflicted["claims"] = [
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 120, "direction": "lte", "unit": "mg"},
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 40, "direction": "lte", "unit": "mg"},  # 同名不同阈值
    ]
    resp = client.post("/api/rules", json=conflicted)
    assert resp.status_code == 409
    err = resp.get_json()["error"]
    assert err["code"] == "CLAIM_CONFLICT"
    assert err["field"] == "claims[1].threshold"
    assert err["details"]["existing"] == 120
    assert err["details"]["new"] == 40

    # 同名且定义完全一致 → 重复声明（400）
    duplicated = dict(RULES_WITH_CLAIMS)
    duplicated["claims"] = [
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 120, "direction": "lte", "unit": "mg"},
        {"name": "low_sodium", "nutrient": "sodium_mg",
         "threshold": 120, "direction": "lte", "unit": "mg"},
    ]
    resp = client.post("/api/rules", json=duplicated)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["field"] == "claims[1].name"
