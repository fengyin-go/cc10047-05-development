"""批量迁移表名（批量改名）：可追溯的表名映射记录、处置结论和字段血缘复核待办。

核心约束（与 ``table_registry`` 配合实现）：

* 表的身份随原表保留：改名只摘除源名、绑定目标名，``table_identity`` 不变；
* 整组只生效一次：同一组或同一组改名重复提交时，已完成的改名不会重复应用；
* 某一项失败时，其他已成功的名称仍按原身份保留；
* B→C 完成而 A→B 未完成时，名称 B 可以暂空，也可以保留原 B 的临时只读兼容名，
  两种状态都至多解析到一张表；
* 每条映射的处置结论同步写成“字段血缘复核待办”。

真实项目里这一层会落到元数据存储 + 任务表；当前实现仍为纯内存，可整体重置。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.table_registry import (
    BINDING_COMPAT,
    BINDING_OFFICIAL,
    DEFAULT_CATALOG,
    POLICIES,
    POLICY_COMPAT,
    POLICY_VACANT,
    RegistryError,
    TableNameRegistry,
    now_iso,
    registry,
)

MAPPING_APPLIED = "已应用"
MAPPING_PENDING = "待应用"
MAPPING_FAILED = "失败"
MAPPING_SKIPPED = "已生效跳过"

GROUP_COMPLETED = "已完成"
GROUP_PARTIAL = "部分完成"
GROUP_FAILED = "失败"
GROUP_PENDING = "待执行"

TODO_PENDING = "待复核"
TODO_DONE = "已复核"


@dataclass
class NameMapping:
    id: int
    group_id: int
    seq: int
    catalog: str
    source_name: str
    target_name: str
    table_identity: str | None
    status: str = MAPPING_PENDING
    message: str = ""
    applied_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def is_done(self) -> bool:
        return self.status in {MAPPING_APPLIED, MAPPING_SKIPPED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mapping_id": self.id,
            "group_id": self.group_id,
            "seq": self.seq,
            "catalog": self.catalog,
            "源表名": self.source_name,
            "source_name": self.source_name,
            "目标表名": self.target_name,
            "target_name": self.target_name,
            "table_identity": self.table_identity,
            "status": self.status,
            "message": self.message,
            "applied_at": self.applied_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class RenameGroup:
    id: int
    catalog: str
    policy: str
    remark: str
    signature: tuple[tuple[str, str], ...]
    status: str = GROUP_PENDING
    applied_once: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.id,
            "catalog": self.catalog,
            "policy": self.policy,
            "remark": self.remark,
            "status": self.status,
            "applied_once": self.applied_once,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [
                {"source_name": source, "target_name": target}
                for source, target in self.signature
            ],
        }


class NameMappingLedger:
    def __init__(self, name_registry: TableNameRegistry | None = None) -> None:
        self._lock = threading.RLock()
        self.registry = name_registry or registry
        self.groups: dict[int, RenameGroup] = {}
        self.mappings: list[NameMapping] = []
        self.todos: list[dict[str, Any]] = []
        self._completed: list[dict[str, Any]] = []
        self._group_seq = 0
        self._mapping_seq = 0
        self._todo_seq = 0
        self.reset(clear_registry=False)

    # -- 生命周期 ---------------------------------------------------------
    def reset(self, clear_registry: bool = True) -> None:
        with self._lock:
            self.groups = {}
            self.mappings = []
            self.todos = []
            self._completed = []
            self._group_seq = 0
            self._mapping_seq = 0
            self._todo_seq = 0
            if clear_registry:
                self.registry.reset(preload=True)

    # -- 查询 -------------------------------------------------------------
    def group(self, group_id: int) -> RenameGroup | None:
        return self.groups.get(group_id)

    def group_mappings(self, group_id: int) -> list[NameMapping]:
        return sorted(
            (item for item in self.mappings if item.group_id == group_id),
            key=lambda item: item.seq,
        )

    def group_view(self, group_id: int) -> dict[str, Any] | None:
        group = self.groups.get(group_id)
        if group is None:
            return None
        mappings = self.group_mappings(group_id)
        todos = [dict(todo) for todo in self.todos if todo["group_id"] == group_id]
        view = group.to_dict()
        view.update({
            "item_count": len(mappings),
            "applied_count": sum(1 for item in mappings if item.status == MAPPING_APPLIED),
            "skipped_count": sum(1 for item in mappings if item.status == MAPPING_SKIPPED),
            "failed_count": sum(1 for item in mappings if item.status == MAPPING_FAILED),
            "pending_count": sum(1 for item in mappings if item.status == MAPPING_PENDING),
            "mappings": [item.to_dict() for item in mappings],
            "review_todos": todos,
        })
        return view

    def list_groups(self) -> list[dict[str, Any]]:
        return [
            self.group_view(group.id) or group.to_dict()
            for group in sorted(self.groups.values(), key=lambda item: item.id, reverse=True)
        ]

    def list_mappings(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in sorted(self.mappings, key=lambda item: item.id)]

    def list_todos(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(todo) for todo in sorted(self.todos, key=lambda item: item["id"])]
        if status:
            rows = [item for item in rows if item["review_status"] == status]
        return rows

    # -- 提交批次 ---------------------------------------------------------
    def submit_batch(
        self,
        items: Iterable[dict[str, Any]],
        *,
        policy: str = POLICY_VACANT,
        apply_only: list[str] | None = None,
        catalog: str = DEFAULT_CATALOG,
        remark: str = "",
    ) -> dict[str, Any]:
        if policy not in POLICIES:
            raise RegistryError(f"未知名称处置策略「{policy}」，可选：vacant / compat")
        catalog = (catalog or DEFAULT_CATALOG).strip() or DEFAULT_CATALOG
        items_norm = self._normalize_items(items, catalog)
        # 批次指纹与提交顺序无关：同样一组改名重复提交时归档到同一批次。
        signature = tuple(sorted(items_norm))
        with self._lock:
            existing = self._find_group(catalog, signature)
            if existing is not None:
                # 同一批次重复提交：只补做未完成的项，已完成的改名不重复应用。
                at = self.registry.now()
                self._apply_pending(existing, apply_only, at=at)
                self._refresh_group(existing, at=at)
                self._sync_review_todos(existing)
                view = self.group_view(existing.id) or existing.to_dict()
                view["deduplicated"] = True
                view["message"] = (
                    "该批次已登记，本次仅补做未完成项；已应用的改名不会重复执行"
                )
                return view
            at = self.registry.now()
            self._group_seq += 1
            group = RenameGroup(
                id=self._group_seq,
                catalog=catalog,
                policy=policy,
                remark=remark.strip(),
                signature=signature,
                created_at=at,
                updated_at=at,
            )
            self.groups[group.id] = group
            for seq, (source, target) in enumerate(items_norm, start=1):
                self._mapping_seq += 1
                identity, skip_message = self._resolve_source(source, target, catalog, at)
                mapping = NameMapping(
                    id=self._mapping_seq,
                    group_id=group.id,
                    seq=seq,
                    catalog=catalog,
                    source_name=source,
                    target_name=target,
                    table_identity=identity,
                    status=MAPPING_PENDING if identity else MAPPING_FAILED,
                    message=skip_message,
                    created_at=at,
                    updated_at=at,
                )
                self.mappings.append(mapping)
            self._apply_pending(group, apply_only, at=at)
            self._refresh_group(group, at=at)
            self._sync_review_todos(group)
            return self.group_view(group.id) or group.to_dict()

    def apply_group(
        self,
        group_id: int,
        *,
        apply_only: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            group = self.groups.get(group_id)
            if group is None:
                raise RegistryError(f"改名批次 {group_id} 不存在")
            at = self.registry.now()
            self._apply_pending(group, apply_only, at=at)
            self._refresh_group(group, at=at)
            self._sync_review_todos(group)
            view = self.group_view(group.id) or group.to_dict()
            view["deduplicated"] = True
            view["message"] = "已补做未完成项；已应用的改名不会重复执行"
            return view

    def _normalize_items(
        self, items: Iterable[dict[str, Any]], catalog: str = DEFAULT_CATALOG,
    ) -> list[tuple[str, str]]:
        normalized: list[tuple[str, str]] = []
        sources: set[str] = set()
        targets: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                raise RegistryError("改名项必须是对象，包含 source_name 和 target_name")
            source = str(raw.get("source_name") or raw.get("source") or "").strip()
            target = str(raw.get("target_name") or raw.get("target") or "").strip()
            if not source or not target:
                raise RegistryError("改名项的源表名和目标表名都不能为空")
            if source == target:
                raise RegistryError(f"源表名和目标表名相同：{source}")
            if source in sources:
                raise RegistryError(f"同一批次里源表名重复：{source}")
            if target in targets:
                raise RegistryError(f"同一批次里目标表名重复：{target}")
            sources.add(source)
            targets.add(target)
            normalized.append((source, target))
        if not normalized:
            raise RegistryError("批量迁移表名至少需要一个改名项")
        # 同一张表在同一批次里只能出现一次（按提交时的源表绑定判定）。
        identities: set[str] = set()
        at = self.registry.now()
        for source, _ in normalized:
            identity = self._official_identity(source, catalog, at)
            if identity is None:
                continue
            if identity in identities:
                raise RegistryError(f"同一张表在同一批次里出现多次：{source}")
            identities.add(identity)
        return normalized

    def _find_group(
        self, catalog: str, signature: tuple[tuple[str, str], ...],
    ) -> RenameGroup | None:
        for group in self.groups.values():
            if group.catalog == catalog and group.signature == signature:
                return group
        return None

    def _official_identity(self, name: str, catalog: str, at: str) -> str | None:
        binding = self.registry.active_binding(name, catalog, at=at)
        if binding is None or binding["kind"] != BINDING_OFFICIAL:
            return None
        return binding["table_identity"]

    def _completed_identity(self, source: str, target: str, catalog: str) -> str | None:
        for item in self._completed:
            if (
                item["catalog"] == catalog
                and item["source_name"] == source
                and item["target_name"] == target
            ):
                return item["table_identity"]
        return None

    def _resolve_source(
        self, source: str, target: str, catalog: str, at: str,
    ) -> tuple[str | None, str]:
        """确定改名项的源表身份；已生效过的同名改名只做跳过标记，不重复应用。"""
        identity = self._official_identity(source, catalog, at)
        if identity is not None:
            return identity, ""
        completed = self._completed_identity(source, target, catalog)
        if completed is not None:
            return completed, "该改名此前已生效，本次只登记不重复应用"
        return None, f"源表名「{source}」当前没有正式绑定"

    # -- 应用 -------------------------------------------------------------
    def _apply_pending(
        self,
        group: RenameGroup,
        apply_only: list[str] | None,
        *,
        at: str,
    ) -> None:
        mappings = self.group_mappings(group.id)
        if apply_only is not None:
            selected = {str(name).strip() for name in apply_only}
            unknown = selected - {item.source_name for item in mappings}
            if unknown:
                raise RegistryError(f"批次里没有这些源表名：{'、'.join(sorted(unknown))}")
        else:
            selected = None

        candidates: list[NameMapping] = []
        for mapping in mappings:
            if mapping.is_done():
                # 整组只生效一次：已完成的改名不得重复应用。
                continue
            if selected is not None and mapping.source_name not in selected:
                continue
            if mapping.table_identity is None:
                mapping.status = MAPPING_FAILED
                mapping.updated_at = at
                continue
            if self._completed_identity(mapping.source_name, mapping.target_name, mapping.catalog):
                self._mark_skipped(mapping, at)
                continue
            candidates.append(mapping)

        planned_identities = {
            item.table_identity for item in candidates if item.table_identity is not None
        }
        # 预检查目标名冲突：目标若被本批次之外的正式名占用，直接判失败且不动源名。
        proceed: list[NameMapping] = []
        for mapping in candidates:
            assert mapping.table_identity is not None
            occupied = self.registry.active_binding(mapping.target_name, mapping.catalog, at=at)
            if (
                occupied is not None
                and occupied["kind"] == BINDING_OFFICIAL
                and occupied["table_identity"] != mapping.table_identity
                and occupied["table_identity"] not in planned_identities
            ):
                mapping.status = MAPPING_FAILED
                mapping.message = (
                    f"目标名「{mapping.target_name}」已由表 "
                    f"{occupied['table_identity']} 正式占用"
                )
                mapping.updated_at = at
                continue
            proceed.append(mapping)

        # 第一步：摘除待改名表的源名（同一批次里可能互相顶替名称）。
        retired: dict[int, int] = {}
        for mapping in proceed:
            assert mapping.table_identity is not None
            binding = self.registry.active_binding(mapping.source_name, mapping.catalog, at=at)
            if (
                binding is not None
                and binding["kind"] == BINDING_OFFICIAL
                and binding["table_identity"] == mapping.table_identity
            ):
                retired_binding = self.registry.retire(
                    binding["id"], at=at, reason=f"批次#{group.id} 迁移表名",
                )
                if retired_binding is not None:
                    retired[mapping.id] = retired_binding["id"]

        # 第二步：把目标名正式绑定到原表身份。
        for mapping in proceed:
            assert mapping.table_identity is not None
            try:
                binding, _replaced = self.registry.bind_official(
                    mapping.target_name,
                    mapping.table_identity,
                    catalog=mapping.catalog,
                    at=at,
                    group_id=group.id,
                    mapping_id=mapping.id,
                    reason=f"批次#{group.id} 迁移表名",
                    replace_compat=True,
                )
            except RegistryError as exc:
                mapping.status = MAPPING_FAILED
                mapping.message = str(exc)
                mapping.updated_at = at
                # 失败项保持原身份和原名；若原名已被同批次其他项接手，则至少保住身份。
                binding_id = retired.get(mapping.id)
                if binding_id is not None:
                    try:
                        self.registry.restore_binding(binding_id, at=at)
                    except RegistryError:
                        mapping.message = (
                            f"{mapping.message}；原名「{mapping.source_name}」已被同批次其他改名接手，"
                            f"表身份 {mapping.table_identity} 仍保留"
                        )
                continue
            mapping.status = MAPPING_APPLIED
            mapping.message = "已应用"
            mapping.applied_at = binding["effective_from"]
            mapping.updated_at = at
            group.applied_once = True
            self._remember_completed(group, mapping)

        self._ensure_partial_compat_names(group, at=at)
        group.updated_at = at

    def _mark_skipped(self, mapping: NameMapping, at: str) -> None:
        mapping.status = MAPPING_SKIPPED
        mapping.message = mapping.message or "该改名此前已生效，本次只登记不重复应用"
        mapping.applied_at = mapping.applied_at or at
        mapping.updated_at = at

    def _remember_completed(self, group: RenameGroup, mapping: NameMapping) -> None:
        for item in self._completed:
            if (
                item["catalog"] == mapping.catalog
                and item["source_name"] == mapping.source_name
                and item["target_name"] == mapping.target_name
            ):
                return
        self._completed.append({
            "catalog": mapping.catalog,
            "source_name": mapping.source_name,
            "target_name": mapping.target_name,
            "table_identity": mapping.table_identity,
            "group_id": group.id,
            "mapping_id": mapping.id,
            "applied_at": mapping.applied_at,
        })

    def _ensure_partial_compat_names(self, group: RenameGroup, *, at: str) -> None:
        """部分完成时的名称空洞：按策略暂空，或给原表留一个临时只读兼容名。

        只有“被本批次已应用改名腾出、且仍被本批次未完成改名等着接手”的名称，
        才处于部分完成态；真正腾空的旧名保持暂空，不额外制造兼容名。
        """
        if group.policy != POLICY_COMPAT:
            return
        mappings = self.group_mappings(group.id)
        applied_sources = {
            item.source_name: item for item in mappings if item.status == MAPPING_APPLIED
        }
        pending_targets = {
            item.target_name for item in mappings if item.status == MAPPING_PENDING
        }
        for name in pending_targets & set(applied_sources):
            vacated = applied_sources[name]
            if vacated.table_identity is None:
                continue
            if self.registry.active_binding(name, vacated.catalog, at=at) is not None:
                continue
            try:
                self.registry.bind_compat(
                    name,
                    vacated.table_identity,
                    catalog=vacated.catalog,
                    at=at,
                    group_id=group.id,
                    mapping_id=vacated.id,
                    reason="部分完成：保留原表临时只读兼容名",
                )
            except RegistryError:
                # 该名称已被别的正式绑定接手时，无需兼容名。
                continue

    # -- 状态与待办 -------------------------------------------------------
    def _refresh_group(self, group: RenameGroup, *, at: str) -> None:
        mappings = self.group_mappings(group.id)
        done = sum(1 for item in mappings if item.is_done())
        failed = sum(1 for item in mappings if item.status == MAPPING_FAILED)
        if done == len(mappings):
            group.status = GROUP_COMPLETED
        elif done > 0:
            group.status = GROUP_PARTIAL
        elif failed == len(mappings):
            group.status = GROUP_FAILED
        else:
            group.status = GROUP_PENDING
        group.updated_at = at

    def _sync_review_todos(self, group: RenameGroup) -> None:
        for mapping in self.group_mappings(group.id):
            conclusion, evidence = self._disposition(group, mapping)
            todo = self._find_todo(mapping.id)
            if todo is None:
                self._todo_seq += 1
                todo = {
                    "id": self._todo_seq,
                    "todo_id": self._todo_seq,
                    "group_id": group.id,
                    "mapping_id": mapping.id,
                    "table_identity": mapping.table_identity,
                    "source_name": mapping.source_name,
                    "target_name": mapping.target_name,
                    "review_status": TODO_PENDING,
                    "disposition": mapping.status,
                    "conclusion": conclusion,
                    "evidence": evidence,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "reviewed_at": None,
                }
                self.todos.append(todo)
            else:
                todo.update({
                    "table_identity": mapping.table_identity,
                    "disposition": mapping.status,
                    "conclusion": conclusion,
                    "evidence": evidence,
                    "updated_at": now_iso(),
                })

    def _find_todo(self, mapping_id: int) -> dict[str, Any] | None:
        for todo in self.todos:
            if todo["mapping_id"] == mapping_id:
                return todo
        return None

    def _disposition(self, group: RenameGroup, mapping: NameMapping) -> tuple[str, dict[str, Any]]:
        at = self.registry.now()
        evidence: dict[str, Any] = {
            "group_id": group.id,
            "mapping_id": mapping.id,
            "policy": group.policy,
            "catalog": mapping.catalog,
            "table_identity": mapping.table_identity,
            "source_name": mapping.source_name,
            "target_name": mapping.target_name,
            "mapping_status": mapping.status,
            "mapping_message": mapping.message,
        }
        if mapping.table_identity is not None:
            table = self.registry.table(mapping.table_identity)
            if table is not None:
                evidence["initial_name"] = table.initial_name
                evidence["columns"] = list(table.columns)
                evidence["field_transformations"] = [
                    dict(item) for item in table.field_transformations
                ]
                evidence["current_official_name"] = self.registry.current_official_name(
                    table.identity
                )
                evidence["current_compat_names"] = self.registry.current_compat_names(
                    table.identity
                )

        target = self.registry.resolve(mapping.target_name, catalog=mapping.catalog, as_of=at)
        evidence["target_resolution"] = {
            "state": target["state"],
            "resolved": target["resolved"],
            "table_identity": target["table_identity"],
            "binding_kind": target["binding_kind"],
            "read_only": target["read_only"],
        }

        if mapping.status == MAPPING_APPLIED:
            official = evidence.get("current_official_name") or "（无）"
            conclusion = (
                f"已应用：{mapping.source_name}→{mapping.target_name}；"
                f"表身份 {mapping.table_identity} 随原表保留，现行正式名 {official}"
            )
        elif mapping.status == MAPPING_SKIPPED:
            conclusion = (
                f"已生效跳过：{mapping.source_name}→{mapping.target_name} 此前已经完成，"
                f"本次未重复应用；表身份 {mapping.table_identity} 保持原表"
            )
        elif mapping.status == MAPPING_FAILED:
            conclusion = (
                f"失败：{mapping.source_name}→{mapping.target_name}；{mapping.message or '未应用'}；"
                f"原表 {mapping.table_identity or '（未知）'} 仍保留原名 {mapping.source_name}"
            )
        else:
            if target["resolved"]:
                kind = "临时只读兼容名" if target["binding_kind"] == BINDING_COMPAT else "正式名"
                state_text = f"当前由表 {target['table_identity']} 以{kind}占用"
            else:
                state_text = "当前暂空（无绑定）"
            conclusion = (
                f"待应用：{mapping.source_name}→{mapping.target_name}；名称 "
                f"{mapping.target_name} {state_text}"
            )
        evidence["conclusion"] = conclusion
        evidence["field_lineage"] = [
            {
                "column": item.get("target_column") or item.get("source_column"),
                "source_column": item.get("source_column"),
                "target_column": item.get("target_column"),
                "operator": item.get("operator"),
                "description": item.get("description"),
            }
            for item in evidence.get("field_transformations", [])
        ]
        return conclusion, evidence

    # -- 复核待办 ---------------------------------------------------------
    def review_todo(self, todo_id: int, *, reviewer: str = "", note: str = "") -> dict[str, Any]:
        with self._lock:
            todo = next((item for item in self.todos if item["id"] == todo_id), None)
            if todo is None:
                raise RegistryError(f"复核待办 {todo_id} 不存在")
            todo["review_status"] = TODO_DONE
            todo["reviewed_at"] = now_iso()
            todo["reviewer"] = reviewer.strip()
            todo["review_note"] = note.strip()
            todo["updated_at"] = now_iso()
            return dict(todo)

    # -- 血缘证据 ---------------------------------------------------------
    def lineage_evidence(
        self,
        name: str,
        *,
        as_of: str | None = None,
        catalog: str = DEFAULT_CATALOG,
    ) -> dict[str, Any]:
        """湖表血缘解析证据：从可追溯的表名映射记录定位原表和字段转换。"""
        resolution = self.registry.resolve(name, catalog=catalog, as_of=as_of)
        identity = resolution.get("table_identity")
        evidence: dict[str, Any] = {
            "name": resolution["name"],
            "catalog": resolution["catalog"],
            "as_of": resolution["as_of"],
            "resolved": resolution["resolved"],
            "state": resolution["state"],
            "table_identity": identity,
            "binding_kind": resolution.get("binding_kind"),
            "read_only": resolution.get("read_only", False),
            "writable": resolution.get("writable", False),
            "initial_name": resolution.get("initial_name"),
            "current_official_name": resolution.get("official_name"),
            "current_compat_names": resolution.get("compat_names", []),
            "columns": resolution.get("columns", []),
            "field_transformations": resolution.get("field_transformations", []),
            "name_history": resolution.get("name_history", []),
            "mappings": [],
            "field_lineage": [],
            "locating_mapping_id": None,
        }
        if identity:
            mappings = [
                mapping.to_dict() for mapping in self.mappings
                if mapping.table_identity == identity
            ]
            evidence["mappings"] = mappings
            binding = resolution.get("binding") or {}
            binding_mapping_id = binding.get("mapping_id")
            if binding_mapping_id in {item["mapping_id"] for item in mappings}:
                evidence["locating_mapping_id"] = binding_mapping_id
            evidence["field_lineage"] = [
                {
                    "column": item.get("target_column") or item.get("source_column"),
                    "source_column": item.get("source_column"),
                    "target_column": item.get("target_column"),
                    "operator": item.get("operator"),
                    "description": item.get("description"),
                }
                for item in evidence["field_transformations"]
            ]
        return evidence


ledger = NameMappingLedger()
