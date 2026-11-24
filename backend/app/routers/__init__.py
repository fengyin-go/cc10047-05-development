"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, chains, renames

# renames 的固定路径（/api/table-catalog/renames 等）含两段路径，必须排在
# 通用资源路由 /api/{module}/{entry_id} 之前，否则会被动态路径抢先匹配。
ROUTERS = [renames, resource, chains]
