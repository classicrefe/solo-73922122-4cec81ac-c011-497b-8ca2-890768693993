"""两份已持久化发布清单的对照比较。

业务目标：影像训练数据工程师需要判断两次发布的病例切分还能否用于对照实验。
本模块完全只读，输入是两个清单的持久化快照（含已失效清单），不依赖当前生效
输入，也绝不修改任何输入数据或清单。

处理流程：
1. 严格校验每份快照：缺字段、病例重复、切分结果与快照不一致（分组非法、
   连通分量内不同组、配额计数不符、锁定未满足、引用未知患者等）一律拒绝，
   不产生报告；
2. 分别按“同一患者 + 患者亲属对（传递闭包）”构建连通分量，并保留每个分量
   内的原始亲属边；
3. 以两版共有的病例号为桥梁，用并查集把两版分量配对成“变化分量”；
4. 逐分量区分：
   - group_only      单纯分组变化：两版成员完全一致，仅训练/评估归属变化；
   - connectivity    连通范围变化：分量成员集合或其亲属边/患者构成发生变化
                     （新增/移除病例、亲属边增减、分量合并或分裂）；
   - group_and_connectivity 两者同时发生；
   - unchanged       无变化（默认不放入变化分量列表，仅计入统计）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .splitter import Case, RelativePair, build_components

GROUP_NAMES = {1: "训练集", 2: "评估集"}


class ReportValidationError(Exception):
    """清单快照无法通过完整性/一致性校验，拒绝生成对照报告。"""

    def __init__(self, errors: List[str]):
        super().__init__("；".join(errors))
        self.errors = errors


@dataclass
class ParsedManifest:
    """一份清单快照校验后的结构化结果。"""

    manifest_id: str
    dataset_version: int
    expired: bool
    created_at: str
    cases: List[Case]
    relatives: List[RelativePair]
    assignments: Dict[str, int]
    # 分量稳定键 -> 分量详情
    components: Dict[str, Dict[str, Any]]
    # case_id -> 所属分量键
    case_component: Dict[str, str]
    patient_of: Dict[str, str]
    locks: Dict[str, int] = field(default_factory=dict)
    quotas: Dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 快照校验与解析
# --------------------------------------------------------------------------- #
def _err(errors: List[str], cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def parse_and_validate_manifest(manifest: Dict[str, Any], label: str) -> ParsedManifest:
    """校验单份清单快照并解析为结构化数据。

    任何不通过都会抛出 ReportValidationError，错误信息带清单标识便于定位。
    """
    errors: List[str] = []
    prefix = f"[{label}]"

    if not isinstance(manifest, dict):
        raise ReportValidationError([f"{prefix} 清单数据无法读取或格式不是 JSON 对象。"])

    manifest_id = manifest.get("manifest_id", "未知")
    prefix = f"[{label} 清单 {str(manifest_id)[:8]}]"
    input_data = manifest.get("input")
    result = manifest.get("result")

    _err(errors, isinstance(input_data, dict), f"{prefix} 快照缺少 input 字段或类型错误。")
    _err(errors, isinstance(result, dict), f"{prefix} 快照缺少 result 字段或类型错误。")
    if errors:
        raise ReportValidationError(errors)

    # ---- 病例：缺字段、重复病例号、非法标签 ----------------------------- #
    raw_cases = input_data.get("cases")
    _err(errors, isinstance(raw_cases, list), f"{prefix} input.cases 缺失或不是列表。")
    cases: List[Case] = []
    seen_cases: Dict[str, int] = {}
    if isinstance(raw_cases, list):
        for idx, item in enumerate(raw_cases):
            if not isinstance(item, dict):
                errors.append(f"{prefix} cases 第 {idx + 1} 项不是对象：{item!r}")
                continue
            cid = item.get("case_id")
            pid = item.get("patient_id")
            lab = item.get("label")
            if not isinstance(cid, str) or not cid.strip():
                errors.append(f"{prefix} cases 第 {idx + 1} 项缺少有效病例号 case_id。")
                continue
            cid = cid.strip()
            if not isinstance(pid, str) or not pid.strip():
                errors.append(f"{prefix} 病例 {cid} 缺少有效患者号 patient_id。")
                continue
            if lab not in (0, 1) or isinstance(lab, bool):
                errors.append(f"{prefix} 病例 {cid} 的标签必须是 0 或 1，实际为 {lab!r}。")
                continue
            if cid in seen_cases:
                errors.append(
                    f"{prefix} 病例号 {cid} 重复（出现于第 {seen_cases[cid]} 项与第 {idx + 1} 项）。"
                )
                continue
            seen_cases[cid] = idx + 1
            cases.append(Case(case_id=cid, patient_id=pid.strip(), label=lab))
    if not cases and isinstance(raw_cases, list):
        errors.append(f"{prefix} 快照中没有任何病例，无法对照。")

    # ---- 亲属对：缺字段、引用未知患者 ----------------------------------- #
    raw_rels = input_data.get("relatives", [])
    relatives: List[RelativePair] = []
    known_patients = {c.patient_id for c in cases}
    if raw_rels is None:
        raw_rels = []
    _err(errors, isinstance(raw_rels, list), f"{prefix} input.relatives 不是列表。")
    if isinstance(raw_rels, list):
        seen_pairs = set()
        for idx, item in enumerate(raw_rels):
            if not isinstance(item, dict):
                errors.append(f"{prefix} relatives 第 {idx + 1} 项不是对象：{item!r}")
                continue
            a, b = item.get("patient_a"), item.get("patient_b")
            if not isinstance(a, str) or not isinstance(b, str) or not a.strip() or not b.strip():
                errors.append(f"{prefix} relatives 第 {idx + 1} 项缺少有效患者号。")
                continue
            a, b = a.strip(), b.strip()
            if a == b:
                errors.append(f"{prefix} 亲属对两端不能是同一患者（第 {idx + 1} 项：{a}）。")
                continue
            if cases:
                if a not in known_patients:
                    errors.append(f"{prefix} 亲属边 {a}–{b} 引用了快照中不存在的患者 {a}。")
                if b not in known_patients:
                    errors.append(f"{prefix} 亲属边 {a}–{b} 引用了快照中不存在的患者 {b}。")
            key = tuple(sorted((a, b)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            relatives.append(RelativePair(patient_a=a, patient_b=b))

    # ---- 切分结果 assignments：缺字段、非法分组、覆盖不一致 -------------- #
    assignments_raw = result.get("assignments")
    assignments: Dict[str, int] = {}
    _err(errors, isinstance(assignments_raw, dict),
         f"{prefix} result.assignments 缺失或不是对象。")
    if isinstance(assignments_raw, dict):
        case_ids = {c.case_id for c in cases}
        for cid, grp in assignments_raw.items():
            if grp not in (1, 2):
                errors.append(f"{prefix} 病例 {cid} 的分组结果必须是 1 或 2，实际为 {grp!r}。")
                continue
            assignments[str(cid)] = grp
        missing = sorted(case_ids - set(assignments))
        extra = sorted(set(assignments) - case_ids)
        if missing:
            errors.append(f"{prefix} 切分结果缺少 {len(missing)} 个病例的分组：{', '.join(missing[:10])}"
                          + ("…" if len(missing) > 10 else ""))
        if extra:
            errors.append(f"{prefix} 切分结果包含快照中不存在的病例：{', '.join(extra[:10])}"
                          + ("…" if len(extra) > 10 else ""))

    # ---- 锁定与配额（快照内可能缺字段，缺配额不阻断其余一致性检查）------ #
    locks_raw = input_data.get("locks", {}) or {}
    locks: Dict[str, int] = {}
    if not isinstance(locks_raw, dict):
        errors.append(f"{prefix} input.locks 不是对象。")
    else:
        for cid, grp in locks_raw.items():
            if grp in (1, 2):
                locks[str(cid)] = grp
            else:
                errors.append(f"{prefix} 锁定项 {cid!r} 的分组必须是 1 或 2，实际为 {grp!r}。")

    quotas_raw = input_data.get("quotas")
    quotas: Dict[str, int] = {}
    if isinstance(quotas_raw, dict):
        for key in ("g1_label0", "g1_label1", "g2_label0", "g2_label1"):
            val = quotas_raw.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                quotas[key] = val
            else:
                errors.append(f"{prefix} 配额 {key} 缺失或不是非负整数，实际为 {val!r}。")
    else:
        errors.append(f"{prefix} input.quotas 缺失或不是对象。")

    if errors:
        raise ReportValidationError(errors)

    # ---- 结果与快照一致性复核 ------------------------------------------- #
    # 1) 连通分量内所有病例必须同组（同一患者 + 亲属传递）
    components_raw, _merges = build_components(cases, relatives)
    components: Dict[str, Dict[str, Any]] = {}
    case_component: Dict[str, str] = {}
    patient_of = {c.case_id: c.patient_id for c in cases}

    # 分量稳定键：用分量内最小病例号，输出顺序确定
    ordered_roots = sorted(components_raw, key=lambda root: min(components_raw[root]))
    for comp_index, root in enumerate(ordered_roots, start=1):
        members = sorted(components_raw[root])
        comp_key = f"CC{comp_index:03d}:{members[0]}"
        groups = {assignments[cid] for cid in members}
        if len(groups) > 1:
            offenders = [f"{cid}→{GROUP_NAMES[assignments[cid]]}" for cid in members]
            errors.append(
                f"{prefix} 连通分量 {comp_key} 内的病例未同组，违反同患者/亲属约束："
                + "，".join(offenders)
            )
        group = next(iter(groups)) if len(groups) == 1 else None
        comp_patients = sorted({patient_of[cid] for cid in members})
        # 原始亲属边：分量患者集合内部、且出现在快照亲属对中的边
        patient_set = set(comp_patients)
        edges = sorted(
            ({"patient_a": r.patient_a, "patient_b": r.patient_b}
             for r in relatives if r.patient_a in patient_set and r.patient_b in patient_set),
            key=lambda e: (e["patient_a"], e["patient_b"]),
        )
        components[comp_key] = {
            "component_key": comp_key,
            "cases": members,
            "patients": comp_patients,
            "relative_edges": edges,
            "group": group,
        }
        for cid in members:
            case_component[cid] = comp_key

    # 2) 锁定必须被切分结果满足
    for cid, grp in locks.items():
        if cid in assignments and assignments[cid] != grp:
            errors.append(
                f"{prefix} 病例 {cid} 被锁定到{GROUP_NAMES[grp]}，"
                f"但切分结果为{GROUP_NAMES[assignments[cid]]}，结果与快照不一致。"
            )

    # 3) 四格实际计数必须精确等于配额
    if len(quotas) == 4:
        actual = {k: 0 for k in quotas}
        for c in cases:
            actual[f"g{assignments[c.case_id]}_label{c.label}"] += 1
        if actual != quotas:
            errors.append(
                f"{prefix} 切分结果的四格计数 {actual} 与快照配额 {quotas} 不一致。"
            )

    if errors:
        raise ReportValidationError(errors)

    return ParsedManifest(
        manifest_id=str(manifest.get("manifest_id")),
        dataset_version=manifest.get("dataset_version"),
        expired=bool(manifest.get("expired")),
        created_at=str(manifest.get("created_at", "")),
        cases=cases,
        relatives=relatives,
        assignments=assignments,
        components=components,
        case_component=case_component,
        patient_of=patient_of,
        locks=locks,
        quotas=quotas,
    )


# --------------------------------------------------------------------------- #
# 跨版本分量匹配（并查集）
# --------------------------------------------------------------------------- #
class _UnionFind:
    def __init__(self, items: Sequence[str]):
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 固定方向，保证输出确定性
            if ra > rb:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _side_component_view(parsed: ParsedManifest, comp_key: str) -> Dict[str, Any]:
    comp = parsed.components[comp_key]
    return {
        "component_key": comp_key,
        "group": comp["group"],
        "group_name": GROUP_NAMES[comp["group"]] if comp["group"] else None,
        "cases": comp["cases"],
        "patients": comp["patients"],
        "relative_edges": comp["relative_edges"],
        "case_count": len(comp["cases"]),
        "patient_count": len(comp["patients"]),
        "edge_count": len(comp["relative_edges"]),
    }


def build_comparison_report(
    manifest_a: Dict[str, Any],
    manifest_b: Dict[str, Any],
) -> Dict[str, Any]:
    """构建两份清单的对照报告。校验失败抛 ReportValidationError。"""
    a = parse_and_validate_manifest(manifest_a, "旧版 A")
    b = parse_and_validate_manifest(manifest_b, "新版 B")

    keys_a = [f"A::{k}" for k in a.components]
    keys_b = [f"B::{k}" for k in b.components]
    uf = _UnionFind(keys_a + keys_b)

    # 以共有病例号为桥梁配对分量
    for cid in sorted(set(a.case_component) & set(b.case_component)):
        uf.union(f"A::{a.case_component[cid]}", f"B::{b.case_component[cid]}")

    groups: Dict[str, List[str]] = {}
    for key in keys_a + keys_b:
        groups.setdefault(uf.find(key), []).append(key)

    a_cases = {c.case_id for c in a.cases}
    b_cases = {c.case_id for c in b.cases}
    added_cases = sorted(b_cases - a_cases)      # 新版新增
    removed_cases = sorted(a_cases - b_cases)    # 新版移除
    shared_cases = sorted(a_cases & b_cases)

    # 病例级归属变化
    group_changes: List[Dict[str, Any]] = []
    for cid in shared_cases:
        ga, gb = a.assignments[cid], b.assignments[cid]
        if ga != gb:
            group_changes.append({
                "case_id": cid,
                "patient_id": a.patient_of[cid],
                "label": next(c.label for c in a.cases if c.case_id == cid),
                "group_a": ga, "group_a_name": GROUP_NAMES[ga],
                "group_b": gb, "group_b_name": GROUP_NAMES[gb],
            })

    change_components: List[Dict[str, Any]] = []
    unchanged_count = 0
    for root in sorted(groups):
        members = sorted(groups[root])
        a_keys = sorted(k[3:] for k in members if k.startswith("A::"))
        b_keys = sorted(k[3:] for k in members if k.startswith("B::"))

        a_member_cases = sorted({
            cid for k in a_keys for cid in a.components[k]["cases"]
        })
        b_member_cases = sorted({
            cid for k in b_keys for cid in b.components[k]["cases"]
        })
        a_member_patients = sorted({
            p for k in a_keys for p in a.components[k]["patients"]
        })
        b_member_patients = sorted({
            p for k in b_keys for p in b.components[k]["patients"]
        })
        a_edges = sorted(
            (e for k in a_keys for e in a.components[k]["relative_edges"]),
            key=lambda e: (e["patient_a"], e["patient_b"]),
        )
        b_edges = sorted(
            (e for k in b_keys for e in b.components[k]["relative_edges"]),
            key=lambda e: (e["patient_a"], e["patient_b"]),
        )
        # 去重（多分量合并视角下边不会重复，保险起见）
        a_edges = list({(e["patient_a"], e["patient_b"]): e for e in a_edges}.values())
        b_edges = list({(e["patient_a"], e["patient_b"]): e for e in b_edges}.values())

        set_a, set_b = set(a_member_cases), set(b_member_cases)
        comp_added = sorted(set_b - set_a)
        comp_removed = sorted(set_a - set_b)
        comp_shared = sorted(set_a & set_b)

        # 分组变化：共有病例在两版归属不同
        comp_group_changes = [
            gc for gc in group_changes if gc["case_id"] in set(comp_shared)
        ]
        group_changed = len(comp_group_changes) > 0

        # 连通范围变化：成员集合、患者构成、亲属边、分量合并/分裂任一发生
        connectivity_changed = (
            set_a != set_b
            or set(a_member_patients) != set(b_member_patients)
            or {(e["patient_a"], e["patient_b"]) for e in a_edges}
            != {(e["patient_a"], e["patient_b"]) for e in b_edges}
            or len(a_keys) != 1 or len(b_keys) != 1
            or not a_keys or not b_keys
        )

        if group_changed and connectivity_changed:
            change_type = "group_and_connectivity"
        elif group_changed:
            change_type = "group_only"
        elif connectivity_changed:
            change_type = "connectivity"
        else:
            change_type = "unchanged"

        if change_type == "unchanged":
            unchanged_count += 1
            continue

        total_sides = len(a_keys) + len(b_keys)

        def _side(keys_side: List[str], parsed: ParsedManifest) -> Optional[Dict[str, Any]]:
            if not keys_side:
                return None
            # 两侧都是 1 个分量的普通配对：返回带稳定 component_key 的分量视图
            if total_sides == 2:
                return _side_component_view(parsed, keys_side[0])
            # 分量合并/分裂：该侧在同一变化分量内包含多个分量，聚合为一个视角
            all_cases = sorted(cid for k in keys_side
                               for cid in parsed.components[k]["cases"])
            all_patients = sorted(p for k in keys_side
                                  for p in parsed.components[k]["patients"])
            all_edges = sorted(
                (e for k in keys_side for e in parsed.components[k]["relative_edges"]),
                key=lambda e: (e["patient_a"], e["patient_b"]),
            )
            all_edges = list({(e["patient_a"], e["patient_b"]): e for e in all_edges}.values())
            groups_present = sorted({parsed.components[k]["group"] for k in keys_side})
            return {
                "component_keys": keys_side,
                "merged_view": len(keys_side) > 1,
                "component_count": len(keys_side),
                "group": groups_present[0] if len(groups_present) == 1 else None,
                "group_name": (GROUP_NAMES[groups_present[0]]
                               if len(groups_present) == 1 else "多个分组（分量分裂）"),
                "cases": all_cases,
                "patients": all_patients,
                "relative_edges": all_edges,
                "case_count": len(all_cases),
                "patient_count": len(all_patients),
                "edge_count": len(all_edges),
            }

        change_components.append({
            "change_type": change_type,
            "change_type_name": _change_type_name(change_type),
            "group_changed": group_changed,
            "connectivity_changed": connectivity_changed,
            "shared_cases": comp_shared,
            "added_cases": [_case_view(cid, b, a) for cid in comp_added],
            "removed_cases": [_case_view(cid, a, b) for cid in comp_removed],
            "group_changes": comp_group_changes,
            "edges_added": [
                e for e in b_edges
                if (e["patient_a"], e["patient_b"])
                not in {(x["patient_a"], x["patient_b"]) for x in a_edges}
            ],
            "edges_removed": [
                e for e in a_edges
                if (e["patient_a"], e["patient_b"])
                not in {(x["patient_a"], x["patient_b"]) for x in b_edges}
            ],
            "side_a": _side(a_keys, a),
            "side_b": _side(b_keys, b),
        })

    # 排序：连通范围变化优先，再按 A 侧首个病例号，输出稳定
    type_order = {"connectivity": 0, "group_and_connectivity": 1, "group_only": 2}
    change_components.sort(key=lambda c: (
        type_order.get(c["change_type"], 9),
        (c["side_a"] or c["side_b"])["cases"][0],
    ))

    unchanged_case_assignments = sum(
        1 for cid in shared_cases
        if a.assignments[cid] == b.assignments[cid]
    )
    report = {
        "report_kind": "manifest_comparison",
        "manifest_a": _manifest_meta(a),
        "manifest_b": _manifest_meta(b),
        "summary": {
            "case_count_a": len(a_cases),
            "case_count_b": len(b_cases),
            "shared_case_count": len(shared_cases),
            "added_case_count": len(added_cases),
            "removed_case_count": len(removed_cases),
            "group_changed_case_count": len(group_changes),
            "unchanged_case_count": unchanged_case_assignments,
            "component_count_a": len(a.components),
            "component_count_b": len(b.components),
            "change_component_count": len(change_components),
            "unchanged_component_count": unchanged_count,
            "group_only_component_count": sum(
                1 for c in change_components if c["change_type"] == "group_only"),
            "connectivity_component_count": sum(
                1 for c in change_components
                if c["change_type"] in ("connectivity", "group_and_connectivity")),
        },
        "case_changes": {
            "added": [_case_view(cid, b, a) for cid in added_cases],
            "removed": [_case_view(cid, a, b) for cid in removed_cases],
            "group_changed": group_changes,
        },
        "change_components": change_components,
    }
    return report


def _case_view(cid: str, owner: ParsedManifest, other: ParsedManifest) -> Dict[str, Any]:
    """新增/移除病例的详情；归属信息取自其所在版本，另一版本无归属。"""
    case = next(c for c in owner.cases if c.case_id == cid)
    return {
        "case_id": cid,
        "patient_id": case.patient_id,
        "label": case.label,
        "group": owner.assignments.get(cid),
        "group_name": GROUP_NAMES[owner.assignments[cid]] if cid in owner.assignments else None,
        "component_key": owner.case_component.get(cid),
    }


def _manifest_meta(parsed: ParsedManifest) -> Dict[str, Any]:
    return {
        "manifest_id": parsed.manifest_id,
        "dataset_version": parsed.dataset_version,
        "expired": parsed.expired,
        "created_at": parsed.created_at,
        "case_count": len(parsed.cases),
        "component_count": len(parsed.components),
    }


def _change_type_name(change_type: str) -> str:
    return {
        "group_only": "单纯分组变化",
        "connectivity": "连通范围变化",
        "group_and_connectivity": "分组 + 连通范围变化",
        "unchanged": "无变化",
    }.get(change_type, change_type)
