"""批量迁移表名与表名映射登记簿的领域测试。

覆盖：整组只生效一次、失败隔离、暂空/兼容名两种部分完成态、
历史旧名解析、字段血缘复核待办同步。
"""
from __future__ import annotations

import sys
sys.path.insert(0, '.')

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.name_mapping import (
    GROUP_COMPLETED,
    GROUP_PARTIAL,
    MAPPING_APPLIED,
    MAPPING_FAILED,
    MAPPING_PENDING,
    MAPPING_SKIPPED,
    TODO_PENDING,
    NameMappingLedger,
    ledger as global_ledger,
)
from app.table_registry import (
    BINDING_COMPAT,
    POLICY_COMPAT,
    POLICY_VACANT,
    RegistryError,
    TableNameRegistry,
    registry as global_registry,
)


@pytest.fixture()
def world():
    registry = TableNameRegistry()
    ledger = NameMappingLedger(registry)
    ledger.reset(clear_registry=True)
    return registry, ledger


@pytest.fixture(autouse=True)
def reset_global_store():
    """API 测试共享进程级登记簿：每个用例前恢复演示数据，避免用例间串扰。"""
    global_registry.reset(preload=True)
    global_ledger.reset(clear_registry=False)
    yield


def pair():
    return [
        {"source_name": "A", "target_name": "B"},
        {"source_name": "B", "target_name": "C"},
    ]


def identity_of(registry, name, **kwargs):
    return registry.resolve(name, **kwargs)["table_identity"]


class TestInitialRegistry:
    def test_two_demo_tables_each_own_name(self, world):
        registry, _ = world
        assert identity_of(registry, "A") == "lake://lakehouse/A"
        assert identity_of(registry, "B") == "lake://lakehouse/B"

    def test_amount_column_present(self, world):
        registry, _ = world
        resolved = registry.resolve("A")
        assert "amount" in resolved["columns"]
        assert resolved["field_transformations"][0]["source_column"] == "amount"


class TestPartialVacant:
    def test_only_b_to_c_leaves_b_vacant(self, world):
        registry, ledger = world
        view = ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        assert view["status"] == GROUP_PARTIAL
        statuses = {item["source_name"]: item["status"] for item in view["mappings"]}
        assert statuses["B"] == MAPPING_APPLIED
        assert statuses["A"] == MAPPING_PENDING
        # B→C 已用原 B 的身份，C 指向原 B。
        assert identity_of(registry, "C") == "lake://lakehouse/B"
        # A 尚未改名，仍以正式名 A 指向原 A。
        assert identity_of(registry, "A") == "lake://lakehouse/A"
        # B 暂空：没有绑定，也不猜来源。
        state = registry.resolve("B")
        assert state["resolved"] is False
        assert state["state"] == "vacant"
        assert state["table_identity"] is None
        # 至多一张表：任何时刻活跃绑定不超过一条。
        assert len([b for b in registry.name_bindings("B") if b["effective_to"] is None]) == 0

    def test_retry_completes_and_switches_b_to_original_a(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        view = ledger.submit_batch(pair(), policy=POLICY_VACANT)
        assert view["deduplicated"] is True
        assert view["status"] == GROUP_COMPLETED
        assert identity_of(registry, "B") == "lake://lakehouse/A"
        assert identity_of(registry, "C") == "lake://lakehouse/B"
        assert registry.resolve("A")["state"] == "vacant"

    def test_completed_rename_not_reapplied(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        before = list(registry.bindings)
        view = ledger.submit_batch(pair(), policy=POLICY_VACANT)
        assert view["status"] == GROUP_COMPLETED
        # 再次提交没有新增绑定，也没有改动原有效区间。
        assert len(registry.bindings) == len(before)
        assert identity_of(registry, "B") == "lake://lakehouse/A"
        assert identity_of(registry, "C") == "lake://lakehouse/B"


class TestPartialCompat:
    def test_compat_alias_keeps_original_b_read_only(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_COMPAT, apply_only=["B"])
        state_b = registry.resolve("B")
        assert state_b["resolved"] is True
        assert state_b["table_identity"] == "lake://lakehouse/B"
        assert state_b["binding_kind"] == BINDING_COMPAT
        assert state_b["read_only"] is True
        assert state_b["writable"] is False
        # 同一名称只有一个活跃绑定。
        assert len([b for b in registry.name_bindings("B") if b["effective_to"] is None]) == 1

    def test_retry_replaces_compat_with_original_a(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_COMPAT, apply_only=["B"])
        ledger.submit_batch(pair(), policy=POLICY_COMPAT)
        state_b = registry.resolve("B")
        assert state_b["table_identity"] == "lake://lakehouse/A"
        assert state_b["binding_kind"] == "official"
        assert state_b["writable"] is True
        assert identity_of(registry, "C") == "lake://lakehouse/B"
        assert len([b for b in registry.name_bindings("B") if b["effective_to"] is None]) == 1


class TestFailureIsolation:
    def test_failed_item_does_not_block_other_successes(self, world):
        registry, ledger = world
        # 第三项源表不存在，单独失败；A→B、B→C 仍是完整的一对改名。
        view = ledger.submit_batch(
            [
                {"source_name": "A", "target_name": "B"},
                {"source_name": "B", "target_name": "C"},
                {"source_name": "Z", "target_name": "Q"},
            ],
            policy=POLICY_VACANT,
        )
        statuses = {item["source_name"]: item["status"] for item in view["mappings"]}
        assert statuses["A"] == MAPPING_APPLIED
        assert statuses["B"] == MAPPING_APPLIED
        assert statuses["Z"] == MAPPING_FAILED
        assert view["status"] == GROUP_PARTIAL
        # 成功项按原身份保留：B 指向原 A，C 指向原 B。
        assert identity_of(registry, "B") == "lake://lakehouse/A"
        assert identity_of(registry, "C") == "lake://lakehouse/B"
        # 失败项没有制造新名称。
        assert registry.resolve("Q")["resolved"] is False

    def test_occupied_target_fails_and_keeps_source_identity(self, world):
        registry, ledger = world
        # 外部先占用名称 X。
        registry.register_table("X")
        view = ledger.submit_batch(
            [{"source_name": "B", "target_name": "X"}],
            policy=POLICY_VACANT,
        )
        mapping = view["mappings"][0]
        assert mapping["status"] == MAPPING_FAILED
        assert mapping["table_identity"] == "lake://lakehouse/B"
        assert "X" in mapping["message"]
        # 失败项保持原名：B 仍指向原 B，X 仍属于占用者。
        assert identity_of(registry, "B") == "lake://lakehouse/B"
        assert identity_of(registry, "X") == "lake://lakehouse/X"

    def test_missing_source_fails_without_side_effect(self, world):
        registry, ledger = world
        view = ledger.submit_batch(
            [{"source_name": "Z", "target_name": "Q"}],
            policy=POLICY_VACANT,
        )
        assert view["mappings"][0]["status"] == MAPPING_FAILED
        assert registry.resolve("Q")["resolved"] is False


class TestNameUniqueness:
    def test_no_table_ever_has_two_official_names(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_COMPAT, apply_only=["B"])
        ledger.submit_batch(pair(), policy=POLICY_COMPAT)
        for identity in ("lake://lakehouse/A", "lake://lakehouse/B"):
            officials = [
                binding for binding in registry.bindings
                if binding["table_identity"] == identity
                and binding["kind"] == "official"
                and binding["effective_to"] is None
            ]
            assert len(officials) == 1

    def test_no_name_ever_resolves_to_two_tables(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        for name in ("A", "B", "C"):
            active = [
                binding for binding in registry.name_bindings(name)
                if binding["effective_to"] is None
            ]
            assert len(active) <= 1, f"名称 {name} 同时绑定多张表：{active}"
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        for name in ("A", "B", "C"):
            active = [
                binding for binding in registry.name_bindings(name)
                if binding["effective_to"] is None
            ]
            assert len(active) <= 1, f"名称 {name} 同时绑定多张表：{active}"

    def test_out_of_order_groups_converge(self, world):
        registry, ledger = world
        # 分两个批次登记，按 B→C、A→B 的逆序应用，最终状态与正序一致。
        first = ledger.submit_batch([{"source_name": "A", "target_name": "B"}], apply_only=[])
        second = ledger.submit_batch([{"source_name": "B", "target_name": "C"}], apply_only=[])
        ledger.apply_group(second["id"])
        ledger.apply_group(first["id"])
        assert identity_of(registry, "B") == "lake://lakehouse/A"
        assert identity_of(registry, "C") == "lake://lakehouse/B"
        assert registry.resolve("A")["state"] == "vacant"

    def test_taking_b_before_b_moves_out_is_rejected(self, world):
        """B→C 未完成时，A→B 不能抢占仍在使用的正式名 B。"""
        registry, ledger = world
        view = ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["A"])
        statuses = {item["source_name"]: item["status"] for item in view["mappings"]}
        assert statuses["A"] == MAPPING_FAILED
        assert statuses["B"] == MAPPING_PENDING
        # 失败不改动任何身份：A 仍是 A，B 仍指向原 B。
        assert identity_of(registry, "A") == "lake://lakehouse/A"
        assert identity_of(registry, "B") == "lake://lakehouse/B"
        # 先完成 B→C，再补 A→B 即可。
        ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        done = ledger.submit_batch(pair(), policy=POLICY_VACANT)
        assert done["status"] == GROUP_COMPLETED
        assert identity_of(registry, "B") == "lake://lakehouse/A"

    def test_duplicate_target_rejected(self, world):
        _, ledger = world
        with pytest.raises(RegistryError):
            ledger.submit_batch(
                [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "B"},
                ]
            )

    def test_target_names_must_be_unique_in_batch(self, world):
        _, ledger = world
        with pytest.raises(RegistryError):
            ledger.submit_batch(
                [{"source_name": "A", "target_name": "Z"}, {"source_name": "B", "target_name": "Z"}]
            )

    def test_bind_official_rejects_occupied_name(self, world):
        registry, _ = world
        with pytest.raises(RegistryError):
            registry.bind_official("B", "lake://lakehouse/A")


class TestHistoricalResolution:
    def test_old_name_resolves_to_original_source(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        before = "2026-06-01T00:00:00+00:00"
        assert identity_of(registry, "A", as_of=before) == "lake://lakehouse/A"
        assert identity_of(registry, "B", as_of=before) == "lake://lakehouse/B"
        # 改名完成后，历史时点的 B 仍指向原 B，而不是原 A。
        assert identity_of(registry, "C", as_of=before, ) is None

    def test_history_chain_is_traceable(self, world):
        registry, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        history = registry.name_bindings("B")
        assert [item["table_identity"] for item in history] == [
            "lake://lakehouse/B",
            "lake://lakehouse/A",
        ]
        assert history[0]["effective_to"] is not None
        assert history[1]["effective_to"] is None


class TestLineageEvidence:
    def test_evidence_locates_original_table_and_fields(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        evidence = ledger.lineage_evidence("C")
        assert evidence["resolved"] is True
        assert evidence["table_identity"] == "lake://lakehouse/B"
        assert evidence["initial_name"] == "B"
        assert evidence["locating_mapping_id"] is not None
        assert evidence["field_lineage"][0]["source_column"] == "amount"
        assert any(
            item["source_name"] == "B" and item["target_name"] == "C"
            for item in evidence["mappings"]
        )

    def test_historical_evidence_uses_as_of(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        evidence = ledger.lineage_evidence("B", as_of="2026-06-01T00:00:00+00:00")
        assert evidence["table_identity"] == "lake://lakehouse/B"
        assert evidence["binding_kind"] == "official"
        assert evidence["field_lineage"]

    def test_vacant_name_reports_no_identity(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        evidence = ledger.lineage_evidence("B")
        assert evidence["resolved"] is False
        assert evidence["state"] == "vacant"
        assert evidence["table_identity"] is None


class TestReviewTodos:
    def test_disposition_syncs_to_todos(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT, apply_only=["B"])
        todos = {item["source_name"]: item for item in ledger.list_todos()}
        assert todos["B"]["disposition"] == MAPPING_APPLIED
        assert todos["A"]["disposition"] == "待应用"
        assert todos["A"]["review_status"] == TODO_PENDING
        assert "暂空" in todos["A"]["conclusion"]
        assert todos["B"]["evidence"]["table_identity"] == "lake://lakehouse/B"

    def test_todo_updates_after_retry(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_COMPAT, apply_only=["B"])
        assert len(ledger.list_todos()) == 2
        ledger.submit_batch(pair(), policy=POLICY_COMPAT)
        # 待办按映射去重更新，不重复生成。
        assert len(ledger.list_todos()) == 2
        todo_a = next(item for item in ledger.list_todos() if item["source_name"] == "A")
        assert todo_a["disposition"] == MAPPING_APPLIED
        assert todo_a["evidence"]["table_identity"] == "lake://lakehouse/A"

    def test_review_marks_done(self, world):
        _, ledger = world
        ledger.submit_batch(pair(), policy=POLICY_VACANT)
        todo = ledger.list_todos()[0]
        updated = ledger.review_todo(todo["id"], reviewer="data-eng", note="已核对")
        assert updated["review_status"] == "已复核"
        assert updated["reviewer"] == "data-eng"


class TestApi:
    def setup_method(self):
        self.client = TestClient(app)

    def test_rename_flow_over_http(self):
        resp = self.client.post(
            "/api/table-catalog/renames",
            json={
                "items": [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "C"},
                ],
                "policy": "vacant",
                "apply_only": ["B"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "部分完成"

        resolved = self.client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert resolved["ok"] is True
        assert resolved["state"] == "vacant"

        group_id = body["id"]
        retry = self.client.post(f"/api/table-catalog/renames/groups/{group_id}/apply")
        assert retry.status_code == 200
        assert retry.json()["status"] == "已完成"

        resolved_b = self.client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert resolved_b["table_identity"] == "lake://lakehouse/A"

        todos = self.client.get("/api/table-catalog/lineage-review-todos").json()
        assert todos["ok"] is True
        assert {item["source_name"] for item in todos["items"]} == {"A", "B"}

    def test_resolve_historical_over_http(self):
        self.client.post(
            "/api/table-catalog/renames",
            json={
                "items": [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "C"},
                ],
                "policy": "vacant",
            },
        )
        resp = self.client.get(
            "/api/lake-lineage/resolve",
            params={"name": "B", "as_of": "2026-06-01T00:00:00+00:00"},
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["table_identity"] == "lake://lakehouse/B"

    def test_empty_batch_rejected(self):
        resp = self.client.post("/api/table-catalog/renames", json={"items": []})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_register_table_keeps_name_unique(self):
        first = self.client.post("/api/table-catalog/tables", json={"name": "REG"}).json()
        assert first["ok"] is True
        assert first["table"]["official_name"] == "REG"
        second = self.client.post("/api/table-catalog/tables", json={"name": "REG"}).json()
        assert second["ok"] is False
        assert "同一可读名只能对应一张表" in second["message"]

    def test_lineage_trace_uses_mapping_records(self):
        self.client.post(
            "/api/table-catalog/renames",
            json={
                "items": [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "C"},
                ],
                "policy": "vacant",
            },
        )
        # 现行名称 C 解析到原表 B，并带出 amount 字段转换。
        resolved = self.client.get("/api/lake-lineage/resolve", params={"name": "C"}).json()
        assert resolved["table_identity"] == "lake://lakehouse/B"
        assert resolved["field_lineage"][0]["source_column"] == "amount"
        assert resolved["locating_mapping_id"] is not None


class TestDemoScenario:
    """题面场景：A→B、B→C，先只让 B→C 成功，再重试补齐 A→B。"""

    def _setup(self, client, policy):
        return client.post(
            "/api/table-catalog/renames",
            json={
                "items": [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "C"},
                ],
                "policy": policy,
                "apply_only": ["B"],
            },
        ).json()

    def test_vacant_then_complete(self):
        client = TestClient(app)
        first = self._setup(client, "vacant")
        assert first["status"] == "部分完成"

        current_b = client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert current_b["state"] == "vacant"
        current_c = client.get("/api/lake-lineage/resolve", params={"name": "C"}).json()
        assert current_c["table_identity"] == "lake://lakehouse/B"
        historical_b = client.get(
            "/api/lake-lineage/resolve",
            params={"name": "B", "as_of": "2026-06-01T00:00:00+00:00"},
        ).json()
        assert historical_b["table_identity"] == "lake://lakehouse/B"

        second = client.post(
            f"/api/table-catalog/renames/groups/{first['id']}/apply",
        ).json()
        assert second["status"] == "已完成"

        final_b = client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert final_b["table_identity"] == "lake://lakehouse/A"
        assert final_b["binding_kind"] == "official"
        final_c = client.get("/api/lake-lineage/resolve", params={"name": "C"}).json()
        assert final_c["table_identity"] == "lake://lakehouse/B"
        # 历史 B 始终指向原 B，不受后续改名影响。
        historical_b = client.get(
            "/api/lake-lineage/resolve",
            params={"name": "B", "as_of": "2026-06-01T00:00:00+00:00"},
        ).json()
        assert historical_b["table_identity"] == "lake://lakehouse/B"

    def test_compat_then_complete(self):
        client = TestClient(app)
        first = self._setup(client, "compat")
        assert first["status"] == "部分完成"

        current_b = client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert current_b["table_identity"] == "lake://lakehouse/B"
        assert current_b["binding_kind"] == "compat"
        assert current_b["read_only"] is True

        client.post(f"/api/table-catalog/renames/groups/{first['id']}/apply")
        final_b = client.get("/api/lake-lineage/resolve", params={"name": "B"}).json()
        assert final_b["table_identity"] == "lake://lakehouse/A"
        assert final_b["binding_kind"] == "official"
        assert final_b["read_only"] is False

    def test_group_never_double_applies(self):
        client = TestClient(app)
        first = self._setup(client, "vacant")
        client.post(f"/api/table-catalog/renames/groups/{first['id']}/apply")
        overview_before = client.get("/api/table-catalog/renames").json()["counts"]
        # 再次提交同批次：幂等，不新增映射、不重复应用。
        again = client.post(
            "/api/table-catalog/renames",
            json={
                "items": [
                    {"source_name": "A", "target_name": "B"},
                    {"source_name": "B", "target_name": "C"},
                ],
                "policy": "vacant",
            },
        ).json()
        assert again["deduplicated"] is True
        overview_after = client.get("/api/table-catalog/renames").json()["counts"]
        assert overview_after["mappings"] == overview_before["mappings"]
        assert overview_after["applied"] == overview_before["applied"]
