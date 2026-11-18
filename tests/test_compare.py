"""清单对照测试：分量匹配、分组变化/连通范围变化分类、快照校验拒绝、只读保证。"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.compare import ReportValidationError, build_comparison_report  # noqa: E402
from app.splitter import Case, RelativePair, solve_split  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_compare.db"
    monkeypatch.setenv("SPLIT_DB_PATH", str(db_file))
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.db.close()


# 两个亲属连通分量：{P1,P2} 与 {P3,P4}，每分量两个病例，标签各一个 0/1
CASES_CSV = (
    "病例号,患者号,标签\n"
    "C1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
    "C5,P3,1\nC6,P3,0\nC7,P4,1\nC8,P4,0\n"
)
REL_CSV = "患者A,患者B\nP1,P2\nP3,P4\n"
QUOTAS = {"g1_label0": 2, "g1_label1": 2, "g2_label0": 2, "g2_label1": 2}


def _publish(client, cases_csv=CASES_CSV, rel_csv=REL_CSV, quotas=QUOTAS, locks=None):
    r = client.post("/api/upload/cases",
                    files={"file": ("cases.csv", cases_csv.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    if rel_csv:
        r = client.post("/api/upload/relatives",
                        files={"file": ("r.csv", rel_csv.encode("utf-8"), "text/csv")})
        assert r.status_code == 200, r.text
    r = client.put("/api/quotas", json=quotas)
    assert r.status_code == 200, r.text
    if locks:
        r = client.put("/api/locks", json={"locks": locks})
        assert r.status_code == 200, r.text
    token = client.post("/api/solve").json()["preview_token"]
    assert token
    pub = client.post("/api/publish", json={"preview_token": token})
    assert pub.status_code == 200, pub.text
    return pub.json()


def _compare(client, id_a, id_b):
    return client.post("/api/compare", json={"manifest_a": id_a, "manifest_b": id_b})


# --------------------------------------------------------------------------- #
# API 全链路
# --------------------------------------------------------------------------- #
def test_compare_group_only_change(client):
    m1 = _publish(client, locks=[{"case_id": "C1", "group": 1}])
    # 反向锁定：第一个连通分量整体换到另一组（成员不变），另一分量随之对调
    m2 = _publish(client, locks=[{"case_id": "C1", "group": 2}])

    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    report = r.json()

    assert report["manifest_a"]["manifest_id"] == m1["manifest_id"]
    assert report["manifest_b"]["manifest_id"] == m2["manifest_id"]
    summary = report["summary"]
    assert summary["case_count_a"] == summary["case_count_b"] == 8
    assert summary["added_case_count"] == 0 and summary["removed_case_count"] == 0
    # 配额要求两个分量各占一组，锁定反转后两个分量整体对调，8 个病例全部换组
    assert summary["group_changed_case_count"] == 8
    assert summary["group_only_component_count"] == 2
    assert summary["connectivity_component_count"] == 0
    assert summary["unchanged_component_count"] == 0

    comp = report["change_components"][0]
    assert comp["change_type"] == "group_only"
    assert comp["side_a"]["cases"] == comp["side_b"]["cases"]
    assert comp["side_a"]["group"] != comp["side_b"]["group"]
    assert comp["added_cases"] == [] and comp["removed_cases"] == []
    # 原始亲属边随分量输出
    assert comp["side_a"]["relative_edges"] == [{"patient_a": "P1", "patient_b": "P2"}]
    # 病例级变化与分量一致
    assert len(report["case_changes"]["group_changed"]) == 8


def test_compare_accepts_invalidated_manifest(client):
    """真正失效（invalidated=1）的清单依然可以选来对照。"""
    m1 = _publish(client, locks=[{"case_id": "C1", "group": 1}])
    # 一次不重新发布的输入变动，使 m1 被标记 invalidated
    r = client.put("/api/locks", json={"locks": [{"case_id": "C2", "group": 1}]})
    assert r.status_code == 200
    detail = client.get(f"/api/manifests/{m1['manifest_id']}").json()
    assert detail["expired"] is True and detail["invalidated"] is True

    m2 = _publish(client, locks=[{"case_id": "C1", "group": 2}])
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    assert r.json()["manifest_a"]["expired"] is True
    assert r.json()["summary"]["group_changed_case_count"] == 8


def test_compare_connectivity_change_added_cases(client):
    base_locks = [{"case_id": "C1", "group": 1}]
    m1 = _publish(client, locks=base_locks)
    # 新增独立患者的病例，配额相应调整；已有 8 个病例的归属用相同锁定保持不变
    new_cases = CASES_CSV + "C9,P5,0\n"
    m2 = _publish(client, cases_csv=new_cases,
                  quotas={"g1_label0": 3, "g1_label1": 2, "g2_label0": 2, "g2_label1": 2},
                  locks=base_locks)
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    report = r.json()
    summary = report["summary"]
    assert summary["added_case_count"] == 1
    assert summary["group_changed_case_count"] == 0
    assert summary["connectivity_component_count"] == 1
    assert summary["group_only_component_count"] == 0

    comp = report["change_components"][0]
    assert comp["change_type"] == "connectivity"
    assert comp["added_cases"][0]["case_id"] == "C9"
    assert comp["side_a"] is None  # 新版才出现的分量
    assert comp["side_b"]["cases"] == ["C9"]
    assert report["case_changes"]["added"][0]["patient_id"] == "P5"
    # 原有 8 个病例分组逐一对齐
    detail_a = client.get(f"/api/manifests/{m1['manifest_id']}").json()
    detail_b = client.get(f"/api/manifests/{m2['manifest_id']}").json()
    ga, gb = detail_a["result"]["assignments"], detail_b["result"]["assignments"]
    assert all(ga[cid] == gb[cid] for cid in ga)


def test_compare_removed_cases_and_edges(client):
    m1 = _publish(client)
    # 删除 C7/C8（患者 P4），亲属边 P3-P4 随患者消失；其余 6 病例保持原归属
    kept = (
        "病例号,患者号,标签\n"
        "C1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
        "C5,P3,1\nC6,P3,0\n"
    )
    new_rel = "患者A,患者B\nP1,P2\n"
    m2 = _publish(client, cases_csv=kept, rel_csv=new_rel,
                  quotas={"g1_label0": 2, "g1_label1": 2, "g2_label0": 1, "g2_label1": 1})
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["summary"]["removed_case_count"] == 2
    removed = {c["case_id"] for c in report["case_changes"]["removed"]}
    assert removed == {"C7", "C8"}
    # 包含移除病例的变化分量
    comp = next(c for c in report["change_components"]
                if {x["case_id"] for x in c["removed_cases"]} == {"C7", "C8"})
    assert comp["connectivity_changed"] is True
    assert comp["edges_removed"] == [{"patient_a": "P3", "patient_b": "P4"}]


def test_compare_edge_addition_without_group_flip(client):
    """只增加一条亲属边、分组完全不变 -> connectivity 且无分组变化。"""
    locks = [{"case_id": "C1", "group": 1}]
    m1 = _publish(client, rel_csv="患者A,患者B\nP1,P2\n", locks=locks)
    m2 = _publish(client, rel_csv="患者A,患者B\nP1,P2\nP3,P4\n", locks=locks)
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    comp = next(c for c in r.json()["change_components"]
                if c["change_type"] == "connectivity")
    assert comp["group_changed"] is False
    assert comp["edges_added"] == [{"patient_a": "P3", "patient_b": "P4"}]
    assert r.json()["summary"]["group_changed_case_count"] == 0


def test_compare_merge_components_group_and_connectivity(client):
    """亲属边把两个原本独立的分量连成一个，且锁定迫使整体换组。"""
    m1 = _publish(client,
                  rel_csv="患者A,患者B\nP1,P2\nP3,P4\n",
                  locks=[{"case_id": "C1", "group": 1}])
    # 合并后 8 病例成为同一连通分量，必须同组；配额改为整组容纳
    m2 = _publish(client,
                  rel_csv="患者A,患者B\nP1,P2\nP3,P4\nP2,P3\n",
                  quotas={"g1_label0": 4, "g1_label1": 4, "g2_label0": 0, "g2_label1": 0},
                  locks=[{"case_id": "C1", "group": 1}])
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    report = r.json()
    # B 侧两个分量经 P2-P3 合并为一个；跨版配对后 A 侧呈现两个分量、B 侧一个
    merged = [c for c in report["change_components"]
              if (c["side_b"] and c["side_b"].get("merged_view"))
              or (c["side_a"] and c["side_a"].get("merged_view"))
              or (c["side_a"] and c["side_b"]
                  and c["side_a"].get("component_count", 1) != c["side_b"].get("component_count", 1))]
    assert len(merged) == 1
    mc = merged[0]
    assert mc["change_type"] == "group_and_connectivity"
    # B 侧为单一合并分量，包含全部 8 个病例
    assert mc["side_b"]["cases"] == sorted(mc["side_b"]["cases"])
    assert len(mc["side_b"]["cases"]) == 8
    assert {"patient_a": "P2", "patient_b": "P3"} in mc["edges_added"]
    # A 侧两个分量被并到同一变化分量中
    assert mc["side_a"].get("component_count") == 2
    # 原 P3-P4 分量从评估集整体并入训练集
    assert {gc["case_id"] for gc in mc["group_changes"]} == {
        "C5", "C6", "C7", "C8"}


def test_compare_identical_assignments_reports_no_changes(client):
    # 同一输入发布两次（第二次需要新写操作：改配额再改回，版本不回退）
    locks = [{"case_id": "C1", "group": 1}]
    m1 = _publish(client, locks=locks)
    client.put("/api/quotas", json={
        "g1_label0": 3, "g1_label1": 2, "g2_label0": 1, "g2_label1": 2})
    client.put("/api/quotas", json=QUOTAS)
    m2 = _publish(client, locks=locks)
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["summary"]["change_component_count"] == 0
    assert report["summary"]["unchanged_component_count"] == 2
    assert report["change_components"] == []
    assert report["case_changes"]["group_changed"] == []


def test_compare_endpoint_validation_errors(client):
    m1 = _publish(client)
    # 同一个清单
    r = _compare(client, m1["manifest_id"], m1["manifest_id"])
    assert r.status_code == 400
    # 不存在的清单
    r = _compare(client, m1["manifest_id"], "nonexistent-id")
    assert r.status_code == 404
    # 空负载
    r = client.post("/api/compare", json={"manifest_a": "", "manifest_b": ""})
    assert r.status_code == 422


def test_compare_is_read_only(client):
    before = client.get("/api/state").json()
    m1 = _publish(client)
    m2 = _publish(client, locks=[{"case_id": "C1", "group": 2}])
    state_before_compare = client.get("/api/state").json()
    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 200
    state_after = client.get("/api/state").json()
    assert state_after["dataset_version"] == state_before_compare["dataset_version"]
    assert state_after["input"] == state_before_compare["input"]
    # 清单记录数量与失效标记不变
    assert len(state_after["manifests"]) == len(state_before_compare["manifests"])
    assert before is not None  # 占位：保留 before 变量语义


# --------------------------------------------------------------------------- #
# 快照损坏：缺字段 / 病例重复 / 结果与快照不一致 -> 拒绝生成报告
# --------------------------------------------------------------------------- #
def _good_manifest():
    # 两个独立分量（P1、P2 各两个病例），锁定 C1 到组 1 使切分确定
    cases = [Case("C1", "P1", 1), Case("C2", "P1", 0),
             Case("C3", "P2", 1), Case("C4", "P2", 0)]
    rels: list = []
    quotas = {(1, 0): 1, (1, 1): 1, (2, 0): 1, (2, 1): 1}
    result = solve_split(cases, rels, quotas, {"C1": 1})
    assert result["status"] == "sat"
    return {
        "manifest_id": "aaaaaaaa11111111",
        "dataset_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "expired": False,
        "input": {
            "cases": [{"case_id": c.case_id, "patient_id": c.patient_id, "label": c.label}
                      for c in cases],
            "relatives": [{"patient_a": r.patient_a, "patient_b": r.patient_b} for r in rels],
            "quotas": {"g1_label0": 1, "g1_label1": 1, "g2_label0": 1, "g2_label1": 1},
            "locks": {"C1": 1},
        },
        "result": result,
    }


def test_duplicate_case_id_rejected():
    m = _good_manifest()
    m["input"]["cases"].append(dict(m["input"]["cases"][0]))  # 重复病例号
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("重复" in e for e in ei.value.errors)


def test_missing_fields_rejected():
    m = _good_manifest()
    del m["input"]["quotas"]
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("quotas" in e for e in ei.value.errors)

    m2 = _good_manifest()
    del m2["result"]["assignments"]
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(_good_manifest(), m2)
    assert any("assignments" in e for e in ei.value.errors)


def test_assignment_inconsistent_with_kinship_rejected():
    m = _good_manifest()
    # 手工篡改：让同一连通分量内的两个病例分到不同组
    cid = m["input"]["cases"][1]["case_id"]
    grp = m["result"]["assignments"][cid]
    m["result"]["assignments"][cid] = 2 if grp == 1 else 1
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("同组" in e or "连通分量" in e for e in ei.value.errors)


def test_assignment_inconsistent_with_quota_rejected():
    # 6 个独立患者、每患者单病例：无同患者/亲属连通约束，互换两个同标签病例后
    # 只违反配额复核，便于精确验证“结果与快照配额不一致”这条拒绝路径
    cases = [Case(f"C{i}", f"P{i}", 1 if i % 2 else 0) for i in range(1, 7)]
    quotas = {(1, 0): 2, (1, 1): 1, (2, 0): 1, (2, 1): 2}
    result = solve_split(cases, [], quotas, {})
    assert result["status"] == "sat"
    m = {
        "manifest_id": "bbbbbbbb22222222",
        "dataset_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "expired": False,
        "input": {
            "cases": [{"case_id": c.case_id, "patient_id": c.patient_id, "label": c.label}
                      for c in cases],
            "relatives": [],
            "quotas": {"g1_label0": 2, "g1_label1": 1, "g2_label0": 1, "g2_label1": 2},
            "locks": {},
        },
        "result": result,
    }
    g = m["result"]["assignments"]
    # 翻转一个标签 1 病例的分组：四格计数与配额不再一致
    target = next(c.case_id for c in cases if c.label == 1)
    g[target] = 2 if g[target] == 1 else 1
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("计数" in e and "配额" in e for e in ei.value.errors)
    # 无亲属、每患者单病例，不会触发连通性或锁定错误
    assert not any("连通分量" in e or "锁定" in e for e in ei.value.errors)


def test_lock_violation_in_snapshot_rejected():
    m = _good_manifest()
    cid = m["input"]["cases"][0]["case_id"]
    grp = m["result"]["assignments"][cid]
    m["input"]["locks"] = {cid: 2 if grp == 1 else 1}  # 锁定与结果相反
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("锁定" in e for e in ei.value.errors)


def test_unknown_patient_in_relative_edge_rejected():
    m = _good_manifest()
    m["input"]["relatives"].append({"patient_a": "P1", "patient_b": "P999"})
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("P999" in e for e in ei.value.errors)


def test_self_loop_relative_edge_rejected():
    m = _good_manifest()
    m["input"]["relatives"].append({"patient_a": "P1", "patient_b": "P1"})
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("同一患者" in e for e in ei.value.errors)


def test_illegal_group_value_in_assignments_rejected():
    m = _good_manifest()
    m["result"]["assignments"]["C1"] = 3
    with pytest.raises(ReportValidationError) as ei:
        build_comparison_report(m, _good_manifest())
    assert any("分组" in e for e in ei.value.errors)


def test_api_rejects_corrupted_persisted_snapshot(client, tmp_path):
    """直接破坏数据库中的快照 JSON：读取/解析失败时不返回报告、不改动数据。"""
    m1 = _publish(client)
    m2 = _publish(client, locks=[{"case_id": "C1", "group": 2}])
    import sqlite3
    db_path = os.environ["SPLIT_DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE published_manifests SET input_json = ? WHERE manifest_id = ?",
        ("{不是合法JSON", m1["manifest_id"]),
    )
    conn.commit()
    conn.close()

    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 422
    assert "报告" in r.json()["detail"]
    # 输入版本与另一份清单未被修改
    state = client.get("/api/state").json()
    assert state["manifests"] and len(state["manifests"]) == 2
    intact = client.get(f"/api/manifests/{m2['manifest_id']}")
    assert intact.status_code == 200 and intact.json()["manifest_id"] == m2["manifest_id"]


def test_corrupted_rows_kept_untouched_after_failed_compare(client):
    """校验拒绝后，损坏行原样保留（服务不修复、不删除输入与清单）。"""
    import sqlite3
    m1 = _publish(client)
    m2 = _publish(client)
    db_path = os.environ["SPLIT_DB_PATH"]
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT input_json FROM published_manifests WHERE manifest_id = ?",
        (m1["manifest_id"],),
    ).fetchone()
    original = row[0]
    tampered = json.loads(original)
    del tampered["cases"]
    conn.execute(
        "UPDATE published_manifests SET input_json = ? WHERE manifest_id = ?",
        (json.dumps(tampered, ensure_ascii=False), m1["manifest_id"]),
    )
    conn.commit()
    conn.close()

    r = _compare(client, m1["manifest_id"], m2["manifest_id"])
    assert r.status_code == 422

    conn = sqlite3.connect(db_path)
    kept = conn.execute(
        "SELECT input_json FROM published_manifests WHERE manifest_id = ?",
        (m1["manifest_id"],),
    ).fetchone()[0]
    conn.close()
    assert json.loads(kept) == tampered  # 损坏数据原样保留，未被服务改写
