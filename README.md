# 配方标签推导 API（central-kitchen label derivation）

中央厨房半成品层层嵌入配方时，自动：

- **递归展开嵌套配方成分树**（配方可引用带版本号的子配方）；
- 按根配方产量/份数推导**每份营养值与 NRV%**、**原料最终占比**；
- 沿成分树**逐项保留过敏原来源路径**（`contains` 优先于 `may_contain`，`free` 不上标签）；
- 对**循环引用、未知原料/配方、单位冲突、声明缺失**返回 4xx 并指出字段路径；
- **资料版本不可变 + 计算时版本锁定**，历史计算永远可复算；
- 以**请求指纹**复用完全等价的计算；
- 可**按配方追溯**其作为根配方或子配方参与过的全部计算；
- 可导出**含完整计算依据**（缩放边界、单位换算路径、基准因子、锁定版本哈希）的 JSON；
- 可**试算临时换料方案**（`set_qty`/`replace`/`remove`，按嵌套组件路径定位），
  与基准并排比较营养、占比、过敏原差异，全程不落库。

技术栈：Python 3.11、Flask、sqlite3（标准库）、pytest。

## 启动

```bash
pip install -r requirements.txt
export LABEL_DB_PATH=./labels.db     # 可选，默认 ./labels.db
python run.py                        # http://localhost:5000
pytest -q                            # 50 个端到端测试
```

## 数据模型与版本语义

四类资料均为**带版本号、提交即不可变**：

| 实体 | 提交接口 | 关键内容 |
|---|---|---|
| 原料资料 release | `POST /api/ingredients` | 一个版本含多个原料；营养值按 `basis_amount/basis_unit` 声明；过敏原状态 `contains/may_contain/free` |
| 单位换算表 | `POST /api/units` | `1 from_unit = factor to_unit`（自动加反向边，闭包一致性校验） |
| 标签规则 | `POST /api/rules` | 必报营养素、NRV 每日参考值、舍入位数、必报过敏原、营养声明 `claims`（名称/营养素/每份阈值/方向/单位） |
| 配方 | `POST /api/recipes` | 产量 `yield{qty,unit}`、份数、成分（原料或子配方引用，可指定版本） |

- 相同版本 + 相同内容重复提交 → `200 replayed`（幂等）；
- 相同版本 + 不同内容 → `409 CONFLICT`（不可覆盖，请用新版本号）。

## 计算口径

对成分树中的每个原料出现（叶子）：

1. **出现量** = 配方声明用量 × 沿途每个配方边界的缩放系数连乘；
   边界缩放系数 = 引用量换算到子配方产量单位后 ÷ 子配方产量；
2. **营养贡献** = 出现量换算到原料 `basis_unit` 后 ÷ `basis_amount` × 基准营养值；
3. **每份营养** = 全部叶子营养贡献之和 ÷ 份数（支持 `servings_override`），
   `NRV% = 每份值 ÷ daily_value × 100`，按规则位数四舍五入（HALF_UP）；
4. **原料占比** = 该原料所有出现量换算到根配方产量单位之和 ÷ 根产量；
   出现量若无法换算到根产量单位（量纲不通，如 `g` 与 `ml`）直接 422，
   不会产生空占比，也不会写入计算记录；
5. **过敏原** = 每个非 `free` 声明保留一条来源（原料、状态、完整字段路径、出现量）；
6. **营养声明** = 按锁定规则版本的 `claims` 逐条判定：每份实际值按该营养素
   规则位数舍入后与每份阈值比较，`lte` 要求 ≤ 阈值、`gte` 要求 ≥ 阈值。

请求指纹 = SHA256(规范化 JSON{请求参数, 原料/单位/规则版本哈希, 展开闭包内各配方版本+内容哈希})，
任一锁定资料或配方内容变化都会产生新指纹；同指纹直接复用历史记录并累计 `hits`。

## 营养声明（claims）

规则版本可携带可选的 `claims` 数组，定义低钠、高蛋白等自愿声明的判定口径：

```json
{"version": "r-1",
 "required_nutrients": [
   {"name": "protein", "daily_value": 50, "decimals": 1},
   {"name": "sodium_mg", "daily_value": 2000, "decimals": 0}],
 "claims": [
   {"name": "low_sodium", "nutrient": "sodium_mg",
    "threshold": 120, "direction": "lte", "unit": "mg"},
   {"name": "high_protein", "nutrient": "protein",
    "threshold": 25, "direction": "gte", "unit": "g"}]}
```

- `nutrient` 必须已在本版本 `required_nutrients` 中声明（判定依赖强制申报的
  每份值），否则提交期 `400`，字段定位 `claims[i].nutrient`；
- `direction` 仅接受 `lte`（不超过阈值）/ `gte`（不低于阈值），
  其他取值 `400`，字段定位 `claims[i].direction`；
- `threshold` 为非负数字（每份阈值），`unit` 为阈值/实际值的展示单位；
- 同一版本内声明名称唯一：同名定义矛盾（如阈值不同）→ `409 CLAIM_CONFLICT`，
  字段定位到冲突属性（如 `claims[1].threshold`）；完全重复 → `400`。

计算时逐条判定并随结果返回（计算摘要、`GET /api/computations/<id>`、
导出文档的 `claims` 字段均可见）：

```json
{"name": "low_sodium", "nutrient": "sodium_mg", "direction": "lte",
 "threshold": 120, "unit": "mg", "actual": 10, "actual_raw": 9.6,
 "delta": -110, "passed": true, "rule_version": "r-1",
 "basis": "每份 sodium_mg 实际值 10 mg（未舍入 9.6，按规则保留 0 位）≤ 阈值 120 mg（规则版本 r-1） → 达标"}
```

`actual` 为按规则位数舍入后的每份值（与标签展示值一致，参与比较），
`delta` = 实际值 − 阈值（同位数舍入），`basis` 为文字版判定依据。
声明定义随规则体哈希进入请求指纹：换用阈值不同的规则版本会得到新的
计算记录，历史记录始终按其锁定的规则版本复算。

## 快速示例

```bash
# 1) 原料资料（营养按每 100g 声明）
curl -X POST localhost:5000/api/ingredients -H 'Content-Type: application/json' -d '{
  "release_version":"ing-1",
  "ingredients":[
    {"code":"PASTA","basis_amount":100,"basis_unit":"g",
     "nutrition":{"energy_kcal":350,"protein":13},
     "allergens":{"gluten":"contains","milk":"free"}},
    {"code":"TOMATO","basis_amount":100,"basis_unit":"g",
     "nutrition":{"energy_kcal":20,"protein":1},
     "allergens":{"gluten":"free","milk":"free"}}]}'

# 2) 单位换算
curl -X POST localhost:5000/api/units -H 'Content-Type: application/json' -d '{
  "version":"u-1","conversions":[
    {"from_unit":"kg","to_unit":"g","factor":1000},
    {"from_unit":"portion","to_unit":"g","factor":100}]}'

# 3) 标签规则
curl -X POST localhost:5000/api/rules -H 'Content-Type: application/json' -d '{
  "version":"r-1",
  "required_nutrients":[
    {"name":"energy_kcal","daily_value":2000,"decimals":0},
    {"name":"protein","daily_value":50,"decimals":1}],
  "required_allergens":["gluten","milk"]}'

# 4) 子配方 SAUCE（产量 1000g，10 份）与嵌套配方 MEAL（按 portion 引用 SAUCE）
curl -X POST localhost:5000/api/recipes -H 'Content-Type: application/json' -d '{
  "code":"SAUCE","version":"1",
  "yield":{"qty":1000,"unit":"g"},"servings":10,
  "components":[{"kind":"ingredient","code":"TOMATO","qty":600,"unit":"g"}]}'

curl -X POST localhost:5000/api/recipes -H 'Content-Type: application/json' -d '{
  "code":"MEAL","version":"1",
  "yield":{"qty":2000,"unit":"g"},"servings":4,
  "components":[
    {"kind":"ingredient","code":"PASTA","qty":0.8,"unit":"kg"},
    {"kind":"recipe","code":"SAUCE","version":"1","qty":10,"unit":"portion"}]}'

# 5) 推导标签（可带 servings_override）
curl -X POST localhost:5000/api/computations -H 'Content-Type: application/json' -d '{
  "recipe_code":"MEAL","recipe_version":"1",
  "ingredient_release":"ing-1","unit_version":"u-1","rule_version":"r-1"}'
```

计算结果中的过敏原来源路径形如：

```text
components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1)
```

## 试算比较（POST /api/scenarios/compare）

临时换料还没到发布新配方版本的阶段时，用试算先把影响算清。
输入基准配方与锁定的原料/单位/规则版本（同 `POST /api/computations`），
外加一组 `adjustments`；接口沿用递归展开与声明校验分别推导基准与候选，
并排返回差异。**全程只读**：不写 `recipe`/`computation` 表、不累计命中次数、
不影响正式计算的指纹缓存（`persisted: false`）。

```json
{
  "recipe_code": "MEAL", "recipe_version": "1",
  "ingredient_release": "ing-1", "unit_version": "u-1", "rule_version": "r-1",
  "adjustments": [
    {"op": "set_qty",
     "path": "components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1)",
     "qty": 300},
    {"op": "replace", "path": "components[0](ingredient:PASTA@1)",
     "component": {"kind": "ingredient", "code": "NUT_PESTO",
                   "qty": 0.8, "unit": "kg"}},
    {"op": "remove",
     "path": "components[1](recipe:SAUCE@1)[1](ingredient:BASIL@1)"}
  ]
}
```

- **路径**：与计算结果/错误中的字段路径同格式，三种写法等价——
  规范注解串 `components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1)`、
  简写下标串 `components[1][0]`、下标数组 `[1, 0]`。
  注解（kind/code@version）会与实际成分逐一核对，不符即 `INVALID_PATH`。
- **op**：
  - `set_qty`：改成分用量（`qty` 必填正数，`unit` 可选，缺省保持原单位）；
  - `replace`：整体替换成分（`component.kind/code` 必填，`version` 缺省 `"1"`，
    `qty`/`unit` 缺省**继承**被替换成分）；
  - `remove`：移除成分。
- **顺序生效**：调整按提交顺序应用，后一条的路径基于前面调整后的状态解析
  （`remove` 会使同层后续成分下标前移）；同一子配方被引用多次时，
  路径调整只作用于被命中的那次出现。父级 `replace`/`remove` 会废弃
  该路径下此前调整保存的子级覆盖，候选仅按替换/移除后的组件树计算。
- **响应**：`baseline_fingerprint`（基准请求指纹，与正式计算同口径）、
  `candidate_hash`（候选内容哈希）、`matched_paths`（每条调整实际命中的
  规范组件路径）、`baseline`/`candidate` 两侧完整结果，以及 `diff`——
  每份营养与 NRV% 差值、原料占比变化（`added/removed/changed/unchanged`）、
  过敏原变化（`added/removed/escalated/de-escalated/unchanged`）。

试算特有的 4xx（字段定位到 `adjustments[i]`，候选展开期的单位/声明错误
也会归因到引发它的那条调整，`details.component_path` 保留原始组件路径）：

| HTTP | code | 触发场景 |
|---|---|---|
| 422 | `INVALID_PATH` | 调整路径失效：下标越界、注解与实际成分不符、下钻原料成分 |
| 422 | `EMPTY_RECIPE` | `remove` 使配方（或某层子配方出现）不再含任何成分 |

## 错误模型（4xx，均带字段路径）

```json
{"error": {"code": "UNIT_CONFLICT",
           "message": "单位冲突：'ml' 与 'g' 之间不存在换算路径",
           "field": "components[0](ingredient:TOMATO@1).unit",
           "details": {"from_unit": "ml", "to_unit": "g", "reason": "no_path"}}}
```

| HTTP | code | 触发场景 |
|---|---|---|
| 400 | `MALFORMED` | JSON 非法、字段缺失/类型错误/非正数；声明营养素未在 `required_nutrients` 中、无效比较方向（field 定位 `claims[i].nutrient` / `claims[i].direction`） |
| 404 | `NOT_FOUND` | 锁定版本（原料/单位/规则）或根配方不存在 |
| 409 | `CONFLICT` / `UNIT_CONFLICT` / `CLAIM_CONFLICT` | 不可变版本被改内容重提；换算表因子自相矛盾；同名声明在同一规则版本中定义矛盾（阈值冲突，field 定位 `claims[i].threshold`） |
| 422 | `CIRCULAR_REFERENCE` | 配方树存在环（details 给出 cycle） |
| 422 | `UNKNOWN_INGREDIENT` / `UNKNOWN_RECIPE` | 引用了锁定资料中不存在的原料/配方版本 |
| 422 | `UNIT_CONFLICT` | 未知单位、引用单位无法换算到子配方产量单位、出现量无法换算到原料基准单位或根配方产量单位（如质量 g 与体积 ml 之间无换算） |
| 422 | `MISSING_DECLARATION` | 规则要求的营养素/过敏原在某个叶子原料上未声明 |
| 422 | `INVALID_PATH` | 试算调整路径失效：下标越界、注解与实际成分不符、下钻原料成分（field 定位 `adjustments[i].path`） |
| 422 | `EMPTY_RECIPE` | 试算 `remove` 使配方（或某层子配方出现）不再含任何成分（field 定位 `adjustments[i]`） |

字段路径按引用链拼装，例如
`components[1](recipe:SAUCE@1)[0](ingredient:TOMATO@1).allergens.milk`，
可直接定位到出错的那一层配方的那一个成分。

## API 一览

```
POST   /api/ingredients                 提交原料资料版本     GET  /api/ingredients[?…]
GET    /api/ingredients/<release>       查看原料资料版本
POST   /api/units                       提交单位换算表版本   GET  /api/units[/<version>]
POST   /api/rules                       提交标签规则版本     GET  /api/rules[/<version>]
POST   /api/recipes                     提交配方版本         GET  /api/recipes?code=
GET    /api/recipes/<code>/<version>    查看配方版本
POST   /api/computations                推导（指纹命中返回 200，否则 201）
POST   /api/scenarios/compare           临时换料试算比较（只读不落库）
GET    /api/computations/<id>           取回计算摘要
GET    /api/computations/fingerprint/<fp>   指纹反查记录
GET    /api/computations/<id>/export    导出含计算依据的完整 JSON
GET    /api/recipes/<code>/<version>/history  按配方追溯计算历史（含作为子配方，depth>0）
GET    /health
```

导出文档包含：锁定的四个版本号与 `body_hash`、展开闭包、完整成分树、
每个叶子的基准换算量/根产量单位换算量与营养贡献、跨配方边界缩放记录、
实际使用的单位换算路径、营养声明逐条判定结果（`claims`）以及文字版计算口径。

## 目录结构

```
app/
  db.py          sqlite3 schema（版本表/配方表/计算表/闭包关系表）
  errors.py      统一 4xx 模型
  units.py       单位换算图 + 闭包一致性 + BFS 换算路径
  validation.py  载荷形状校验
  engine.py      递归展开、营养/占比/过敏原推导、营养声明判定、指纹、导出文档
  scenario.py    试算比较：按路径调整、基准/候选并排推导、差异汇总（不落库）
  api.py         Flask 路由
  app_factory.py 应用工厂与错误序列化
tests/           test_api.py / test_scenarios.py / test_claims.py（50 个端到端测试）
run.py
```
