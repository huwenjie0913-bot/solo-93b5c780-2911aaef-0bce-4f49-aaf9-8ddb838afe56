"""端到端测试：配料表编排（括号结构/完全展开、省略阈值、强制添加剂、
同码合并、来源路径、规则指纹、规则冲突与 4xx）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_conn, init_db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LABEL_DB_PATH", str(tmp_path / "test.db"))
    init_db(get_conn())
    app = create_app(init_database=False)
    with app.test_client() as c:
        yield c


INGREDIENTS = {
    "release_version": "ing-1",
    "ingredients": [
        {"code": "FLOUR", "name": "小麦粉", "basis_unit": "g",
         "nutrition": {"energy_kcal": 350}, "allergens": {"gluten": "contains"}},
        {"code": "PORK", "name": "猪肉", "basis_unit": "g",
         "nutrition": {"energy_kcal": 200}, "allergens": {"gluten": "free"}},
        {"code": "WATER", "name": "水", "basis_unit": "g",
         "nutrition": {"energy_kcal": 0}, "allergens": {"gluten": "free"}},
        {"code": "SOY", "name": "大豆", "basis_unit": "g",
         "nutrition": {"energy_kcal": 100}, "allergens": {"gluten": "free"}},
        {"code": "SALT", "name": "食用盐", "basis_unit": "g",
         "nutrition": {"energy_kcal": 0}, "allergens": {"gluten": "free"}},
        {"code": "SUGAR", "name": "白砂糖", "basis_unit": "g",
         "nutrition": {"energy_kcal": 400}, "allergens": {"gluten": "free"}},
        {"code": "MSG", "name": "谷氨酸钠", "basis_unit": "g",
         "category": "additive", "nutrition": {"energy_kcal": 0},
         "allergens": {"gluten": "free"}},
        {"code": "PRES", "name": "山梨酸钾", "basis_unit": "g",
         "category": "additive", "nutrition": {"energy_kcal": 0},
         "allergens": {"gluten": "free"}},
    ],
}

UNITS = {"version": "u-1", "conversions": [
    {"from_unit": "kg", "to_unit": "g", "factor": 1000}]}

RULES = {
    "version": "r-1",
    "required_nutrients": [
        {"name": "energy_kcal", "daily_value": 2000, "decimals": 0}],
    "required_allergens": ["gluten"],
    "ingredient_list": {
        "mode": "parenthesize",
        "minor_threshold_pct": 2,
        "compound_threshold_pct": 25,
        "mandatory_additives": ["PRES"],
        "omit_eligible": ["WATER", "SALT", "SUGAR", "MSG"],
    },
}


def seed(client, rules=RULES):
    assert client.post("/api/ingredients", json=INGREDIENTS).status_code == 201
    assert client.post("/api/units", json=UNITS).status_code == 201
    assert client.post("/api/rules", json=rules).status_code == 201


# 酱油 100g（子配方，含添加剂）
SOY_SAUCE = {
    "code": "SOY_SAUCE", "version": "1", "name": "酱油",
    "yield": {"qty": 100, "unit": "g"}, "servings": 1,
    "components": [
        {"kind": "ingredient", "code": "SOY", "qty": 60, "unit": "g"},
        {"kind": "ingredient", "code": "SALT", "qty": 30, "unit": "g"},
        {"kind": "ingredient", "code": "WATER", "qty": 8, "unit": "g"},
        {"kind": "ingredient", "code": "MSG", "qty": 1.5, "unit": "g"},
        {"kind": "ingredient", "code": "PRES", "qty": 0.5, "unit": "g"},
    ],
}

# 肉馅 400g（复合配料）
FILLING = {
    "code": "FILLING", "version": "1", "name": "肉馅",
    "yield": {"qty": 400, "unit": "g"}, "servings": 1,
    "components": [
        {"kind": "ingredient", "code": "PORK", "qty": 300, "unit": "g"},
        {"kind": "recipe", "code": "SOY_SAUCE", "version": "1",
         "qty": 40, "unit": "g"},
        {"kind": "ingredient", "code": "SUGAR", "qty": 50, "unit": "g"},
        {"kind": "ingredient", "code": "MSG", "qty": 8, "unit": "g"},
        {"kind": "ingredient", "code": "PRES", "qty": 2, "unit": "g"},
    ],
}

# 成品包子 1000g：小麦粉 600g（kg 引用，验证换算链），肉馅 400g
BUN = {
    "code": "BUN", "version": "1", "name": "肉包",
    "yield": {"qty": 1000, "unit": "g"}, "servings": 4,
    "components": [
        {"kind": "ingredient", "code": "FLOUR", "qty": 0.6, "unit": "kg"},
        {"kind": "recipe", "code": "FILLING", "version": "1",
         "qty": 400, "unit": "g"},
    ],
}


def compute(client, rule_version="r-1", recipe="BUN"):
    return client.post("/api/computations", json={
        "recipe_code": recipe, "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": rule_version})


# ---- 括号结构模式 -------------------------------------------------------------

def test_parenthesize_text_and_structure(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)

    body = compute(client).get_json()
    il = body["ingredient_list"]

    # 可直接印刷的配料表文本：复合配料保留括号、嵌套括号
    assert il["text"] == (
        "配料表：小麦粉、肉馅（猪肉、白砂糖、酱油（谷氨酸钠、山梨酸钾）、"
        "谷氨酸钠、山梨酸钾）"
    )
    top = {i["code"]: i for i in il["items"]}
    assert list(top) == ["FLOUR", "FILLING"]  # 60% > 40% 降序

    filling = top["FILLING"]
    assert filling["type"] == "compound"
    assert filling["status"] == "shown" and filling["expanded"] is True
    assert filling["proportion_pct"] == 40.0
    # 括号内按占比降序：猪肉 30% > 糖 5% > 酱油 4% > MSG 0.8% > PRES 0.2%
    assert [i["code"] for i in filling["items"]] == [
        "PORK", "SUGAR", "SOY_SAUCE", "MSG", "PRES"]

    sauce = filling["items"][2]
    # 酱油成品占比 4% < 25% 复合展开阈值 → 折叠简式，括号内仅留强制添加剂
    assert sauce["status"] == "collapsed"
    assert sauce["expanded"] is False
    assert [i["code"] for i in sauce["items"]] == ["MSG", "PRES"]
    assert all(i["status"] == "forced" for i in sauce["items"])
    # 非强制成分进入折叠层 omitted 审计明细（大豆 2.4% 实际 ≥2%，但折叠层隐藏）
    hidden = {i["code"]: i for i in sauce["omitted"]}
    assert set(hidden) == {"SOY", "SALT", "WATER"}
    assert all(i["status"] == "omitted" for i in sauce["omitted"])


def test_within_parent_percent_and_boundary_scaling(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)
    il = compute(client).get_json()["ingredient_list"]

    filling = next(i for i in il["items"] if i["code"] == "FILLING")
    pork = filling["items"][0]
    # 猪肉在肉馅内占比 75%（300/400），成品占比 30%
    assert pork["within_parent_pct"] == 75.0
    assert pork["proportion_pct"] == 30.0
    sauce = next(i for i in filling["items"] if i["code"] == "SOY_SAUCE")
    # 酱油引用 40g / 产量 100g = 0.4 缩放：大豆 60g*0.4=24g
    soy = next(i for i in sauce["omitted"] if i["code"] == "SOY")
    assert soy["root_unit_qty"] == 24
    assert soy["proportion_pct"] == 2.4
    assert soy["within_parent_pct"] == 60.0


def test_source_paths_preserved_for_every_occurrence(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)
    il = compute(client).get_json()["ingredient_list"]

    filling = next(i for i in il["items"] if i["code"] == "FILLING")
    # 复合配料来源路径指向引用它的成分
    assert filling["sources"][0]["path"] == "components[1](recipe:FILLING@1)"
    msg = next(i for i in filling["items"] if i["code"] == "MSG")
    assert msg["sources"][0]["path"] == (
        "components[1](recipe:FILLING@1)[3](ingredient:MSG@1)")
    sauce = next(i for i in filling["items"] if i["code"] == "SOY_SAUCE")
    # 折叠括号内的 MSG 来自酱油内部（更深的路径）
    sauce_msg = next(i for i in sauce["items"] if i["code"] == "MSG")
    assert sauce_msg["sources"][0]["path"] == (
        "components[1](recipe:FILLING@1)"
        "[1](recipe:SOY_SAUCE@1)[3](ingredient:MSG@1)")


def test_same_code_same_level_merged_descending(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    # 根配方直接再放 0.6kg 面粉 + 另一份肉馅 400g → 2000g 产量
    big = {
        "code": "BUN2", "version": "1", "yield": {"qty": 2000, "unit": "g"},
        "servings": 8,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 0.6, "unit": "kg"},
            {"kind": "recipe", "code": "FILLING", "version": "1",
             "qty": 400, "unit": "g"},
            {"kind": "ingredient", "code": "FLOUR", "qty": 600, "unit": "g"},
            {"kind": "recipe", "code": "FILLING", "version": "1",
             "qty": 400, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=big)
    il = compute(client, recipe="BUN2").get_json()["ingredient_list"]

    codes = [i["code"] for i in il["items"]]
    assert codes.count("FLOUR") == 1
    assert codes.count("FILLING") == 1
    assert codes == ["FLOUR", "FILLING"]  # 1200g vs 800g
    flour = il["items"][0]
    assert flour["root_unit_qty"] == 1200
    assert flour["proportion_pct"] == 60.0
    assert len(flour["sources"]) == 2  # 两处出现的来源路径都保留
    filling = il["items"][1]
    assert len(filling["sources"]) == 2
    # 合并依据写在 reason 中
    assert "同码" in filling["reason"] and "出现 2 次已合并" in filling["reason"]


def test_minor_ingredient_omitted_below_threshold(client):
    # 没有复合层：白砂糖 1.5% 低于 2% 且在白名单 → 省略；食用盐 3% → 保留
    rules = {**RULES, "version": "r-flat"}
    seed(client, rules)
    flat = {
        "code": "FLAT", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 955, "unit": "g"},
            {"kind": "ingredient", "code": "SALT", "qty": 30, "unit": "g"},
            {"kind": "ingredient", "code": "SUGAR", "qty": 15, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=flat)
    il = compute(client, "r-flat", "FLAT").get_json()["ingredient_list"]
    assert il["text"] == "配料表：小麦粉、食用盐"
    assert [i["code"] for i in il["items"]] == ["FLOUR", "SALT"]
    omitted = {i["code"]: i for i in il["omitted"]}
    assert set(omitted) == {"SUGAR"}
    assert omitted["SUGAR"]["proportion_pct"] == 1.5
    assert "低于辅料省略阈值 2%" in omitted["SUGAR"]["reason"]
    assert il["counts"]["omitted"] == 1


def test_non_whitelisted_minor_ingredient_kept(client):
    rules = {**RULES, "version": "r-wl",
             "ingredient_list": {**RULES["ingredient_list"],
                                 "omit_eligible": ["SUGAR"]}}
    seed(client, rules)
    flat = {
        "code": "FLAT", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 955, "unit": "g"},
            {"kind": "ingredient", "code": "SALT", "qty": 30, "unit": "g"},
            {"kind": "ingredient", "code": "SUGAR", "qty": 15, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=flat)
    il = compute(client, "r-wl", "FLAT").get_json()["ingredient_list"]
    # SALT 3% 保留；SUGAR 1.5% 在白名单内 → 省略
    assert [i["code"] for i in il["items"]] == ["FLOUR", "SALT"]
    assert [i["code"] for i in il["omitted"]] == ["SUGAR"]

    # 白名单外的低值项：把 omit_eligible 改为仅 SALT 时，SUGAR 不予省略
    rules2 = {**RULES, "version": "r-wl2",
              "ingredient_list": {**RULES["ingredient_list"],
                                  "omit_eligible": ["SALT"]}}
    client.post("/api/rules", json=rules2)
    il2 = compute(client, "r-wl2", "FLAT").get_json()["ingredient_list"]
    assert [i["code"] for i in il2["items"]] == ["FLOUR", "SALT", "SUGAR"]
    sugar = next(i for i in il2["items"] if i["code"] == "SUGAR")
    assert "omit_eligible" in sugar["reason"] and "白名单" in sugar["reason"]


def test_additives_never_omitted(client):
    # 添加剂即使远低于阈值且未列入强制名单，也必须展示
    rules = {**RULES, "version": "r-add",
             "ingredient_list": {"mode": "expand", "minor_threshold_pct": 2,
                                 "mandatory_additives": [], "omit_eligible": []}}
    seed(client, rules)
    flat = {
        "code": "FLAT", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 995, "unit": "g"},
            {"kind": "ingredient", "code": "MSG", "qty": 1, "unit": "g"},
            {"kind": "ingredient", "code": "PRES", "qty": 1, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=flat)
    il = compute(client, "r-add", "FLAT").get_json()["ingredient_list"]
    assert il["text"] == "配料表：小麦粉、谷氨酸钠、山梨酸钾"
    for code in ("MSG", "PRES"):
        item = next(i for i in il["items"] if i["code"] == code)
        assert item["status"] == "forced"
        assert item["proportion_pct"] == 0.1
        assert "食品添加剂" in item["reason"]


def test_mandatory_additive_hoisted_from_omitted_compound(client):
    # SAUCE 1% 低于辅料阈值，其非保护成分全在白名单 → 整体省略；
    # 强制添加剂 PRES 与添加剂 MSG 上提到根层展示
    seed(client)
    sauce = {
        "code": "SAUCE", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "WATER", "qty": 90, "unit": "g"},
            {"kind": "ingredient", "code": "SALT", "qty": 8, "unit": "g"},
            {"kind": "ingredient", "code": "MSG", "qty": 1.5, "unit": "g"},
            {"kind": "ingredient", "code": "PRES", "qty": 0.5, "unit": "g"},
        ],
    }
    root = {
        "code": "R", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 4,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 800, "unit": "g"},
            {"kind": "ingredient", "code": "WATER", "qty": 190, "unit": "g"},
            {"kind": "recipe", "code": "SAUCE", "version": "1",
             "qty": 10, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=sauce)
    client.post("/api/recipes", json=root)
    il = compute(client, recipe="R").get_json()["ingredient_list"]

    assert il["text"] == "配料表：小麦粉、水、谷氨酸钠、山梨酸钾"
    omitted = {i["code"]: i for i in il["omitted"]}
    assert omitted["SAUCE"]["status"] == "omitted_with_hoist"
    hoisted = {h["code"]: h for h in omitted["SAUCE"]["hoisted"]}
    assert set(hoisted) == {"MSG", "PRES"}
    # PRES 0.5g × 0.1 缩放 = 0.05g，成品占比 0.005%（2 位小数显示为 0.01%）
    assert hoisted["PRES"]["root_unit_qty"] == 0.05
    assert hoisted["PRES"]["proportion_pct"] == 0.01
    # 上提项在顶层 items 中标记 forced 并记录来源复合配料路径
    for code in ("MSG", "PRES"):
        item = next(i for i in il["items"] if i["code"] == code)
        assert item["status"] == "forced"
        assert item["hoisted_from"] == ["components[2](recipe:SAUCE@1)"]
        assert item["sources"][0]["path"].endswith(f"(ingredient:{code}@1)")


def test_hoisted_additive_merges_with_same_code_at_parent(client):
    seed(client)
    sauce = {
        "code": "SAUCE", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "WATER", "qty": 98, "unit": "g"},
            {"kind": "ingredient", "code": "MSG", "qty": 1.5, "unit": "g"},
            {"kind": "ingredient", "code": "PRES", "qty": 0.5, "unit": "g"},
        ],
    }
    root = {
        "code": "R", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 4,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 800, "unit": "g"},
            {"kind": "ingredient", "code": "MSG", "qty": 5, "unit": "g"},
            {"kind": "recipe", "code": "SAUCE", "version": "1",
             "qty": 10, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=sauce)
    client.post("/api/recipes", json=root)
    il = compute(client, recipe="R").get_json()["ingredient_list"]

    msg = next(i for i in il["items"] if i["code"] == "MSG")
    assert len([i for i in il["items"] if i["code"] == "MSG"]) == 1
    # 5g 顶层 + 1.5g*0.1=0.15g 上提 = 5.15g
    assert msg["root_unit_qty"] == 5.15
    assert msg["status"] == "forced"
    assert len(msg["sources"]) == 2
    assert msg["hoisted_from"] == ["components[2](recipe:SAUCE@1)"]


# ---- 完全展开模式 -------------------------------------------------------------

def test_expand_mode_flattens_and_merges_across_levels(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)
    client.post("/api/rules", json={
        **RULES, "version": "r-exp",
        "ingredient_list": {**RULES["ingredient_list"], "mode": "expand"}})

    il = compute(client, "r-exp").get_json()["ingredient_list"]
    assert il["mode"] == "expand"
    # 无任何括号：跨层出现全部合并为一项，按成品占比降序
    assert "（" not in il["text"]
    rows = [(i["code"], i["proportion_pct"]) for i in il["items"]]
    # 面粉 600、猪肉 300、糖 50、大豆 24 均 ≥2% 展示；
    # 盐 12(1.2%)、水 3.2(0.32%) 低于阈值省略；MSG 8.6/PRES 2.2 强制展示
    assert [r[0] for r in rows] == [
        "FLOUR", "PORK", "SUGAR", "SOY", "MSG", "PRES"]
    assert {i["code"] for i in il["omitted"]} == {"SALT", "WATER"}
    msg = next(i for i in il["items"] if i["code"] == "MSG")
    # 肉馅 8g + 酱油内 1.5g*0.4=0.6g = 8.6g，两处来源合并
    assert msg["root_unit_qty"] == 8.6
    assert len(msg["sources"]) == 2
    assert msg["status"] == "forced"


# ---- 默认规则与版本/指纹 ------------------------------------------------------

def test_default_ingredient_list_when_rule_omits_block(client):
    # 旧版规则体没有 ingredient_list：默认 parenthesize / 2% / 25% / 空名单
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json=UNITS)
    plain = {"version": "r-plain",
             "required_nutrients": [
                 {"name": "energy_kcal", "daily_value": 2000, "decimals": 0}],
             "required_allergens": ["gluten"]}
    client.post("/api/rules", json=plain)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)

    il = compute(client, "r-plain").get_json()["ingredient_list"]
    assert il["mode"] == "parenthesize"
    assert il["thresholds"] == {"minor_threshold_pct": 2.0,
                                "compound_threshold_pct": 25.0}
    assert il["mandatory_additives"] == []
    assert il["text"].startswith("配料表：")


def test_label_rule_version_enters_fingerprint(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)

    id1 = compute(client, "r-1").get_json()["computation_id"]
    # 同版本同内容重放 → 缓存命中
    again = compute(client, "r-1")
    assert again.status_code == 200
    assert again.get_json()["computation_id"] == id1

    # 仅配料规则不同（expand）→ 新指纹、新记录
    client.post("/api/rules", json={
        **RULES, "version": "r-exp",
        "ingredient_list": {**RULES["ingredient_list"], "mode": "expand"}})
    r2 = compute(client, "r-exp")
    assert r2.status_code == 201
    assert r2.get_json()["computation_id"] != id1

    # 阈值不同也是新指纹（规则体哈希不同）
    client.post("/api/rules", json={
        **RULES, "version": "r-5pct",
        "ingredient_list": {**RULES["ingredient_list"],
                            "minor_threshold_pct": 5}})
    r3 = compute(client, "r-5pct")
    assert r3.status_code == 201
    assert r3.get_json()["computation_id"] not in (id1, r2.get_json()["computation_id"])


def test_export_contains_full_ingredient_list(client):
    seed(client)
    client.post("/api/recipes", json=SOY_SAUCE)
    client.post("/api/recipes", json=FILLING)
    client.post("/api/recipes", json=BUN)
    cid = compute(client).get_json()["computation_id"]
    doc = client.get(f"/api/computations/{cid}/export").get_json()
    il = doc["ingredient_list"]
    assert il["text"]
    assert il["items"] and il["basis"]
    # 叶子带资料类别
    assert all("category" in l for l in doc["leaves"])
    # 导出文档的规则体含配料表编排规则
    assert doc["label_rules_applied"]["ingredient_list"]["mode"] == "parenthesize"


# ---- 规则提交期 4xx -----------------------------------------------------------

def test_rule_conflict_mandatory_vs_omittable(client):
    seed(client)
    resp = client.post("/api/rules", json={
        **RULES, "version": "r-conflict",
        "ingredient_list": {
            "mode": "parenthesize", "minor_threshold_pct": 2,
            "mandatory_additives": ["MSG"],
            "omit_eligible": ["SALT", "MSG"]}})
    assert resp.status_code == 409
    err = resp.get_json()["error"]
    assert err["code"] == "RULE_CONFLICT"
    assert err["field"] == "ingredient_list.mandatory_additives"
    assert err["details"]["conflicting_codes"] == ["MSG"]


def test_rule_ingredient_list_malformed(client):
    seed(client)

    def post(block, version):
        return client.post("/api/rules", json={
            "version": version, "required_nutrients": [],
            "ingredient_list": block})

    r = post({"mode": "nope"}, "v1")
    assert r.status_code == 400
    assert r.get_json()["error"]["field"] == "ingredient_list.mode"

    r = client.post("/api/rules", json={
        "version": "v2", "required_nutrients": [],
        "ingredient_list": {"minor_threshold_pct": 101}})
    assert r.status_code == 400
    assert r.get_json()["error"]["field"] == "ingredient_list.minor_threshold_pct"

    r = client.post("/api/rules", json={
        "version": "v3", "required_nutrients": [],
        "ingredient_list": {"minor_threshold_pct": -1}})
    assert r.status_code == 400

    r = client.post("/api/rules", json={
        "version": "v4", "required_nutrients": [],
        "ingredient_list": {"mandatory_additives": ["A", "A"]}})
    assert r.status_code == 400
    assert r.get_json()["error"]["field"] == \
        "ingredient_list.mandatory_additives"

    r = client.post("/api/rules", json={
        "version": "v5", "required_nutrients": [],
        "ingredient_list": "nope"})
    assert r.status_code == 400
    assert r.get_json()["error"]["field"] == "ingredient_list"

    r = client.post("/api/rules", json={
        "version": "v6", "required_nutrients": [],
        "ingredient_list": {"mandatory_additives": [1, 2]}})
    assert r.status_code == 400


def test_ingredient_category_validation(client):
    bad = {"release_version": "ing-bad", "ingredients": [
        {"code": "X", "basis_unit": "g", "nutrition": {},
         "allergens": {}, "category": "poison"}]}
    r = client.post("/api/ingredients", json=bad)
    assert r.status_code == 400
    assert r.get_json()["error"]["field"] == "ingredients[0].category"

    # category 缺省为 ingredient；显式 additive 合法
    ok = {"release_version": "ing-ok", "ingredients": [
        {"code": "Y", "basis_unit": "g", "nutrition": {}, "allergens": {}},
        {"code": "Z", "basis_unit": "g", "nutrition": {}, "allergens": {},
         "category": "additive"}]}
    assert client.post("/api/ingredients", json=ok).status_code == 201


def test_unit_unconvertible_still_4xx_with_field_path(client):
    # 占比无法换算（g/ml 量纲不通）沿用既有 UNIT_CONFLICT 失败模型，
    # 不产出配料表，也不写计算记录
    client.post("/api/ingredients", json=INGREDIENTS)
    client.post("/api/units", json={"version": "u-vol", "conversions": [
        {"from_unit": "kg", "to_unit": "g", "factor": 1000},
        {"from_unit": "l", "to_unit": "ml", "factor": 1000}]})
    client.post("/api/rules", json=RULES)
    vol_meal = {
        "code": "VOL", "version": "1", "yield": {"qty": 1000, "unit": "ml"},
        "servings": 4,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 200, "unit": "g"}]}
    client.post("/api/recipes", json=vol_meal)
    resp = client.post("/api/computations", json={
        "recipe_code": "VOL", "ingredient_release": "ing-1",
        "unit_version": "u-vol", "rule_version": "r-1"})
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "UNIT_CONFLICT"
    assert err["field"] == "components[0](ingredient:FLOUR@1).unit"
    assert "ingredient_list" not in resp.get_data(as_text=True)
    assert client.get("/api/recipes/VOL/1/history").get_json() == []


def test_mandatory_additive_absent_note(client):
    seed(client)
    flat = {
        "code": "FLAT", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 100, "unit": "g"}]}
    client.post("/api/recipes", json=flat)
    il = compute(client, recipe="FLAT").get_json()["ingredient_list"]
    assert any("PRES" in n and "未出现" in n for n in il["notes"])


# ---- 试算比较 -----------------------------------------------------------------

def test_scenario_compare_includes_ingredient_list_diff(client):
    seed(client)
    sauce = {
        "code": "SAUCE", "version": "1", "yield": {"qty": 100, "unit": "g"},
        "servings": 1,
        "components": [
            {"kind": "ingredient", "code": "WATER", "qty": 90, "unit": "g"},
            {"kind": "ingredient", "code": "SALT", "qty": 8, "unit": "g"},
            {"kind": "ingredient", "code": "PRES", "qty": 2, "unit": "g"},
        ],
    }
    root = {
        "code": "R", "version": "1", "yield": {"qty": 1000, "unit": "g"},
        "servings": 4,
        "components": [
            {"kind": "ingredient", "code": "FLOUR", "qty": 800, "unit": "g"},
            {"kind": "recipe", "code": "SAUCE", "version": "1",
             "qty": 10, "unit": "g"},
        ],
    }
    client.post("/api/recipes", json=sauce)
    client.post("/api/recipes", json=root)

    # 基准：SAUCE 1% 整体省略、PRES 上提；候选：SAUCE 提到 300g（30%）展开括号
    resp = client.post("/api/scenarios/compare", json={
        "recipe_code": "R", "ingredient_release": "ing-1",
        "unit_version": "u-1", "rule_version": "r-1",
        "adjustments": [
            {"op": "set_qty",
             "path": "components[1](recipe:SAUCE@1)", "qty": 300}]})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["persisted"] is False
    lb = body["baseline"]["ingredient_list"]
    lc = body["candidate"]["ingredient_list"]
    assert "SAUCE" not in lb["text"]
    assert "SAUCE（" in lc["text"]
    diff = body["diff"]["ingredient_list"]
    assert diff["text_changed"] is True
    salt = next(d for d in diff["items"] if d["code"] == "SALT")
    assert salt["change"] == "added"
    assert salt["candidate_proportion_pct"] == 2.4
