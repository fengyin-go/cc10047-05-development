import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  NameResolution,
  OverviewResult,
  PageResult,
  RenameGroupView,
  RenameItem,
  RenameOverview,
  ReviewTodo,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 批量迁移表名 / 表名映射 / 字段血缘复核待办
// ---------------------------------------------------------------------------

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

export async function loadRenameOverview(): Promise<RenameOverview> {
  return readJson<RenameOverview>(
    await request('/api/table-catalog/renames'),
    '改名批次读取失败',
  )
}

export async function submitRenameBatch(
  items: RenameItem[],
  options: { policy?: string; applyOnly?: string[]; remark?: string } = {},
): Promise<RenameGroupView> {
  return readJson<RenameGroupView>(
    await request('/api/table-catalog/renames', {
      method: 'POST',
      body: JSON.stringify({
        items,
        policy: options.policy ?? 'vacant',
        apply_only: options.applyOnly ?? null,
        remark: options.remark ?? '',
      }),
    }),
    '批量迁移表名提交失败',
  )
}

export async function applyRenameGroup(
  groupId: number,
  applyOnly?: string[],
): Promise<RenameGroupView> {
  return readJson<RenameGroupView>(
    await request(`/api/table-catalog/renames/groups/${groupId}/apply`, {
      method: 'POST',
      body: JSON.stringify({ apply_only: applyOnly ?? null }),
    }),
    '补齐改名批次失败',
  )
}

export async function loadReviewTodos(): Promise<{ ok: boolean; items: ReviewTodo[] }> {
  return readJson(
    await request('/api/table-catalog/lineage-review-todos'),
    '字段血缘复核待办读取失败',
  )
}

export async function reviewTodo(todoId: number, note = ''): Promise<ActionResult> {
  return readJson(
    await request(`/api/table-catalog/lineage-review-todos/${todoId}/review`, {
      method: 'POST',
      body: JSON.stringify({ reviewer: '数据工程组', note }),
    }),
    '复核确认失败',
  )
}

export async function resolveTableName(
  name: string,
  asOf?: string,
): Promise<NameResolution> {
  const query = new URLSearchParams({ name })
  if (asOf) query.set('as_of', asOf)
  return readJson<NameResolution>(
    await request(`/api/lake-lineage/resolve?${query.toString()}`),
    '表名解析失败',
  )
}
