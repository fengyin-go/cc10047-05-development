"""湖表血缘领域服务：链路采集 + 按可追溯表名映射定位原表与字段转换。"""
from __future__ import annotations

from typing import Any

from app.name_mapping import ledger
from app.services.base import BaseModuleService
from app.store import store
from app.table_registry import DEFAULT_CATALOG, RegistryError


class LakeLineageService(BaseModuleService):
    MODULE = "lake_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["链路标识", "上游表", "下游表", "转换算子", "字段映射", "采集时间", "链路版本", "数据量级"]
    REQUIRED_FIELDS = ["链路标识", "上游表", "下游表"]
    STATUS_ORDER = ["已建立", "待验证", "已断裂", "已废弃"]
    ACTION_RULES = {"刷新血缘": "已建立", "标记断裂": "已断裂"}
    NEGATIVE_ACTIONS = []

    def domain_trace_lineage(self, entry_id: int) -> dict[str, Any]:
        """追踪血缘：上游/下游表名先按表名映射记录解析到原表身份，再输出字段转换。"""
        entry = store.find(self.MODULE, entry_id)
        if not entry:
            return {"ok": False, "message": f"血缘链路 {entry_id} 不存在"}

        upstream_name = str(entry.get("上游表") or "")
        downstream_name = str(entry.get("下游表") or "")
        upstream_evidence = ledger.lineage_evidence(upstream_name)
        downstream_evidence = ledger.lineage_evidence(downstream_name)

        def table_view(name: str, evidence: dict[str, Any]) -> dict[str, Any]:
            if not evidence["resolved"]:
                return {
                    "table": name,
                    "table_identity": None,
                    "resolved": False,
                    "state": evidence["state"],
                    "columns": ["id", "name", "amount"],
                }
            return {
                "table": name,
                "table_identity": evidence["table_identity"],
                "initial_name": evidence["initial_name"],
                "binding_kind": evidence["binding_kind"],
                "read_only": evidence["read_only"],
                "resolved": True,
                "state": evidence["state"],
                "columns": evidence["columns"] or ["id", "name", "amount"],
            }

        upstream = [table_view(upstream_name, upstream_evidence)]
        downstream = [table_view(downstream_name, downstream_evidence)]
        field_lineage = downstream_evidence.get("field_lineage") or []

        return {
            "ok": True,
            "lineage_id": entry.get("链路标识"),
            "upstream_tables": len(upstream),
            "downstream_tables": len(downstream),
            "upstream": upstream,
            "downstream": downstream,
            "transformation": entry.get("转换算子", "SELECT"),
            "field_mapping": entry.get("字段映射", "1:1"),
            "field_lineage": field_lineage,
            "locating_mapping_id": downstream_evidence.get("locating_mapping_id"),
            "depth": 3,
        }

    # ------------------------------------------------------------------
    # 表名解析证据
    # ------------------------------------------------------------------

    def resolve_table(
        self,
        name: str,
        *,
        as_of: str | None = None,
        catalog: str = DEFAULT_CATALOG,
    ) -> dict[str, Any]:
        """解析一个可读表名（含历史旧名），给出原表身份和字段血缘证据。"""
        if not str(name or "").strip():
            return {"ok": False, "message": "表名称不能为空"}
        try:
            evidence = ledger.lineage_evidence(name, as_of=as_of, catalog=catalog)
        except RegistryError as exc:
            return {"ok": False, "message": str(exc)}
        message = self._resolution_message(evidence)
        return {"ok": True, "message": message, **evidence}

    def batch_resolve(
        self,
        names: list[str],
        *,
        as_of: str | None = None,
        catalog: str = DEFAULT_CATALOG,
    ) -> dict[str, Any]:
        cleaned = [str(item).strip() for item in names if str(item or "").strip()]
        if not cleaned:
            return {"ok": False, "message": "至少需要一个待解析的表名"}
        try:
            items = [
                ledger.lineage_evidence(name, as_of=as_of, catalog=catalog)
                for name in cleaned
            ]
        except RegistryError as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": True,
            "message": f"已解析 {len(items)} 个表名",
            "as_of": items[0]["as_of"],
            "items": items,
        }

    def name_history(self, name: str, *, catalog: str = DEFAULT_CATALOG) -> dict[str, Any]:
        if not str(name or "").strip():
            return {"ok": False, "message": "表名称不能为空"}
        evidence = ledger.lineage_evidence(name, catalog=catalog)
        return {
            "ok": True,
            "message": f"名称「{name}」共有 {len(evidence['name_history'])} 段绑定历史",
            "name": evidence["name"],
            "catalog": evidence["catalog"],
            "name_history": evidence["name_history"],
            "mappings": evidence["mappings"],
        }

    @staticmethod
    def _resolution_message(evidence: dict[str, Any]) -> str:
        if not evidence["resolved"]:
            if evidence["state"] == "vacant":
                return f"名称「{evidence['name']}」当前暂空，历史上有绑定但此刻没有可解析的表"
            return f"名称「{evidence['name']}」没有任何表名映射记录"
        kind = "临时只读兼容名" if evidence["binding_kind"] == "compat" else "正式名"
        return (
            f"名称「{evidence['name']}」解析到表 {evidence['table_identity']}，"
            f"以{kind}绑定，原表名 {evidence['initial_name']}"
        )
