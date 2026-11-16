import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sink'
const columns = [
  "配置编号",
  "输出类型",
  "目标地址",
  "写入模式",
  "批处理大小",
  "刷新间隔",
  "重试策略",
  "配置状态"
]
const actions = [
  "启用输出",
  "暂停输出",
  "关闭输出"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "输出配置总数",
    "value": 0
  },
  {
    "label": "已启用输出",
    "value": 0
  },
  {
    "label": "已暂停输出",
    "value": 0
  }
]
const receiptColumns = [
  "配置编号",
  "批次号",
  "目标地址",
  "写入模式",
  "批处理大小",
  "记录数",
  "确认水位",
  "回退位置",
  "重复批次",
  "异常原因",
  "回执状态"
]
const receiptActions = [
  "确认回执",
  "重试回执",
  "回退水位"
]
const receiptResults = [
  "成功",
  "失败",
  "中断"
]

type Row = Record<string, string | number | null>
type Receipt = Record<string, string | number | string[] | null>

const emptyReceiptForm = {
  '配置编号': '',
  '批次号': '',
  '记录数': '',
  '回执结果': '成功',
  '异常原因': '',
}

export default function SinkView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [receipts, setReceipts] = useState<Receipt[]>([])
  const [receiptTotal, setReceiptTotal] = useState(0)
  const [receiptMessage, setReceiptMessage] = useState('')
  const [receiptForm, setReceiptForm] = useState<Record<string, string>>(emptyReceiptForm)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('输出配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据输出端列表读取失败')
    }
  }, [filters])

  const reloadReceipts = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/receipts`)
      if (!response.ok) {
        throw new Error('输出批次数据回执读取失败')
      }
      const payload = await response.json()
      setReceipts(payload.items ?? [])
      setReceiptTotal(payload.total ?? 0)
    } catch (error) {
      setReceiptMessage(error instanceof Error ? error.message : '输出批次数据回执读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadReceipts()
  }, [reload, reloadReceipts])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('流数据输出端动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据输出端操作失败')
    }
  }

  async function submitReceipt() {
    setReceiptMessage('')
    try {
      const response = await request(`${ENDPOINT}/receipts`, {
        method: 'POST',
        body: JSON.stringify({ values: receiptForm }),
      })
      const payload = await response.json()
      setReceiptMessage(payload.message ?? '回执已处理')
      if (payload.ok) {
        setReceiptForm(emptyReceiptForm)
      }
      await reloadReceipts()
    } catch (error) {
      setReceiptMessage(error instanceof Error ? error.message : '输出批次数据回执写入失败')
    }
  }

  async function runReceiptAction(action: string, receipt: Receipt) {
    setReceiptMessage('')
    try {
      const response = await request(`${ENDPOINT}/receipts/${receipt.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      setReceiptMessage(payload.message ?? '回执动作已处理')
      await reloadReceipts()
    } catch (error) {
      setReceiptMessage(error instanceof Error ? error.message : '输出批次数据回执操作失败')
    }
  }

  function receiptCell(receipt: Receipt, column: string) {
    if (column === '回执状态') {
      return String(receipt.status ?? '—')
    }
    if (column === '重复批次') {
      const duplicates = Array.isArray(receipt.重复批次) ? receipt.重复批次 : []
      return duplicates.length > 0 ? duplicates.join('、') : '—'
    }
    const value = receipt[column]
    return value === null || value === undefined || value === '' ? '—' : String(value)
  }

  return (
    <section className="page" data-module="sink">
      <header className="page-head">
        <div>
          <h2>流数据输出端管理</h2>
          <p className="page-desc">维护流输出的目标地址、写入模式与批处理配置，为流数据输出端管理提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('输出配置登记入口尚未接入审批流')}>
            登记输出配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流数据输出端清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无流数据输出端数据，可先登记输出配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条流数据输出端记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <header className="page-head">
        <div>
          <h2>输出批次数据回执</h2>
          <p className="page-desc">校验批次回执后写入回执，同一批次重复导入只写一次；回执保留重复批次、确认水位和回退位置。</p>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void submitReceipt() }}>
        <label className="filter-item">
          <span>配置编号</span>
          <input
            value={receiptForm.配置编号}
            placeholder="如 SINK-0002"
            onChange={(event) => setReceiptForm((prev) => ({ ...prev, 配置编号: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>批次号</span>
          <input
            value={receiptForm.批次号}
            placeholder="如 batch-23"
            onChange={(event) => setReceiptForm((prev) => ({ ...prev, 批次号: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>记录数</span>
          <input
            value={receiptForm.记录数}
            placeholder="如 500"
            onChange={(event) => setReceiptForm((prev) => ({ ...prev, 记录数: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>回执结果</span>
          <select
            value={receiptForm.回执结果}
            onChange={(event) => setReceiptForm((prev) => ({ ...prev, 回执结果: event.target.value }))}
          >
            {receiptResults.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>异常原因</span>
          <input
            value={receiptForm.异常原因}
            placeholder="失败或中断时填写"
            onChange={(event) => setReceiptForm((prev) => ({ ...prev, 异常原因: event.target.value }))}
          />
        </label>
        <button className="btn primary" type="submit">写入回执</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {receiptColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {receipts.map((receipt) => (
            <tr key={String(receipt.id)}>
              {receiptColumns.map((column) => <td key={column}>{receiptCell(receipt, column)}</td>)}
              <td className="row-actions">
                {receiptActions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runReceiptAction(action, receipt)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {receipts.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={receiptColumns.length + 1}>暂无输出批次数据回执，可先写入批次回执</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {receiptTotal} 条输出批次数据回执</span>
        {receiptMessage && <span>{receiptMessage}</span>}
      </footer>
    </section>
  )
}
