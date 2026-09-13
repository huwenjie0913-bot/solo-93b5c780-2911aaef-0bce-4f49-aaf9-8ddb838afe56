"""单位换算图。

换算表中每条声明 ``from_unit -> to_unit: factor`` 表示
``1 from_unit = factor to_unit``，自动加入反向边。提交时做闭包一致性检查：
若同两个单位经多条路径换算因子不一致（相对误差 > 容差），视为单位冲突。
"""

import math

from .errors import unit_conflict

TOL = 1e-9


class UnitGraph:
    def __init__(self, units, edges):
        # units: 出现过的所有单位集合；edges: {from: {to: (factor, declared)}}
        self.units = set(units)
        self.edges = {u: {} for u in self.units}
        for f, t, factor in edges:
            self._put(f, t, factor, declared=True)
            self._put(t, f, 1.0 / factor, declared=False)

    def _put(self, f, t, factor, declared):
        old = self.edges.setdefault(f, {}).get(t)
        if old is not None and not math.isclose(old[0], factor, rel_tol=TOL, abs_tol=TOL):
            raise unit_conflict(
                f"单位换算声明冲突：{f} -> {t} 同时为 {old[0]:g} 和 {factor:g}",
                f"conversions[{f}->{t}]",
                {"existing_factor": old[0], "new_factor": factor},
            )
        self.edges[f][t] = (factor, declared)

    # -- 闭包一致性检查 -----------------------------------------------------
    def check_transitive_consistency(self):
        """Floyd 闭包：检查所有可达单位对的路径因子是否唯一。"""
        dist = {u: {v: (1.0 if u == v else None) for v in self.units} for u in self.units}
        for f, nbrs in self.edges.items():
            for t, (factor, _) in nbrs.items():
                dist[f][t] = factor
        for k in self.units:
            for i in self.units:
                dik = dist[i][k]
                if dik is None:
                    continue
                for j in self.units:
                    dkj = dist[k][j]
                    if dkj is None:
                        continue
                    cand = dik * dkj
                    cur = dist[i][j]
                    if cur is None:
                        dist[i][j] = cand
                    elif not math.isclose(cur, cand, rel_tol=TOL, abs_tol=TOL):
                        raise unit_conflict(
                            f"单位换算表存在传递冲突：{i} -> {j} 经不同路径得到 "
                            f"{cur:g} 与 {cand:g}",
                            "conversions",
                            {
                                "from_unit": i,
                                "to_unit": j,
                                "factor_a": cur,
                                "factor_b": cand,
                            },
                        )

    # -- 换算 ---------------------------------------------------------------
    def convert(self, qty, from_unit, to_unit, field):
        """返回 (换算后数量, 审计路径)；找不到路径时抛 UNIT_CONFLICT。"""
        if from_unit == to_unit:
            return qty, [from_unit]
        if from_unit not in self.units:
            raise unit_conflict(
                f"未知单位 {from_unit!r}，换算表版本中未声明",
                field,
                {"unit": from_unit, "reason": "unknown_unit"},
            )
        if to_unit not in self.units:
            raise unit_conflict(
                f"未知单位 {to_unit!r}，换算表版本中未声明",
                field,
                {"unit": to_unit, "reason": "unknown_unit"},
            )
        # BFS 求一条最短路径
        prev = {from_unit: None}
        q = [from_unit]
        found = False
        while q:
            nxt = []
            for u in q:
                if u == to_unit:
                    found = True
                    break
                for v in self.edges.get(u, {}):
                    if v not in prev:
                        prev[v] = u
                        nxt.append(v)
            if found:
                break
            q = nxt
        if to_unit not in prev:
            raise unit_conflict(
                f"单位冲突：{from_unit!r} 与 {to_unit!r} 之间不存在换算路径",
                field,
                {"from_unit": from_unit, "to_unit": to_unit, "reason": "no_path"},
            )
        # 回溯路径并累乘
        chain = []
        cur = to_unit
        while cur is not None:
            chain.append(cur)
            cur = prev[cur]
        chain.reverse()
        result = qty
        for a, b in zip(chain, chain[1:]):
            result *= self.edges[a][b][0]
        return result, chain
