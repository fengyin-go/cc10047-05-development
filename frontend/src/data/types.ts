/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type RenameItem = {
  source_name: string
  target_name: string
}

export type NameMapping = {
  id: number
  mapping_id: number
  group_id: number
  seq: number
  源表名: string
  目标表名: string
  source_name: string
  target_name: string
  table_identity: string | null
  status: string
  message: string
  applied_at: string | null
}

export type ReviewTodo = {
  id: number
  todo_id: number
  group_id: number
  mapping_id: number
  table_identity: string | null
  source_name: string
  target_name: string
  review_status: string
  disposition: string
  conclusion: string
  evidence: Record<string, unknown>
  reviewed_at: string | null
}

export type RenameGroupView = {
  ok?: boolean
  message?: string
  id: number
  group_id: number
  catalog: string
  policy: string
  status: string
  applied_once: boolean
  deduplicated?: boolean
  item_count: number
  applied_count: number
  skipped_count: number
  failed_count: number
  pending_count: number
  mappings: NameMapping[]
  review_todos: ReviewTodo[]
}

export type LakeTableIdentity = {
  table_identity: string
  catalog: string
  initial_name: string
  official_name: string | null
  compat_names: string[]
  columns: string[]
}

export type RenameOverview = {
  tables: LakeTableIdentity[]
  groups: RenameGroupView[]
  mappings: NameMapping[]
  review_todos: ReviewTodo[]
  counts: Record<string, number>
}

export type NameResolution = {
  ok: boolean
  message: string
  name: string
  catalog: string
  as_of: string
  resolved: boolean
  state: string
  table_identity: string | null
  binding_kind: string | null
  read_only: boolean
  writable: boolean
  initial_name: string | null
  current_official_name: string | null
  current_compat_names: string[]
  columns: string[]
  field_lineage: {
    column: string | null
    source_column: string
    target_column: string
    operator: string
    description: string
  }[]
  name_history: {
    name: string
    kind: string
    table_identity: string
    effective_from: string
    effective_to: string | null
    covers_as_of: boolean
  }[]
  mappings: NameMapping[]
  locating_mapping_id: number | null
}
