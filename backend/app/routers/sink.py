"""流数据输出端接口：维护输出配置，覆盖启用输出、暂停输出、关闭输出等动作。

同时承载输出批次数据回执：校验批次回执后写入回执，重复批次只写一次，
确认水位与回退位置都留在回执上，供水位线管理（另一个执行入口）核对。

注意：固定路径（/export、/receipts）必须声明在 /{entry_id} 之前，
否则会被路径参数抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sink import SinkService
from app.services.sinkreceipt import receipt_service

router = APIRouter(prefix="/api/sink", tags=["流数据输出端"])

service = SinkService()

LIST_FIELDS = ["配置编号", "输出类型", "目标地址", "写入模式", "批处理大小", "刷新间隔", "重试策略", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤流数据输出端列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条输出配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="输出配置已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出流数据输出端清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sink", "total": total, "items": items}


@router.get("/receipts", response_model=PageResult[dict])
def list_receipts(
    keyword: str | None = Query(default=None, description="按配置编号或批次号检索"),
    status: str | None = Query(default=None, description="待确认、已确认、异常、处理中断、重试中、已回退"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """输出批次数据回执列表；空态返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = receipt_service.list_receipts(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/receipts", response_model=ActionResult)
def write_receipt(payload: EntryPayload) -> ActionResult:
    """校验批次回执后写入输出批次数据回执；同一批次重复导入只允许一次写入。"""
    receipt, message = receipt_service.write_receipt(payload.values)
    if receipt is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=receipt)


@router.post("/receipts/{receipt_id}/actions", response_model=ActionResult)
def run_receipt_action(receipt_id: int, payload: EntryPayload) -> ActionResult:
    """对单条回执执行确认回执、重试回执、回退水位；异常回执保留原写入模式和批处理大小。"""
    action = str(payload.values.get("action") or "").strip()
    receipt, message = receipt_service.run_receipt_action(receipt_id, action)
    if receipt is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=receipt)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条输出配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"输出配置 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条输出配置执行启用输出、暂停输出、关闭输出；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
