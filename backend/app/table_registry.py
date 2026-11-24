"""湖表表名注册表：稳定表身份、可读名称绑定和按时间点解析。

本模块只负责“名称 -> 表身份”的底层事实，不涉及批量迁移流程：

* 每张湖表有稳定的 ``table_identity``，改名只切换可读名称，身份随原表保留；
* 名称绑定分正式名 ``official`` 与临时只读兼容名 ``compat``；
* 绑定带 ``[effective_from, effective_to)`` 生效区间，历史旧名仍可解析；
* 同一个可读名称在任一时刻至多绑定一张表，登记和改名前都会校验。

批量流程、表名映射记录和字段血缘复核待办在 ``app.name_mapping``。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_CATALOG = "lakehouse"
DEFAULT_COLUMNS = ["id", "name", "amount"]

# 部分完成态的“名称空洞”处置策略：暂空 / 保留原表的临时只读兼容名。
POLICY_VACANT = "vacant"
POLICY_COMPAT = "compat"
POLICIES = {POLICY_VACANT, POLICY_COMPAT}

BINDING_OFFICIAL = "official"
BINDING_COMPAT = "compat"

STATE_ACTIVE = "active"
STATE_HISTORICAL = "historical"
STATE_VACANT = "vacant"
STATE_UNKNOWN = "unknown"

_SEED_EFFECTIVE_FROM = "2026-01-01T00:00:00.000000+00:00"


class RegistryError(ValueError):
    """表名登记、绑定或解析的业务校验错误，由路由层转成可读消息。"""


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(value: Any) -> datetime | None:
    """把日期或 ISO 时间统一成 UTC datetime；空值表示“无界/当前”。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RegistryError(f"无法解析时间：{value}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _covers(binding: dict[str, Any], moment: datetime) -> bool:
    start = parse_ts(binding.get("effective_from")) or datetime.min.replace(tzinfo=timezone.utc)
    if start > moment:
        return False
    end = parse_ts(binding.get("effective_to"))
    return end is None or moment < end


def _clean_name(value: Any) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# 表记录
# ---------------------------------------------------------------------------

@dataclass
class TableRecord:
    identity: str
    catalog: str
    initial_name: str
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    field_transformations: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_identity": self.identity,
            "identity": self.identity,
            "catalog": self.catalog,
            "initial_name": self.initial_name,
            "columns": list(self.columns),
            "field_transformations": [dict(item) for item in self.field_transformations],
            "created_at": self.created_at,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

class TableNameRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clock: Callable[[], str] | None = None
        self.tables: dict[str, TableRecord] = {}
        self.bindings: list[dict[str, Any]] = []
        self._binding_seq = 0
        self.reset(preload=True)

    # -- 生命周期 ---------------------------------------------------------
    def set_clock(self, clock: Callable[[], str] | None) -> None:
        with self._lock:
            self._clock = clock

    def now(self) -> str:
        return self._clock() if self._clock is not None else now_iso()

    def reset(self, preload: bool = True) -> None:
        with self._lock:
            self.tables = {}
            self.bindings = []
            self._binding_seq = 0
            if preload:
                self._preload_demo_tables()

    def _preload_demo_tables(self) -> None:
        transforms = [{
            "source_column": "amount",
            "target_column": "amount",
            "operator": "DIRECT",
            "description": "amount 列原样写入下游湖表",
        }]
        self.register_table(
            "A", columns=list(DEFAULT_COLUMNS),
            field_transformations=transforms,
            effective_from=_SEED_EFFECTIVE_FROM, reason="seed",
        )
        self.register_table(
            "B", columns=list(DEFAULT_COLUMNS),
            field_transformations=transforms,
            effective_from=_SEED_EFFECTIVE_FROM, reason="seed",
        )

    # -- 登记 -------------------------------------------------------------
    def register_table(
        self,
        name: str,
        *,
        catalog: str = DEFAULT_CATALOG,
        columns: list[str] | None = None,
        field_transformations: list[dict[str, Any]] | None = None,
        identity: str | None = None,
        effective_from: str | None = None,
        reason: str = "register",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = _clean_name(name)
        catalog = _clean_name(catalog) or DEFAULT_CATALOG
        if not name:
            raise RegistryError("表名称不能为空")
        with self._lock:
            at = effective_from or self.now()
            moment = parse_ts(at)
            assert moment is not None
            active = self._active_bindings(name, catalog, moment)
            if active:
                other = active[0]["table_identity"]
                raise RegistryError(f"名称「{name}」已绑定表 {other}，同一可读名只能对应一张表")
            identity = _clean_name(identity) or f"lake://{catalog}/{name}"
            if identity in self.tables:
                suffix = 2
                base = identity
                while f"{base}#{suffix}" in self.tables:
                    suffix += 1
                identity = f"{base}#{suffix}"
            table = TableRecord(
                identity=identity,
                catalog=catalog,
                initial_name=name,
                columns=list(columns) if columns else list(DEFAULT_COLUMNS),
                field_transformations=[dict(item) for item in (field_transformations or [])],
                created_at=at,
                metadata=dict(metadata or {}),
            )
            self.tables[identity] = table
            self._add_binding(
                name=name, catalog=catalog, identity=identity,
                kind=BINDING_OFFICIAL, effective_from=at,
                reason=reason,
            )
            return self._table_view(table, moment)

    # -- 绑定 -------------------------------------------------------------
    def _add_binding(
        self,
        *,
        name: str,
        catalog: str,
        identity: str,
        kind: str,
        effective_from: str,
        reason: str = "",
        group_id: int | None = None,
        mapping_id: int | None = None,
    ) -> dict[str, Any]:
        self._binding_seq += 1
        binding = {
            "id": self._binding_seq,
            "name": name,
            "catalog": catalog,
            "table_identity": identity,
            "kind": kind,
            "read_only": kind == BINDING_COMPAT,
            "effective_from": effective_from,
            "effective_to": None,
            "reason": reason,
            "group_id": group_id,
            "mapping_id": mapping_id,
        }
        self.bindings.append(binding)
        return dict(binding)

    def active_binding(
        self, name: str, catalog: str = DEFAULT_CATALOG, *, at: str | None = None,
    ) -> dict[str, Any] | None:
        """返回某名称在指定时刻的唯一活跃绑定；无绑定时返回 None。"""
        moment = parse_ts(at) if at else parse_ts(self.now())
        assert moment is not None
        with self._lock:
            active = self._active_bindings(_clean_name(name), catalog, moment)
            return dict(active[0]) if active else None

    def restore_binding(self, binding_id: int, *, at: str | None = None) -> dict[str, Any] | None:
        """撤销一次 retire：把失效结束时间回退为空，用于失败项保持原名。"""
        moment = parse_ts(at) if at else parse_ts(self.now())
        assert moment is not None
        with self._lock:
            for binding in self.bindings:
                if binding["id"] != binding_id:
                    continue
                others = [
                    other for other in self._active_bindings(
                        binding["name"], binding["catalog"], moment,
                    )
                    if other["id"] != binding_id
                ]
                if others:
                    raise RegistryError(
                        f"名称「{binding['name']}」已被表 {others[0]['table_identity']} 占用，"
                        "无法恢复原绑定"
                    )
                binding["effective_to"] = None
                binding.pop("retire_reason", None)
                return dict(binding)
            return None

    def _active_bindings(self, name: str, catalog: str, moment: datetime) -> list[dict[str, Any]]:
        found = [
            binding for binding in self.bindings
            if binding["name"] == name
            and binding["catalog"] == catalog
            and _covers(binding, moment)
        ]
        if len(found) > 1:
            raise RegistryError(
                f"名称「{name}」在 { _iso(moment) } 同时绑定 {len(found)} 张表，"
                "违反“任何可读表名只对应一张表”"
            )
        return found

    def bind_official(
        self,
        name: str,
        identity: str,
        *,
        catalog: str = DEFAULT_CATALOG,
        at: str | None = None,
        group_id: int | None = None,
        mapping_id: int | None = None,
        reason: str = "rename",
        replace_compat: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """给表绑定正式名称；如目标位置有临时兼容名，可按需摘除后顶替。"""
        name = _clean_name(name)
        catalog = _clean_name(catalog) or DEFAULT_CATALOG
        at = at or self.now()
        moment = parse_ts(at)
        assert moment is not None
        with self._lock:
            if identity not in self.tables:
                raise RegistryError(f"表身份 {identity} 不存在")
            active = self._active_bindings(name, catalog, moment)
            retired: dict[str, Any] | None = None
            if active:
                binding = active[0]
                if binding["table_identity"] == identity:
                    return dict(binding), None
                if binding["kind"] == BINDING_OFFICIAL:
                    raise RegistryError(f"名称「{name}」已由表 {binding['table_identity']} 正式占用")
                if not replace_compat:
                    raise RegistryError(f"名称「{name}」存在临时兼容名，需显式顶替")
                retired = self.retire(binding["id"], at=at, reason="正式改名顶替兼容名")
            binding = self._add_binding(
                name=name, catalog=catalog, identity=identity,
                kind=BINDING_OFFICIAL, effective_from=at, reason=reason,
                group_id=group_id, mapping_id=mapping_id,
            )
            return binding, retired

    def bind_compat(
        self,
        name: str,
        identity: str,
        *,
        catalog: str = DEFAULT_CATALOG,
        at: str | None = None,
        group_id: int | None = None,
        mapping_id: int | None = None,
        reason: str = "partial-compat",
    ) -> dict[str, Any]:
        """给尚未完成改名的名称保留一个临时只读兼容名。"""
        name = _clean_name(name)
        catalog = _clean_name(catalog) or DEFAULT_CATALOG
        at = at or self.now()
        moment = parse_ts(at)
        assert moment is not None
        with self._lock:
            if identity not in self.tables:
                raise RegistryError(f"表身份 {identity} 不存在")
            active = self._active_bindings(name, catalog, moment)
            if active:
                raise RegistryError(f"名称「{name}」已有绑定，不能再建兼容名")
            return self._add_binding(
                name=name, catalog=catalog, identity=identity,
                kind=BINDING_COMPAT, effective_from=at, reason=reason,
                group_id=group_id, mapping_id=mapping_id,
            )

    def retire(self, binding_id: int, *, at: str | None = None, reason: str = "") -> dict[str, Any] | None:
        at = at or self.now()
        moment = parse_ts(at)
        assert moment is not None
        with self._lock:
            for binding in self.bindings:
                if binding["id"] != binding_id:
                    continue
                end = parse_ts(binding.get("effective_to"))
                if end is None or end > moment:
                    binding["effective_to"] = at
                if reason:
                    binding["retire_reason"] = reason
                return dict(binding)
            return None

    def retire_name(
        self,
        name: str,
        *,
        catalog: str = DEFAULT_CATALOG,
        at: str | None = None,
        kind: str | None = None,
        reason: str = "",
    ) -> dict[str, Any] | None:
        at = at or self.now()
        moment = parse_ts(at)
        assert moment is not None
        with self._lock:
            active = self._active_bindings(_clean_name(name), catalog, moment)
            if kind is not None:
                active = [item for item in active if item["kind"] == kind]
            if not active:
                return None
            return self.retire(active[0]["id"], at=at, reason=reason)

    # -- 查询 -------------------------------------------------------------
    def table(self, identity: str) -> TableRecord | None:
        return self.tables.get(identity)

    def current_official_name(self, identity: str) -> str | None:
        moment = parse_ts(self.now())
        assert moment is not None
        with self._lock:
            for binding in self.bindings:
                if (
                    binding["table_identity"] == identity
                    and binding["kind"] == BINDING_OFFICIAL
                    and _covers(binding, moment)
                ):
                    return binding["name"]
        return None

    def current_compat_names(self, identity: str) -> list[str]:
        moment = parse_ts(self.now())
        assert moment is not None
        with self._lock:
            return [
                binding["name"] for binding in self.bindings
                if binding["table_identity"] == identity
                and binding["kind"] == BINDING_COMPAT
                and _covers(binding, moment)
            ]

    def bindings_for_identity(self, identity: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(item) for item in self.bindings if item["table_identity"] == identity]
        return sorted(rows, key=lambda item: (item["effective_from"], item["id"]))

    def name_bindings(self, name: str, catalog: str = DEFAULT_CATALOG) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(item) for item in self.bindings
                if item["name"] == _clean_name(name) and item["catalog"] == catalog
            ]
        return sorted(rows, key=lambda item: (item["effective_from"], item["id"]))

    def _table_view(self, table: TableRecord, moment: datetime) -> dict[str, Any]:
        view = table.to_dict()
        view["official_name"] = self.current_official_name(table.identity)
        view["compat_names"] = self.current_compat_names(table.identity)
        return view

    def list_tables(self, as_of: str | None = None) -> list[dict[str, Any]]:
        moment = parse_ts(as_of) if as_of else parse_ts(self.now())
        assert moment is not None
        with self._lock:
            return [self._table_view(table, moment) for table in self.tables.values()]

    def resolve(
        self,
        name: str,
        *,
        catalog: str = DEFAULT_CATALOG,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """解析一个可读名称；当前无绑定时明确返回 vacant/unknown，不猜来源。"""
        name = _clean_name(name)
        catalog = _clean_name(catalog) or DEFAULT_CATALOG
        moment = parse_ts(as_of) if as_of else parse_ts(self.now())
        assert moment is not None
        with self._lock:
            active = self._active_bindings(name, catalog, moment)
            history = self.name_bindings(name, catalog)
            if active:
                binding = active[0]
                table = self.tables.get(binding["table_identity"])
                state = STATE_ACTIVE if binding["effective_to"] is None else STATE_HISTORICAL
                result = {
                    "name": name,
                    "catalog": catalog,
                    "as_of": _iso(moment),
                    "resolved": True,
                    "state": state,
                    "table_identity": binding["table_identity"],
                    "binding": binding,
                    "binding_kind": binding["kind"],
                    "read_only": bool(binding["read_only"]),
                    "writable": bool(
                        binding["kind"] == BINDING_OFFICIAL
                        and binding["effective_to"] is None
                        and table is not None
                        and table.status == "active"
                    ),
                }
                if table is not None:
                    result.update({
                        "initial_name": table.initial_name,
                        "columns": list(table.columns),
                        "field_transformations": [dict(item) for item in table.field_transformations],
                        "official_name": self.current_official_name(table.identity),
                        "compat_names": self.current_compat_names(table.identity),
                        "table": table.to_dict(),
                    })
                result["name_history"] = self._history_view(name, catalog, moment)
                return result
            return {
                "name": name,
                "catalog": catalog,
                "as_of": _iso(moment),
                "resolved": False,
                "state": STATE_VACANT if history else STATE_UNKNOWN,
                "table_identity": None,
                "binding": None,
                "binding_kind": None,
                "read_only": False,
                "writable": False,
                "initial_name": None,
                "columns": [],
                "field_transformations": [],
                "official_name": None,
                "compat_names": [],
                "table": None,
                "name_history": self._history_view(name, catalog, moment),
            }

    def _history_view(
        self, name: str, catalog: str, moment: datetime,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for binding in self.name_bindings(name, catalog):
            table = self.tables.get(binding["table_identity"])
            item = dict(binding)
            item["covers_as_of"] = _covers(binding, moment)
            item["active_now"] = _covers(binding, parse_ts(self.now()) or moment)
            item["initial_name"] = table.initial_name if table else None
            item["columns"] = list(table.columns) if table else []
            rows.append(item)
        return rows


registry = TableNameRegistry()
