"""配料表编排：按锁定配方树递归计算各层投料占比，依标签规则生成可直接上标签的配料表。

两种复合配料处理模式（``ingredient_list.mode``）：

- ``parenthesize``（默认）：复合配料保留括号结构——
  ``MEAT（猪肉、水、酱油（大豆、食盐））``；低于复合展开阈值的复合配料折叠为
  括号简式，括号内仅保留强制展示的添加剂；低于辅料省略阈值的复合配料整体省略，
  其强制添加剂上提至父展示层级。
- ``expand``：完全展开到叶子原料，跨层同码原料全部合并后按成品占比降序。

无论哪种模式，**同一展示层级的同码原料先合并、再降序排列**，每次出现的来源
路径（与计算结果/错误中的字段路径同格式）逐条保留，便于审核追溯。
"""

from .common import round_half_up

PCT_NDIGITS = 2
QTY_NDIGITS = 4


# ---- 出现树构建与同层同码合并 -------------------------------------------------

def _build_occurrence(node, depth):
    """把展开树的一个节点转成“出现”节点；复合节点的子节点先做同层同码合并。"""
    if node["kind"] == "ingredient":
        return {
            "kind": "ingredient",
            "code": node["code"],
            "name": node["name"],
            "category": node.get("category", "ingredient"),
            "qty": node["root_unit_qty"],
            "sources": [
                {"path": node["path"],
                 "root_unit_qty": round_half_up(node["root_unit_qty"], QTY_NDIGITS),
                 "depth": node.get("depth", depth)}
            ],
        }
    children = [_build_occurrence(c, depth + 1) for c in node["children"]]
    children = _merge_same_code(children)
    qty = sum(c["qty"] for c in children)
    return {
        "kind": "compound",
        "code": node["code"],
        "version": node["version"],
        "name": node["name"],
        "qty": qty,
        "depth": depth,
        "path": node.get("path", "components"),
        "sources": [
            {"path": node.get("path", "components"),
             "root_unit_qty": round_half_up(qty, QTY_NDIGITS),
             "depth": depth,
             "recipe": {"code": node["code"], "version": node["version"]}}
        ],
        "children": children,
    }


def _merge_same_code(nodes):
    """同一展示层级同码原料 / 同码同版复合配料合并（数量相加、来源合并、
    复合配料的子层级递归再合并），保持各编码首次出现的顺序。"""
    groups = {}
    order = []
    for n in nodes:
        key = ("i", n["code"]) if n["kind"] == "ingredient" \
            else ("r", n["code"], n["version"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)

    merged = []
    for key in order:
        ns = groups[key]
        node = {**ns[0], "qty": sum(n["qty"] for n in ns),
                "sources": [s for n in ns for s in n["sources"]]}
        if node["kind"] == "compound":
            node["children"] = _merge_same_code(
                [c for n in ns for c in n["children"]]
            )
        merged.append(node)
    return merged


def _collect_protected(node, mandatory, acc):
    """汇总子树中受保护（添加剂类别或强制名单）叶子，按编码合并。"""
    if node["kind"] == "ingredient":
        if node["category"] == "additive" or node["code"] in mandatory:
            frag = acc.get(node["code"])
            if frag is None:
                acc[node["code"]] = {
                    "kind": "ingredient",
                    "code": node["code"],
                    "name": node["name"],
                    "category": node["category"],
                    "qty": node["qty"],
                    "sources": list(node["sources"]),
                    "origins": [],
                }
            else:
                frag["qty"] += node["qty"]
                frag["sources"].extend(node["sources"])
        return
    for c in node["children"]:
        _collect_protected(c, mandatory, acc)


def _nonprotected_codes(node, mandatory):
    """子树中全部非保护叶子编码（用于省略白名单判定）。"""
    if node["kind"] == "ingredient":
        if node["category"] == "additive" or node["code"] in mandatory:
            return set()
        return {node["code"]}
    out = set()
    for c in node["children"]:
        out |= _nonprotected_codes(c, mandatory)
    return out


# ---- 条目格式化 ---------------------------------------------------------------

def _pct(qty, base):
    return round_half_up(qty / base * 100.0, PCT_NDIGITS) if base else 0.0


def _ingredient_entry(occ, parent_qty, root_qty, unit, status, reason,
                      origins=None):
    entry = {
        "type": "ingredient",
        "code": occ["code"],
        "name": occ["name"],
        "category": occ["category"],
        "root_unit_qty": round_half_up(occ["qty"], QTY_NDIGITS),
        "unit": unit,
        "proportion_pct": _pct(occ["qty"], root_qty),
        "within_parent_pct": _pct(occ["qty"], parent_qty),
        "status": status,
        "sources": occ["sources"],
        "reason": reason,
    }
    if origins:
        entry["hoisted_from"] = origins
    return entry


def _merge_hoist_into_kept(kept, fragments, parent_qty, root_qty, unit,
                           minor_pct, rule_version):
    """把被省略复合配料上提的强制片段并入本层保留项（同码合并）。"""
    for frag in sorted(fragments.values(), key=lambda f: -f["qty"]):
        same = next((e for e in kept
                     if e["type"] == "ingredient" and e["code"] == frag["code"]),
                    None)
        via = "、".join(frag["origins"])
        if same is not None:
            # 同码原料本层已有保留项：数量与来源合并，记录上提来源
            same["root_unit_qty"] = round_half_up(
                same["root_unit_qty"] + frag["qty"], QTY_NDIGITS)
            same["proportion_pct"] = _pct(
                same["root_unit_qty"], root_qty)
            same["within_parent_pct"] = _pct(
                same["root_unit_qty"], parent_qty)
            same["sources"].extend(frag["sources"])
            hoisted = same.setdefault("hoisted_from", [])
            for o in frag["origins"]:
                if o not in hoisted:
                    hoisted.append(o)
            same["status"] = "forced"
            same["reason"] = (
                f"同码原料本层出现与自省略复合配料（{via}）上提的强制项已合并；"
                f"合并后成品占比 {same['proportion_pct']}%，食品添加剂/强制添加剂"
                f"不受辅料省略阈值 {minor_pct:g}% 限制，强制展示"
                f"（规则版本 {rule_version}），共 {len(same['sources'])} 处来源")
        else:
            reason = (
                f"复合配料（{via}）成品占比低于辅料省略阈值 {minor_pct:g}% 整体省略，"
                f"其强制添加剂 {frag['name']}（成品占比 {_pct(frag['qty'], root_qty)}%）"
                f"上提至本展示层级强制展示（规则版本 {rule_version}），"
                f"共 {len(frag['sources'])} 处来源")
            kept.append(_ingredient_entry(
                frag, parent_qty, root_qty, unit, "forced", reason,
                origins=list(frag["origins"])))


def _sort_kept(kept):
    """同层保留项按投料量（占比）降序；并列时保持配方树首次出现顺序。"""
    kept.sort(key=lambda e: -e["root_unit_qty"])


# ---- 逐层处理 -----------------------------------------------------------------

def _evaluate_level(nodes, parent_qty, root_qty, unit, cfg, depth, collapsed):
    """处理一个展示层级的合并后出现节点。

    返回 (kept, omitted, hoist)：
      - kept：本层展示的结构化条目（未排序，调用方统一排序）；
      - omitted：本层被省略条目的审计明细（原料或复合配料）；
      - hoist：自本层被省略复合配料上提的受保护原料片段，交由父层级并入。

    collapsed=True 表示外层复合配料按折叠简式展示：本层非保护成分一律隐藏，
    受保护成分作为括号内强制项保留。
    """
    minor_pct = cfg["minor_threshold_pct"]
    compound_pct = cfg["compound_threshold_pct"]
    mandatory = set(cfg["mandatory_additives"])
    whitelist = set(cfg["omit_eligible"]) or None
    rule_version = cfg["rule_version"]

    kept, omitted, hoist = [], [], {}

    def lift(fragments):
        for code, frag in fragments.items():
            tgt = hoist.setdefault(code, {
                "kind": "ingredient", "code": frag["code"],
                "name": frag["name"], "category": frag["category"],
                "qty": 0.0, "sources": [], "origins": []})
            tgt["qty"] += frag["qty"]
            tgt["sources"].extend(frag["sources"])
            for o in frag.get("origins", []):
                if o not in tgt["origins"]:
                    tgt["origins"].append(o)

    for node in nodes:
        pct = _pct(node["qty"], root_qty)
        within = _pct(node["qty"], parent_qty)

        if node["kind"] == "ingredient":
            protected_additive = node["category"] == "additive"
            protected_mandatory = node["code"] in mandatory
            n_src = len(node["sources"])
            merged_note = (
                f"同一展示层级同码出现 {n_src} 次已合并（占比相加、来源保留）；"
                if n_src > 1 else "")
            if collapsed:
                if protected_additive or protected_mandatory:
                    reason = (
                        f"{merged_note}外层复合配料按折叠简式展示，"
                        f"{node['name']} 属{'食品添加剂' if protected_additive else '强制展示添加剂'}"
                        f"（成品占比 {pct}%），在括号内强制保留"
                        f"（规则版本 {rule_version}）")
                    kept.append(_ingredient_entry(
                        node, parent_qty, root_qty, unit, "forced", reason))
                else:
                    reason = (
                        f"外层复合配料成品占比低于复合配料展开阈值 "
                        f"{compound_pct:g}%，按折叠简式展示，非强制成分 "
                        f"{node['name']}（成品占比 {pct}%）不在括号内展开"
                        f"（规则版本 {rule_version}）")
                    omitted.append(_ingredient_entry(
                        node, parent_qty, root_qty, unit, "omitted", reason))
                continue

            if protected_additive:
                reason = (
                    f"{merged_note}食品添加剂（资料类别 category=additive），"
                    f"成品占比 {pct}% 不受辅料省略阈值 {minor_pct:g}% 限制，"
                    f"必须展示（规则版本 {rule_version}）")
                kept.append(_ingredient_entry(
                    node, parent_qty, root_qty, unit, "forced", reason))
            elif protected_mandatory:
                if pct < minor_pct:
                    reason = (
                        f"{merged_note}列入规则版本 {rule_version} 的 "
                        f"mandatory_additives 强制名单，成品占比 {pct}% 虽低于"
                        f"辅料省略阈值 {minor_pct:g}%，仍必须展示")
                    status = "forced"
                else:
                    reason = (
                        f"{merged_note}列入 mandatory_additives 且成品占比 "
                        f"{pct}% ≥ 辅料省略阈值 {minor_pct:g}%，正常展示"
                        f"（规则版本 {rule_version}）")
                    status = "shown"
                kept.append(_ingredient_entry(
                    node, parent_qty, root_qty, unit, status, reason))
            elif pct < minor_pct:
                if whitelist is not None and node["code"] not in whitelist:
                    reason = (
                        f"{merged_note}成品占比 {pct}% 虽低于辅料省略阈值 "
                        f"{minor_pct:g}%，但编码 {node['code']} 不在 "
                        f"omit_eligible 可省略白名单内，不予省略，正常展示"
                        f"（规则版本 {rule_version}）")
                    kept.append(_ingredient_entry(
                        node, parent_qty, root_qty, unit, "shown", reason))
                else:
                    scope = "且编码在 omit_eligible 白名单内" if whitelist \
                        else "（未配置 omit_eligible 白名单，阈值对所有非强制项生效）"
                    reason = (
                        f"{merged_note}成品占比 {pct}% 低于辅料省略阈值 "
                        f"{minor_pct:g}%{scope}，作为辅料省略"
                        f"（规则版本 {rule_version}）")
                    omitted.append(_ingredient_entry(
                        node, parent_qty, root_qty, unit, "omitted", reason))
            else:
                reason = (
                    f"{merged_note}成品占比 {pct}% ≥ 辅料省略阈值 "
                    f"{minor_pct:g}%，正常展示（规则版本 {rule_version}）")
                kept.append(_ingredient_entry(
                    node, parent_qty, root_qty, unit, "shown", reason))
            continue

        # ---- 复合配料 ----
        protected = {}
        _collect_protected(node, mandatory, protected)
        nonprot = _nonprotected_codes(node, mandatory)
        whitelist_ok = whitelist is None or nonprot <= whitelist
        below_minor = pct < minor_pct

        audit = {
            "type": "compound",
            "code": node["code"],
            "version": node["version"],
            "name": node["name"],
            "root_unit_qty": round_half_up(node["qty"], QTY_NDIGITS),
            "unit": unit,
            "proportion_pct": pct,
            "within_parent_pct": within,
            "sources": node["sources"],
        }
        merge_note = (
            f"同一展示层级同码（同版）复合配料出现 {len(node['sources'])} 次已合并；"
            if len(node["sources"]) > 1 else "")

        if below_minor and whitelist_ok:
            # 整体省略；受保护添加剂上提至父展示层级
            hoisted_items = []
            for frag in protected.values():
                for o in (node["path"],):
                    if o not in frag["origins"]:
                        frag["origins"].append(o)
                hoisted_items.append({
                    "code": frag["code"], "name": frag["name"],
                    "category": frag["category"],
                    "root_unit_qty": round_half_up(frag["qty"], QTY_NDIGITS),
                    "proportion_pct": _pct(frag["qty"], root_qty),
                    "sources": frag["sources"],
                    "hoisted_from": [node["path"]],
                })
            hoisted_items.sort(key=lambda e: -e["root_unit_qty"])
            lift(protected)
            if protected:
                wl_note = ("其非强制辅料均在 omit_eligible 白名单内，"
                           if whitelist is not None else "")
                audit["status"] = "omitted_with_hoist"
                audit["hoisted"] = hoisted_items
                audit["reason"] = (
                    f"{merge_note}复合配料 {node['name']}（{node['code']}@{node['version']}）"
                    f"成品占比 {pct}% 低于辅料省略阈值 {minor_pct:g}%，{wl_note}"
                    f"整体省略；强制添加剂 {('、'.join(h['name'] for h in hoisted_items))}"
                    f" 上提至父展示层级展示（规则版本 {rule_version}）")
            else:
                audit["status"] = "omitted"
                audit["hoisted"] = []
                audit["reason"] = (
                    f"{merge_note}复合配料 {node['name']}（{node['code']}@{node['version']}）"
                    f"成品占比 {pct}% 低于辅料省略阈值 {minor_pct:g}%，"
                    f"全部成分均为可省略辅料，整体省略（规则版本 {rule_version}）")
            omitted.append(audit)
            continue

        collapse = pct < compound_pct
        sub_kept, sub_omitted, sub_hoist = _evaluate_level(
            node["children"], node["qty"], root_qty, unit, cfg,
            depth + 1, collapse)

        if collapse:
            # 折叠简式：子层非保护成分全部隐藏（已在 sub_omitted 中给出依据），
            # 深层省略复合配料上提的片段并入括号强制项
            _merge_hoist_into_kept(
                sub_kept, sub_hoist, node["qty"], root_qty, unit,
                minor_pct, rule_version)
            _sort_kept(sub_kept)
            forced_names = "、".join(e["name"] for e in sub_kept) or "无"
            audit.update({
                "status": "collapsed",
                "expanded": False,
                "items": sub_kept,
                "forced_items": sub_kept,
                "omitted": sub_omitted,
                "reason": (
                    f"{merge_note}复合配料 {node['name']}（{node['code']}@{node['version']}）"
                    f"成品占比 {pct}% 低于复合配料展开阈值 {compound_pct:g}%，"
                    f"折叠为括号简式，括号内仅保留强制展示的添加剂：{forced_names}"
                    f"（规则版本 {rule_version}）"),
            })
        else:
            _merge_hoist_into_kept(
                sub_kept, sub_hoist, node["qty"], root_qty, unit,
                minor_pct, rule_version)
            _sort_kept(sub_kept)
            forced_items = [e for e in sub_kept if e["status"] == "forced"]
            audit.update({
                "status": "shown",
                "expanded": True,
                "items": sub_kept,
                "forced_items": forced_items,
                "omitted": sub_omitted,
                "reason": (
                    f"{merge_note}复合配料 {node['name']}（{node['code']}@{node['version']}）"
                    f"成品占比 {pct}% ≥ 复合配料展开阈值 {compound_pct:g}%，"
                    f"保留括号结构；括号内同码原料合并后按占比降序排列，"
                    f"共 {len(sub_kept)} 项展示、{len(sub_omitted)} 项省略"
                    f"（规则版本 {rule_version}）"),
            })
        kept.append(audit)

    # 本层被省略复合配料上提的片段并入本层保留项
    _merge_hoist_into_kept(kept, hoist, parent_qty, root_qty, unit,
                           minor_pct, rule_version)
    return kept, omitted, hoist


# ---- 标签文本 -----------------------------------------------------------------

def _render_entry(entry):
    if entry["type"] == "ingredient":
        return entry["name"]
    items = entry.get("items", [])
    if items:
        return f"{entry['name']}（{'、'.join(_render_entry(e) for e in items)}）"
    return entry["name"]


def render_label_text(entries):
    return "配料表：" + "、".join(_render_entry(e) for e in entries)


# ---- 完全展开模式 -------------------------------------------------------------

def _flatten_leaves(node, acc):
    if node["kind"] == "ingredient":
        acc.append(dict(node))
        return
    for c in node["children"]:
        _flatten_leaves(c, acc)


def _compose_expand(root_children, root_qty, unit, cfg):
    flat = []
    for n in root_children:
        _flatten_leaves(n, flat)
    nodes = _merge_same_code(flat)
    kept, omitted, hoist = _evaluate_level(
        nodes, root_qty, root_qty, unit, cfg, 1, False)
    # 展开模式不存在复合层，理论上不会产生 hoist；防御性并入
    _merge_hoist_into_kept(kept, hoist, root_qty, root_qty, unit,
                           cfg["minor_threshold_pct"], cfg["rule_version"])
    _sort_kept(kept)
    omitted.sort(key=lambda e: -e["root_unit_qty"])
    return kept, omitted


# ---- 入口 ---------------------------------------------------------------------

def compose_ingredient_list(tree, root, rules_pack, ingredient_pack):
    """根据锁定的成分树与标签规则编排配料表。

    返回 ``ingredient_list`` 结果块：可直接印刷的 ``text``、结构化明细
    ``items``/``omitted``，以及每项自带的 ``reason`` 处理依据。
    占比无法换算到根产量单位的情形在展开期已按 UNIT_CONFLICT 失败，
    因此此处所有占比均可计算。
    """
    body = rules_pack["body"].get("ingredient_list", {})
    cfg = {
        "mode": body.get("mode", "parenthesize"),
        "minor_threshold_pct": body.get("minor_threshold_pct", 2.0),
        "compound_threshold_pct": body.get("compound_threshold_pct", 25.0),
        "mandatory_additives": body.get("mandatory_additives", []),
        "omit_eligible": body.get("omit_eligible", []),
        "rule_version": rules_pack["version"],
    }
    root_qty = root["yield_qty"]
    unit = root["yield_unit"]
    root_children = _merge_same_code(
        [_build_occurrence(c, 1) for c in tree["children"]]
    )

    if cfg["mode"] == "expand":
        kept, omitted = _compose_expand(root_children, root_qty, unit, cfg)
    else:
        kept, omitted, _ = _evaluate_level(
            root_children, root_qty, root_qty, unit, cfg, 1, False)
        _sort_kept(kept)
        omitted.sort(key=lambda e: -e["root_unit_qty"])

    text = render_label_text(kept)

    # 强制名单核对：未出现 / 类别不是添加剂的编码给出说明（不构成失败）
    notes = []
    used = {}
    for n in root_children:
        _index_leaves(n, used)
    for code in cfg["mandatory_additives"]:
        if code not in used:
            notes.append(
                f"强制添加剂 {code} 未出现在配方树的任何原料出现中"
                f"（原料资料 {ingredient_pack['version']}），无需展示")
        elif used[code] != "additive":
            notes.append(
                f"强制添加剂 {code} 在原料资料 {ingredient_pack['version']} 中"
                f"类别为 {used[code]}，规则仍按 mandatory_additives 强制展示")

    def _tally(entries, acc):
        for e in entries:
            if e.get("type") == "compound":
                acc[e["status"]] = acc.get(e["status"], 0) + 1
                _tally(e.get("items", []), acc)
                _tally(e.get("omitted", []), acc)
                continue
            acc[e["status"]] = acc.get(e["status"], 0) + 1

    tallied = {}
    _tally(kept, tallied)
    _tally(omitted, tallied)

    basis = (
        f"配料表按锁定标签规则 {cfg['rule_version']} 编排："
        f"1) 各层投料占比 = 叶子出现量（已沿配方边界连乘缩放并换算到根产量单位 "
        f"{unit}）÷ 根产量 {root_qty:g} {unit} × 100；"
        f"2) 复合配料处理模式 = {cfg['mode']}"
        + ("（保留括号结构）" if cfg["mode"] == "parenthesize"
           else "（完全展开到叶子原料）")
        + "；3) 同一展示层级同码原料合并数量、保留全部来源路径后按占比降序；"
        f"4) 成品占比低于辅料省略阈值 {cfg['minor_threshold_pct']:g}% 的辅料可省略"
        + ("（仅 omit_eligible 白名单内编码）" if cfg["omit_eligible"]
           else "（未配白名单，对所有非强制项生效）")
        + "；5) category=additive 的食品添加剂与 mandatory_additives 名单项"
        "不受省略阈值限制，省略复合配料时其上提至父展示层级展示；"
        + (f"6) parenthesize 模式下复合配料成品占比低于 "
           f"{cfg['compound_threshold_pct']:g}% 时折叠为括号简式，括号内仅保留"
           "强制添加剂。" if cfg["mode"] == "parenthesize"
           else "6) expand 模式下跨层同码原料全部合并，不保留括号结构。")
    )

    return {
        "text": text,
        "mode": cfg["mode"],
        "rule_version": cfg["rule_version"],
        "thresholds": {
            "minor_threshold_pct": cfg["minor_threshold_pct"],
            "compound_threshold_pct": cfg["compound_threshold_pct"],
        },
        "mandatory_additives": cfg["mandatory_additives"],
        "omit_eligible": cfg["omit_eligible"],
        "root_yield": {"qty": root_qty, "unit": unit},
        "items": kept,
        "omitted": omitted,
        "notes": notes,
        "counts": {
            "displayed": len(kept),
            "forced": tallied.get("forced", 0),
            "shown": tallied.get("shown", 0),
            "omitted": tallied.get("omitted", 0),
        },
        "basis": basis,
    }


def _index_leaves(node, acc):
    if node["kind"] == "ingredient":
        acc.setdefault(node["code"], node["category"])
        return
    for c in node["children"]:
        _index_leaves(c, acc)
