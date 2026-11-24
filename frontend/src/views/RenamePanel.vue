<template>
  <section class="rename-panel" data-testid="rename-panel">
    <header class="panel-head">
      <div>
        <h3>批量迁移表名</h3>
        <p class="panel-desc">
          一次提交多条改名，形成可追溯的表名映射记录；表的身份随原表保留。
          部分完成时名称按策略暂空或保留临时只读兼容名。
        </p>
      </div>
      <button class="btn" data-testid="rename-refresh" type="button" @click="refresh">
        刷新映射记录
      </button>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">湖表身份</span>
        <strong class="stat-value">{{ overview.counts.tables ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">改名批次</span>
        <strong class="stat-value">{{ overview.counts.groups ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已应用映射</span>
        <strong class="stat-value">{{ overview.counts.applied ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待复核待办</span>
        <strong class="stat-value">{{ overview.counts.todos_pending ?? 0 }}</strong>
      </article>
    </div>

    <table class="data-table" data-testid="table-identities">
      <thead>
        <tr><th>表身份</th><th>原表名</th><th>现行正式名</th><th>临时兼容名</th><th>列</th></tr>
      </thead>
      <tbody>
        <tr v-for="table in overview.tables" :key="table.table_identity" :data-testid="`identity-${table.initial_name}`">
          <td>{{ table.table_identity }}</td>
          <td>{{ table.initial_name }}</td>
          <td>{{ table.official_name ?? '—' }}</td>
          <td>{{ table.compat_names.length ? table.compat_names.join('、') : '—' }}</td>
          <td>{{ table.columns.join('、') }}</td>
        </tr>
      </tbody>
    </table>

    <form class="rename-form" data-testid="rename-form" @submit.prevent="submit">
      <h4>提交改名批次</h4>
      <div v-for="(item, index) in items" :key="index" class="rename-item">
        <input v-model="item.source_name" :data-testid="`rename-source-${index}`" placeholder="原表名" required />
        <span class="rename-arrow">→</span>
        <input v-model="item.target_name" :data-testid="`rename-target-${index}`" placeholder="新表名" required />
        <button class="link" type="button" @click="removeItem(index)">移除</button>
      </div>
      <div class="rename-controls">
        <button class="btn" data-testid="rename-add-item" type="button" @click="addItem">增加一行</button>
        <label class="rename-field">
          <span>部分完成名称处置</span>
          <select v-model="policy" data-testid="rename-policy">
            <option value="vacant">暂空（不绑定）</option>
            <option value="compat">保留原表临时只读兼容名</option>
          </select>
        </label>
        <label class="rename-field">
          <span>本次只应用（原表名，逗号分隔）</span>
          <input v-model="applyOnly" data-testid="rename-apply-only" placeholder="留空表示全部应用" />
        </label>
        <button class="btn primary" data-testid="rename-submit" type="submit" :disabled="busy">
          {{ busy ? '提交中…' : '提交改名批次' }}
        </button>
      </div>
    </form>

    <p v-if="message" class="panel-message" data-testid="rename-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="rename-error">{{ errorMessage }}</p>

    <template v-if="latestGroup">
      <h4>当前批次 #{{ latestGroup.id }}：{{ latestGroup.status }}</h4>
      <table class="data-table" data-testid="latest-group-mappings">
        <thead>
          <tr><th>#</th><th>原表名</th><th>新表名</th><th>表身份</th><th>映射状态</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="mapping in latestGroup.mappings" :key="mapping.id" :data-testid="`mapping-${mapping.seq}`">
            <td>{{ mapping.seq }}</td>
            <td>{{ mapping.source_name }}</td>
            <td>{{ mapping.target_name }}</td>
            <td>{{ mapping.table_identity ?? '—' }}</td>
            <td>{{ mapping.status }}</td>
            <td>{{ mapping.message || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <button
        v-if="latestGroup.status !== '已完成'"
        class="btn"
        data-testid="rename-retry"
        type="button"
        :disabled="busy"
        @click="retry"
      >
        重试补齐未完成项
      </button>
    </template>

    <h4>表名解析证据</h4>
    <form class="resolve-form" @submit.prevent="resolve">
      <input v-model="resolveName" data-testid="resolve-name" placeholder="查询当前或历史表名" />
      <input v-model="resolveAsOf" data-testid="resolve-as-of" placeholder="历史时点，如 2026-06-01（可选）" />
      <button class="btn" data-testid="resolve-submit" type="submit">查询解析</button>
    </form>
    <div v-if="resolution" class="resolution" data-testid="resolution">
      <p>{{ resolution.message }}</p>
      <p v-if="resolution.resolved" class="resolution-meta">
        状态 {{ resolution.state }} · 绑定 {{ bindingLabel(resolution.binding_kind) }} ·
        {{ resolution.read_only ? '只读' : '可写' }} · 原表名 {{ resolution.initial_name }} ·
        定位映射 #{{ resolution.locating_mapping_id ?? '—' }}
      </p>
      <table v-if="resolution.field_lineage.length" class="data-table">
        <thead><tr><th>字段</th><th>源字段</th><th>目标字段</th><th>算子</th></tr></thead>
        <tbody>
          <tr v-for="line in resolution.field_lineage" :key="line.column ?? line.target_column">
            <td>{{ line.column }}</td>
            <td>{{ line.source_column }}</td>
            <td>{{ line.target_column }}</td>
            <td>{{ line.operator }}</td>
          </tr>
        </tbody>
      </table>
      <p class="page-desc">映射记录 {{ resolution.mappings.length }} 条 · 名称历史 {{ resolution.name_history.length }} 段</p>
      <ul class="history-list">
        <li v-for="history in resolution.name_history" :key="`${history.name}-${history.effective_from}`">
          {{ history.name }} · {{ bindingLabel(history.kind) }} · {{ history.table_identity }} ·
          {{ history.effective_from }} → {{ history.effective_to ?? '现在' }}
          <span v-if="history.covers_as_of">（覆盖查询时点）</span>
        </li>
      </ul>
    </div>

    <h4>字段血缘复核待办</h4>
    <table class="data-table" data-testid="review-todos">
      <thead>
        <tr><th>#</th><th>原表名</th><th>新表名</th><th>处置</th><th>结论</th><th>复核</th></tr>
      </thead>
      <tbody>
        <tr v-for="todo in overview.review_todos" :key="todo.id" :data-testid="`todo-${todo.id}`">
          <td>{{ todo.id }}</td>
          <td>{{ todo.source_name }}</td>
          <td>{{ todo.target_name }}</td>
          <td>{{ todo.disposition }}</td>
          <td>{{ todo.conclusion }}</td>
          <td>
            <span v-if="todo.review_status === '已复核'">已复核</span>
            <button v-else class="link" type="button" @click="confirmTodo(todo.id)">确认复核</button>
          </td>
        </tr>
        <tr v-if="!overview.review_todos.length">
          <td colspan="6" class="empty-state">暂无字段血缘复核待办</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  applyRenameGroup,
  loadRenameOverview,
  resolveTableName,
  reviewTodo,
  submitRenameBatch,
} from '@/api/backend-service'
import type {
  NameResolution,
  RenameGroupView,
  RenameItem,
  RenameOverview,
} from '@/data/types'

const overview = ref<RenameOverview>({
  tables: [],
  groups: [],
  mappings: [],
  review_todos: [],
  counts: {},
})
const items = reactive<RenameItem[]>([
  { source_name: 'A', target_name: 'B' },
  { source_name: 'B', target_name: 'C' },
])
const policy = ref('vacant')
const applyOnly = ref('')
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)
const latestGroup = ref<RenameGroupView | null>(null)
const resolveName = ref('B')
const resolveAsOf = ref('')
const resolution = ref<NameResolution | null>(null)

function addItem() {
  items.push({ source_name: '', target_name: '' })
}

function removeItem(index: number) {
  if (items.length > 1) items.splice(index, 1)
}

function bindingLabel(kind: string | null): string {
  if (kind === 'compat') return '临时只读兼容名'
  if (kind === 'official') return '正式名'
  return '无绑定'
}

function selectedNames(): string[] | undefined {
  const names = applyOnly.value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  return names.length ? names : undefined
}

async function refresh() {
  try {
    overview.value = await loadRenameOverview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '改名批次读取失败'
  }
}

async function submit() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const view = await submitRenameBatch(
      items.map((item) => ({ ...item })),
      { policy: policy.value, applyOnly: selectedNames() },
    )
    if (view.ok === false) throw new Error(view.message || '批量迁移表名提交失败')
    latestGroup.value = view
    message.value = view.message ?? ''
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量迁移表名提交失败'
  } finally {
    busy.value = false
  }
}

async function retry() {
  if (!latestGroup.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const view = await applyRenameGroup(latestGroup.value.id)
    if (view.ok === false) throw new Error(view.message || '补齐改名批次失败')
    latestGroup.value = view
    message.value = view.message ?? ''
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补齐改名批次失败'
  } finally {
    busy.value = false
  }
}

async function resolve() {
  errorMessage.value = ''
  try {
    resolution.value = await resolveTableName(resolveName.value.trim(), resolveAsOf.value.trim() || undefined)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表名解析失败'
  }
}

async function confirmTodo(todoId: number) {
  errorMessage.value = ''
  try {
    const result = await reviewTodo(todoId, '已核对字段血缘解析')
    if (!result.ok) throw new Error(result.message)
    await refresh()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核确认失败'
  }
}

onMounted(refresh)
</script>

<style scoped>
.rename-panel { margin-top: 28px; padding-top: 16px; border-top: 2px solid var(--border); }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.panel-head h3 { margin: 0; }
.panel-desc { color: var(--muted); font-size: 12px; margin: 4px 0 12px; max-width: 720px; }
.rename-form { margin: 16px 0; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.rename-form h4, .rename-panel > h4 { margin: 16px 0 8px; font-size: 14px; }
.rename-item { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.rename-item input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.rename-arrow { color: var(--muted); }
.rename-controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin-top: 8px; }
.rename-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.rename-field input, .rename-field select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.panel-message { color: #1f6feb; font-size: 13px; }
.resolve-form { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.resolve-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.resolution { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.resolution-meta { color: var(--muted); font-size: 12px; }
.history-list { font-size: 12px; color: var(--muted); padding-left: 18px; }
</style>
