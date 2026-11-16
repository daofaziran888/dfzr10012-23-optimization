"""水位线管理接口：维护水位线配置，覆盖启用水位线、暂停水位线、关闭水位线等动作。

另一个执行入口：receipt-check 用同一份输出批次数据回执校验水位线结果，
消费侧据此继续后续处理；异常回执与较新水位确认冲突时只认最终确认的回执。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sinkreceipt import receipt_service
from app.services.watermark import WatermarkService

router = APIRouter(prefix="/api/watermark", tags=["水位线管理"])

service = WatermarkService()

LIST_FIELDS = ["配置编号", "流名称", "时间字段", "延迟容忍", "空闲超时", "发射间隔", "对齐策略", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤水位线管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条水位线配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"水位线配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条水位线配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="水位线配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条水位线配置执行启用水位线、暂停水位线、关闭水位线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/receipt-check", response_model=ActionResult)
def receipt_check(entry_id: int) -> ActionResult:
    """用同一份输出批次数据回执校验水位线结果；冲突时水位只退到最后确认批次，不越过失败批次。"""
    result, message = receipt_service.check_watermark(entry_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出水位线管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "watermark", "total": total, "items": items}
