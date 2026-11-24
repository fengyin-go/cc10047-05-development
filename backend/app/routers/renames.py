"""表名迁移与血缘解析接口：批量改名、映射记录、复核待办、名称解析证据。

路径统一挂在 ``/api/table-catalog`` 和 ``/api/lake-lineage`` 下，避免与通用
``/api/{module}`` 动态路由冲突（固定前缀路由先注册）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from fastapi import APIRouter, Query

from app.services import service_for

router = APIRouter(prefix="/api", tags=["表名迁移与血缘"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class RenameItem(BaseModel):
    source_name: str
    target_name: str


class FieldTransform(BaseModel):
    source_column: str
    target_column: str
    operator: str = "DIRECT"
    description: str = ""


class RegisterTableRequest(BaseModel):
    name: str
    catalog: str = "lakehouse"
    columns: list[str] = Field(default_factory=lambda: ["id", "name", "amount"])
    field_transformations: list[FieldTransform] = Field(default_factory=list)


class RenameBatchRequest(BaseModel):
    items: list[RenameItem] = Field(default_factory=list)
    policy: str = "vacant"
    apply_only: list[str] | None = None
    catalog: str = "lakehouse"
    remark: str = ""


class ApplyRenamesRequest(BaseModel):
    apply_only: list[str] | None = None


class ReviewTodoRequest(BaseModel):
    reviewer: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 湖表身份登记
# ---------------------------------------------------------------------------

@router.get("/table-catalog/tables")
def list_registered_tables() -> dict[str, Any]:
    """列出登记簿里的湖表身份与当前可读名称。"""
    service = service_for("table_catalog")
    overview = service.rename_overview()
    return {"ok": True, "message": f"共 {len(overview['tables'])} 张湖表", "items": overview["tables"]}


@router.post("/table-catalog/tables")
def register_table(payload: RegisterTableRequest) -> dict[str, Any]:
    """登记一张湖表（及其字段转换），供后续批量改名和血缘解析使用。"""
    service = service_for("table_catalog")
    return service.register_table(
        payload.name,
        catalog=payload.catalog,
        columns=payload.columns,
        field_transformations=[item.model_dump() for item in payload.field_transformations],
    )


# ---------------------------------------------------------------------------
# 表目录同步：批量迁移表名
# ---------------------------------------------------------------------------

@router.get("/table-catalog/renames")
def rename_overview() -> dict[str, Any]:
    """改名批次总览：湖表身份、批次、表名映射记录和字段血缘复核待办。"""
    return service_for("table_catalog").rename_overview()


@router.post("/table-catalog/renames")
def submit_rename_batch(payload: RenameBatchRequest) -> dict[str, Any]:
    """提交一批表名迁移；同一组重复提交只补做未完成项，已完成的改名不重复应用。"""
    service = service_for("table_catalog")
    return service.rename_batch(
        [item.model_dump() for item in payload.items],
        policy=payload.policy,
        apply_only=payload.apply_only,
        catalog=payload.catalog,
        remark=payload.remark,
    )


@router.get("/table-catalog/renames/groups")
def list_rename_groups() -> dict[str, Any]:
    service = service_for("table_catalog")
    groups = service.rename_groups()
    return {"ok": True, "message": f"共 {len(groups)} 个改名批次", "items": groups}


@router.get("/table-catalog/renames/groups/{group_id}")
def get_rename_group(group_id: int) -> dict[str, Any]:
    service = service_for("table_catalog")
    view = service.rename_group(group_id)
    if view is None:
        return {"ok": False, "message": f"改名批次 {group_id} 不存在"}
    return {"ok": True, "message": "改名批次已读取", **view}


@router.post("/table-catalog/renames/groups/{group_id}/apply")
def apply_rename_group(group_id: int, payload: ApplyRenamesRequest | None = None) -> dict[str, Any]:
    """重试某批次的未完成项；已完成的改名不会重复应用。"""
    service = service_for("table_catalog")
    apply_only = payload.apply_only if payload is not None else None
    return service.apply_renames(group_id, apply_only=apply_only)


@router.get("/table-catalog/renames/mappings")
def list_name_mappings() -> dict[str, Any]:
    service = service_for("table_catalog")
    mappings = service.name_mappings()
    return {"ok": True, "message": f"共 {len(mappings)} 条表名映射记录", "items": mappings}


@router.get("/table-catalog/lineage-review-todos")
def list_lineage_review_todos(
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    service = service_for("table_catalog")
    todos = service.lineage_review_todos(status)
    return {"ok": True, "message": f"共 {len(todos)} 条字段血缘复核待办", "items": todos}


@router.post("/table-catalog/lineage-review-todos/{todo_id}/review")
def review_lineage_todo(todo_id: int, payload: ReviewTodoRequest | None = None) -> dict[str, Any]:
    service = service_for("table_catalog")
    reviewer = payload.reviewer if payload is not None else ""
    note = payload.note if payload is not None else ""
    return service.review_lineage_todo(todo_id, reviewer=reviewer, note=note)


# ---------------------------------------------------------------------------
# 湖表血缘：按名称解析原表
# ---------------------------------------------------------------------------

@router.get("/lake-lineage/resolve")
def resolve_lineage_name(
    name: str = Query(..., min_length=1),
    as_of: str | None = Query(default=None),
    catalog: str = Query(default="lakehouse"),
) -> dict[str, Any]:
    """解析可读表名（含历史旧名）到原表身份与字段转换。"""
    service = service_for("lake_lineage")
    return service.resolve_table(name, as_of=as_of, catalog=catalog)


@router.get("/lake-lineage/resolve-batch")
def resolve_lineage_names(
    names: str = Query(..., description="逗号分隔的表名"),
    as_of: str | None = Query(default=None),
    catalog: str = Query(default="lakehouse"),
) -> dict[str, Any]:
    service = service_for("lake_lineage")
    return service.batch_resolve(
        [item for item in names.split(",") if item.strip()],
        as_of=as_of,
        catalog=catalog,
    )


@router.get("/lake-lineage/name-history")
def lineage_name_history(
    name: str = Query(..., min_length=1),
    catalog: str = Query(default="lakehouse"),
) -> dict[str, Any]:
    service = service_for("lake_lineage")
    return service.name_history(name, catalog=catalog)
