"""输出批次数据回执业务规则：批次回执校验、幂等写入、水位回退与冲突校验都收在这里。

数据流：sink 路由写入/操作回执 -> 本服务校验并维护回执表 -> watermark 路由用同一份
回执表做水位线结果校验（另一个执行入口，消费侧据此继续后续处理）。

核心约定：
- 同一批次重复导入只允许一次写入，重复批次记录在回执上；
- 异常回执必须保留原写入模式和批处理大小（写入时快照，重试不覆盖）；
- 确认水位只认一套最终确认的回执，不能把已确认水位推进到失败批次之后；
- 水位线只从最后确认批次向前回退；
- 同一输出配置同时只允许一项回执操作生效。
"""
from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any

from app.store import store

MODULE = "sinkreceipt"
SINK_MODULE = "sink"
WATERMARK_MODULE = "watermark"

REQUIRED_FIELDS = ["配置编号", "批次号", "记录数", "回执结果"]
RECEIPT_RESULTS = {"成功": "待确认", "失败": "异常", "中断": "处理中断"}
FAILED_STATUSES = {"异常", "处理中断", "重试中", "已回退"}
RETRYABLE_STATUSES = {"异常", "处理中断", "重试中", "已回退"}
RECEIPT_ACTIONS = {"确认回执", "重试回执", "回退水位"}
INITIAL_WATERMARK = "offset-0"

_BATCH_RE = re.compile(r"^batch-(\d+)$")
_OFFSET_RE = re.compile(r"^offset-(\d+)$")


def _batch_number(batch_no: Any) -> int | None:
    match = _BATCH_RE.match(str(batch_no or "").strip())
    return int(match.group(1)) if match else None


def _offset_of(number: int | None) -> str:
    return f"offset-{number}" if number is not None else INITIAL_WATERMARK


def _offset_number(offset: Any) -> int | None:
    match = _OFFSET_RE.match(str(offset or "").strip())
    return int(match.group(1)) if match else None


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _find_by_code(module: str, code: str) -> dict[str, Any] | None:
    for row in store.rows(module):
        if str(row.get("配置编号", "")) == code:
            return row
    return None


def _linked_watermark_code(sink_code: str) -> str:
    """按既有归属把输出配置关联到水位线配置：SINK-0002 -> WATE-0002。"""
    suffix = str(sink_code).split("-")[-1]
    return f"WATE-{suffix}"


def _config_receipts(sink_code: str) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(MODULE) if row.get("配置编号") == sink_code]
    return sorted(rows, key=lambda row: _batch_number(row.get("批次号")) or 0)


def _find_receipt(sink_code: str, batch_no: str) -> dict[str, Any] | None:
    for row in store.rows(MODULE):
        if row.get("配置编号") == sink_code and row.get("批次号") == batch_no:
            return row
    return None


def _confirmed_batch(sink_code: str, before: int | None = None) -> int | None:
    """最后确认批次：从最小批次起连续「已确认」的最大批次号。

    遇到缺失批次或失败批次即停——已确认水位不能推进到失败批次之后。
    """
    receipts = _config_receipts(sink_code)
    numbers = [n for n in (_batch_number(row.get("批次号")) for row in receipts) if n is not None]
    if not numbers:
        return None
    expected = min(numbers)
    confirmed: int | None = None
    for receipt in receipts:
        number = _batch_number(receipt.get("批次号"))
        if number is None or number < expected:
            continue
        if before is not None and number >= before:
            break
        if number > expected:
            break  # 缺失批次，水位不能跳过
        if receipt.get("status") != "已确认":
            break  # 失败批次挡住水位
        confirmed = number
        expected = number + 1
    return confirmed


def _next_id() -> int:
    return max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1


class SinkReceiptService:
    """输出批次数据回执：写入、动作与水位校验共用同一套回执数据。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------- 查询 ----------

    def list_receipts(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("配置编号", "")) or keyword in str(row.get("批次号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: (str(row.get("配置编号", "")), _batch_number(row.get("批次号")) or 0))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, receipt_id)

    # ---------- 写入 ----------

    def write_receipt(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """校验批次回执后写入输出批次数据回执；同一批次重复导入只写一次。"""
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        sink_code = str(values["配置编号"]).strip()
        batch_no = str(values["批次号"]).strip()
        result = str(values["回执结果"]).strip()
        reason = str(values.get("异常原因") or "").strip()

        sink = _find_by_code(SINK_MODULE, sink_code)
        if sink is None:
            return None, f"输出配置 {sink_code} 不存在或已归档"
        if _batch_number(batch_no) is None:
            return None, f"批次号「{batch_no}」格式应为 batch-数字，例如 batch-23"
        if result not in RECEIPT_RESULTS:
            return None, f"回执结果「{result}」不在允许范围：{'、'.join(RECEIPT_RESULTS)}"
        count = _parse_int(values.get("记录数"))
        if count is None or count <= 0:
            return None, "记录数必须是正整数"
        batch_size = _parse_int(sink.get("批处理大小"))
        if batch_size is not None and count > batch_size:
            return None, f"记录数 {count} 超过批处理大小 {batch_size}，批次回执校验未通过"
        if result != "成功" and not reason:
            reason = "未说明"

        lock = self._lock_for(sink_code)
        if not lock.acquire(blocking=False):
            return None, f"输出配置 {sink_code} 有回执操作进行中，同时操作只允许一项生效"
        try:
            return self._write_locked(sink, batch_no, count, result, reason)
        finally:
            lock.release()

    def _write_locked(
        self,
        sink: dict[str, Any],
        batch_no: str,
        count: int,
        result: str,
        reason: str,
    ) -> tuple[dict[str, Any], str]:
        sink_code = str(sink["配置编号"])
        existing = _find_receipt(sink_code, batch_no)
        if existing is not None:
            duplicates = existing.setdefault("重复批次", [])
            duplicates.append(batch_no)
            existing["重复次数"] = len(duplicates)
            if existing.get("status") in RETRYABLE_STATUSES:
                # 重新导入只影响缺失批次：同一行更新，保留原写入模式和批处理大小
                existing["重试次数"] = int(existing.get("重试次数", 0)) + 1
                existing["记录数"] = count
                existing["status"] = RECEIPT_RESULTS[result]
                existing["pending"] = result == "成功"
                existing["abnormal"] = result != "成功"
                existing["异常原因"] = "" if result == "成功" else reason
                existing["回执时间"] = date.today().isoformat()
                self._refresh_watermark(sink_code)
                if result == "成功":
                    return existing, f"缺失批次 {batch_no} 重试写入成功，保留原写入模式与批处理大小"
                return existing, f"缺失批次 {batch_no} 重试仍未成功：{reason}"
            # 已写入过的批次：重复导入只记录，不再写入
            self._refresh_watermark(sink_code)
            return existing, f"批次 {batch_no} 已写入，同一批次重复导入只允许一次写入，本次已忽略"

        receipt = {
            "id": _next_id(),
            "配置编号": sink_code,
            "批次号": batch_no,
            "目标地址": sink.get("目标地址"),
            "写入模式": sink.get("写入模式"),
            "批处理大小": _parse_int(sink.get("批处理大小")) or sink.get("批处理大小"),
            "记录数": count,
            "水位线配置": _linked_watermark_code(sink_code),
            "status": RECEIPT_RESULTS[result],
            "pending": result == "成功",
            "abnormal": result != "成功",
            "异常原因": "" if result == "成功" else reason,
            "重复批次": [],
            "重复次数": 0,
            "重试次数": 0,
            "回执时间": date.today().isoformat(),
        }
        store.rows(MODULE).append(receipt)
        self._refresh_watermark(sink_code)
        return receipt, f"批次 {batch_no} 回执已写入（{receipt['status']}），当前确认水位 {receipt['确认水位']}"

    # ---------- 回执动作 ----------

    def run_receipt_action(self, receipt_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        receipt = store.find(MODULE, receipt_id)
        if receipt is None:
            return None, f"输出批次数据回执 {receipt_id} 不存在或已归档"
        if action not in RECEIPT_ACTIONS:
            return None, f"动作「{action}」不属于输出批次数据回执可执行范围"
        sink_code = str(receipt.get("配置编号", ""))
        lock = self._lock_for(sink_code)
        if not lock.acquire(blocking=False):
            return None, f"输出配置 {sink_code} 有回执操作进行中，同时操作只允许一项生效"
        try:
            return self._action_locked(receipt, action)
        finally:
            lock.release()

    def _action_locked(self, receipt: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        status = str(receipt.get("status", ""))
        batch_no = str(receipt.get("批次号", ""))
        sink_code = str(receipt.get("配置编号", ""))
        if action == "确认回执":
            if status == "待确认":
                receipt["status"] = "已确认"
                receipt["pending"] = False
                receipt["abnormal"] = False
                watermark = self._refresh_watermark(sink_code)
                return receipt, f"批次 {batch_no} 回执已确认，确认水位推进到 {watermark}"
            if status in {"异常", "处理中断", "重试中"}:
                # 确认异常处理结果：水位只停在最后确认批次，不越过失败批次
                receipt["status"] = "已回退"
                receipt["pending"] = False
                receipt["abnormal"] = True
                watermark = self._refresh_watermark(sink_code)
                return receipt, f"批次 {batch_no} 异常回执已确认，水位停在 {watermark}（回退位置 {receipt['回退位置']}）"
            return receipt, f"批次 {batch_no} 已是{status}状态，不重复确认"
        if action == "重试回执":
            if status not in RETRYABLE_STATUSES - {"重试中"}:
                return None, f"批次 {batch_no} 当前状态「{status}」不允许重试"
            receipt["status"] = "重试中"
            receipt["pending"] = True
            self._refresh_watermark(sink_code)
            return receipt, f"批次 {batch_no} 已转入重试中，等待重新导入（保留原写入模式与批处理大小）"
        # 回退水位
        if status == "已回退":
            return receipt, f"批次 {batch_no} 已回退到 {receipt['回退位置']}，不重复回退"
        if status not in {"异常", "处理中断", "重试中"}:
            return None, f"批次 {batch_no} 当前状态「{status}」无需回退水位"
        receipt["status"] = "已回退"
        receipt["pending"] = False
        receipt["abnormal"] = True
        watermark = self._refresh_watermark(sink_code)
        return receipt, f"批次 {batch_no} 水位已回退到 {receipt['回退位置']}，确认水位 {watermark}"

    # ---------- 水位线结果校验（另一个执行入口） ----------

    def check_watermark(self, watermark_id: int) -> tuple[dict[str, Any] | None, str]:
        """用同一份输出批次数据回执校验水位线结果，消费侧据此继续后续处理。"""
        wate = store.find(WATERMARK_MODULE, watermark_id)
        if wate is None:
            return None, f"水位线配置 {watermark_id} 不存在或已归档"
        code = str(wate.get("配置编号", ""))
        receipts = [row for row in store.rows(MODULE) if row.get("水位线配置") == code]
        current = str(wate.get("当前水位") or INITIAL_WATERMARK)
        if not receipts:
            return {
                "水位线配置": code,
                "输出配置": None,
                "确认水位": current,
                "回退位置": None,
                "重复批次": [],
                "异常批次": [],
                "冲突": False,
                "空态": True,
                "回执数": 0,
            }, f"水位线配置 {code} 暂无输出批次数据回执，保持当前水位 {current}"

        sink_code = str(receipts[0].get("配置编号", ""))
        lock = self._lock_for(sink_code)
        if not lock.acquire(blocking=False):
            return None, f"输出配置 {sink_code} 有回执操作进行中，同时操作只允许一项生效"
        try:
            final = self._refresh_watermark(sink_code)
            conflict = _offset_number(current) != _offset_number(final)
            if conflict:
                # 异常回执与较新的水位确认冲突：只认一套最终确认的回执，水位退回确认位置
                wate["当前水位"] = final
            failed = sorted(
                (str(row.get("批次号")) for row in receipts if row.get("status") in FAILED_STATUSES),
                key=lambda item: _batch_number(item) or 0,
            )
            duplicates = sorted({item for row in receipts for item in row.get("重复批次", [])})
            rollback = None
            if failed:
                first_failed = _find_receipt(sink_code, failed[0])
                rollback = first_failed.get("回退位置") if first_failed else None
            result = {
                "水位线配置": code,
                "输出配置": sink_code,
                "确认水位": final,
                "回退位置": rollback,
                "重复批次": duplicates,
                "异常批次": failed,
                "冲突": conflict,
                "空态": False,
                "回执数": len(receipts),
            }
            if conflict:
                return result, f"水位确认冲突：较新水位 {current} 与最终确认回执不一致，已依据回执回退到 {final}，不越过失败批次"
            return result, f"水位线结果校验通过，确认水位 {final}"
        finally:
            lock.release()

    # ---------- 内部 ----------

    def _lock_for(self, sink_code: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(sink_code, threading.Lock())

    def _refresh_watermark(self, sink_code: str) -> str:
        """重算确认水位并同步到每张回执与水位线配置（消费侧共享同一份结果）。"""
        confirmed = _confirmed_batch(sink_code)
        watermark = _offset_of(confirmed)
        for receipt in _config_receipts(sink_code):
            receipt["确认水位"] = watermark
            number = _batch_number(receipt.get("批次号"))
            if receipt.get("status") in FAILED_STATUSES:
                # 水位线只从最后确认批次向前回退
                receipt["回退位置"] = _offset_of(_confirmed_batch(sink_code, before=number))
            elif number is not None:
                receipt["回退位置"] = _offset_of(number - 1)
            else:
                receipt["回退位置"] = watermark
        wate = _find_by_code(WATERMARK_MODULE, _linked_watermark_code(sink_code))
        if wate is not None:
            wate["当前水位"] = watermark
        return watermark


receipt_service = SinkReceiptService()
