<template>
  <section class="eb-page">
    <div class="eb-head">
      <div>
        <h2>生效口径批次</h2>
        <p class="eb-sub">
          指标定义版本 → 按地区维度聚合的生效批次（准备 / 可读 / 已归档）。
          查询的全部地区必须来自同一批次；旧报告按其锚定批次复算。
        </p>
      </div>
      <div class="eb-actions">
        <button class="btn ghost" :disabled="loading" @click="resetScenario">重置中断场景</button>
        <button class="btn primary" :disabled="loading" @click="runSelfCheck">一键自检</button>
      </div>
    </div>

    <div v-if="error" class="eb-error">{{ error }}</div>

    <div class="eb-steps">
      <div class="step" :class="{ active: stage === 'interrupted', done: stepDone >= 1 }">
        <b>1</b><span>聚合中断（仅华东 22）</span>
      </div>
      <div class="step" :class="{ active: stage === 'recovered', done: stepDone >= 2 }">
        <b>2</b><span>恢复，只补待做华南</span>
      </div>
      <div class="step" :class="{ active: stage === 'switched', done: stepDone >= 3 }">
        <b>3</b><span>齐备确认可读并切换</span>
      </div>
      <div class="step" :class="{ done: report }">
        <b>4</b><span>按原批次复算旧报告</span>
      </div>
    </div>

    <div class="eb-grid">
      <article class="card">
        <header>
          <h3>当前生效查询</h3>
          <button class="btn mini" @click="loadCurrent">刷新取数</button>
        </header>
        <template v-if="current">
          <p class="batch-line">
            批次 <code>{{ current.batch_id }}</code>
            <span class="tag" :data-state="statusCode(current.status)">{{ current.status }}</span>
            <span v-if="current.caliber_version" class="muted">口径 {{ current.caliber_version }}</span>
          </p>
          <table class="kv">
            <tbody>
              <tr v-for="(value, region) in current.regions" :key="String(region)">
                <td class="region">{{ region }}</td>
                <td class="num">{{ value }}</td>
                <td class="muted small">同批次 {{ current.batch_id }}</td>
              </tr>
            </tbody>
          </table>
          <p class="total">合计：{{ current.total }}</p>
          <p v-if="current.same_batch" class="ok-text">✓ 所有地区来自同一次口径计算，无跨批次拼接</p>
        </template>
        <p v-else class="muted">暂无当前可读批次。</p>
      </article>

      <article class="card">
        <header><h3>新版批次进度（EB-V2）</h3>
          <button class="btn mini" @click="loadProgress">核对进度</button>
        </header>
        <template v-if="progress">
          <p class="batch-line">
            <code>{{ progress.batch_id }}</code>
            <span class="tag" :data-state="statusCode(progress.status)">{{ progress.status }}</span>
            <span v-if="progress.interrupted" class="tag warn">已中断</span>
          </p>
          <div class="scope">
            <div><span class="label done">已处理</span>
              <span v-for="r in progress.processed_regions" :key="'p'+r" class="chip done">{{ r }}</span>
            </div>
            <div><span class="label pending">待做</span>
              <span v-for="r in progress.pending_regions" :key="'t'+r" class="chip pending">{{ r }}</span>
              <span v-if="!progress.pending_regions?.length" class="muted small">无，各地已齐备</span>
            </div>
          </div>
          <div class="action-row">
            <button class="btn" :disabled="busy" @click="resume">恢复聚合（补华南 40）</button>
            <button class="btn primary" :disabled="busy" @click="switchBatch">最后切换生效</button>
          </div>
          <p v-if="actionMsg" class="muted small">{{ actionMsg }}</p>
        </template>
      </article>

      <article class="card wide">
        <header><h3>旧报告复算（R-OLD，锚定 EB-V1）</h3>
          <button class="btn mini" :disabled="busy" @click="recompute">按原批次复算</button>
        </header>
        <template v-if="report">
          <p class="batch-line">复算批次 <code>{{ report.batch_id }}</code>
            <span class="muted">口径 {{ report.caliber_version }}</span>
            <span class="muted small">{{ report.recomputed_at }}</span>
          </p>
          <table class="kv">
            <tbody>
              <tr v-for="(value, region) in report.regions" :key="'r'+String(region)">
                <td class="region">{{ region }}</td><td class="num">{{ value }}</td>
              </tr>
            </tbody>
          </table>
          <p class="total">合计：{{ report.total }}</p>
          <p class="ok-text">✓ 即使口径已切换，已发布旧报告仍按原批次与输入范围复算。</p>
        </template>
        <p v-else class="muted">点击“按原批次复算”查看 华东20 / 华南40。</p>
      </article>

      <article v-if="selfCheck" class="card wide">
        <header><h3>一键自检结论</h3>
          <span :class="selfCheck.all_passed ? 'ok-text' : 'bad-text'">
            {{ selfCheck.all_passed ? '全部通过' : '存在未通过项' }}
          </span>
        </header>
        <ul class="checks">
          <li v-for="(ok, name) in selfCheck.checks" :key="String(name)" :class="ok ? 'ok-text' : 'bad-text'">
            {{ ok ? '✓' : '✗' }} {{ checkLabel(String(name)) }}
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

type AnyRecord = Record<string, any>

const current = ref<AnyRecord | null>(null)
const progress = ref<AnyRecord | null>(null)
const report = ref<AnyRecord | null>(null)
const selfCheck = ref<AnyRecord | null>(null)
const error = ref('')
const actionMsg = ref('')
const loading = ref(false)
const busy = ref(false)
const stage = ref<'interrupted' | 'recovered' | 'switched'>('interrupted')
const stepDone = ref(0)

async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? '{}' : JSON.stringify(body),
  })
  return (await resp.json()) as T
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  return (await resp.json()) as T
}

function statusCode(status: unknown): string {
  const s = String(status ?? '')
  if (s.includes('可读') || s === 'readable') return 'readable'
  if (s.includes('归档') || s === 'archived') return 'archived'
  return 'preparing'
}

const CHECK_LABELS: Record<string, string> = {
  interrupted_query_serves_old_batch: '中断期查询仍读旧批次完整结果（20/40）',
  interrupted_query_never_mixes_new_east: '不会把新版华东 22 与旧版华南拼接',
  processed_scope_is_east_only: '已处理范围辨认正确：仅华东',
  pending_scope_is_south: '待做范围辨认正确：华南',
  resume_only_computes_pending: '恢复时只补算待做的华南',
  new_batch_readable_after_full_recovery: '各地齐备后新批次才转为可读',
  published_at_recorded: '实际发布时点已记录且保持稳定',
  current_still_old_before_switch: '切换前当前查询仍旧版',
  after_switch_all_regions_from_new_batch: '切换后全部地区同属新批次',
  old_batch_archived: '切换后旧批次归档',
  old_report_recomputes_on_original_batch: '旧报告按原批次复算为 20/40/60',
}
function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name
}

async function loadCurrent(): Promise<void> {
  current.value = await getJson<AnyRecord>('/api/metric-query?metric_id=M-REGION')
}

async function loadProgress(): Promise<void> {
  progress.value = await getJson<AnyRecord>('/api/effective-batches/2/progress')
  if (progress.value?.status) {
    const code = statusCode(progress.value.status)
    if (code === 'readable') {
      stage.value = 'recovered'
      stepDone.value = Math.max(stepDone.value, 2)
    }
  }
}

async function resetScenario(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    await postJson('/api/effective-batches/scenario', {})
    report.value = null
    selfCheck.value = null
    stage.value = 'interrupted'
    stepDone.value = 1
    actionMsg.value = '已重建：旧版 EB-V1（华东20/华南40）生效中；新版 EB-V2 仅华东22后中断。'
    await Promise.all([loadCurrent(), loadProgress()])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function resume(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const result = await postJson<AnyRecord>('/api/effective-batches/2/resume', {
      values: { 华南: 40 },
    })
    actionMsg.value = `本次补算：${(result.entry?.computed_regions ?? ['华南']).join('、')}；跳过已处理：${(result.entry?.skipped_regions ?? ['华东']).join('、')}`
    stepDone.value = 2
    stage.value = 'recovered'
    await Promise.all([loadProgress(), loadCurrent()])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function switchBatch(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    await postJson('/api/effective-batches/2/switch', {})
    stepDone.value = 3
    stage.value = 'switched'
    actionMsg.value = '已切换：EB-V2 成为当前生效批次，EB-V1 归档。'
    await Promise.all([loadProgress(), loadCurrent()])
  } catch (e) {
    const err = e as Error
    error.value = err.message || '切换失败：地区结果尚未齐备'
  } finally {
    busy.value = false
  }
}

async function recompute(): Promise<void> {
  busy.value = true
  try {
    report.value = await postJson<AnyRecord>('/api/reports/R-OLD/recompute', {})
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function runSelfCheck(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    selfCheck.value = await getJson<AnyRecord>('/api/effective-batches/demo/run')
    stepDone.value = 3
    stage.value = 'switched'
    await Promise.all([loadCurrent(), loadProgress()])
    report.value = await postJson<AnyRecord>('/api/reports/R-OLD/recompute', {})
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    await Promise.all([loadCurrent(), loadProgress()])
    stepDone.value = 1
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<style scoped>
.eb-page { padding: 4px 4px 32px; }
.eb-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 16px; }
.eb-head h2 { margin: 0 0 6px; }
.eb-sub { color: #667085; max-width: 760px; line-height: 1.6; margin: 0; }
.eb-actions { display: flex; gap: 8px; flex-shrink: 0; }
.eb-error { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; padding: 10px 12px; border-radius: 8px; margin-bottom: 12px; }
.eb-steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 16px; }
.step { display: flex; align-items: center; gap: 8px; background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; padding: 10px 12px; color: #667085; font-size: 13px; }
.step b { width: 22px; height: 22px; border-radius: 50%; background: #f2f4f7; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; }
.step.active { border-color: #2e90fa; color: #175cd3; }
.step.active b { background: #d1e9ff; color: #175cd3; }
.step.done b { background: #dcfae6; color: #067647; }
.eb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.card { background: #fff; border: 1px solid #e4e7ec; border-radius: 12px; padding: 16px; }
.card.wide { grid-column: 1 / -1; }
.card header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.card h3 { margin: 0; font-size: 15px; }
.batch-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 4px 0 10px; }
code { background: #f2f4f7; padding: 2px 6px; border-radius: 6px; font-size: 12px; }
.tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #f2f4f7; color: #475467; }
.tag[data-state='readable'] { background: #dcfae6; color: #067647; }
.tag[data-state='archived'] { background: #eaecf0; color: #475467; }
.tag[data-state='preparing'] { background: #fffaeb; color: #b54708; }
.tag.warn { background: #fef3f2; color: #b42318; }
.kv { width: 100%; border-collapse: collapse; }
.kv td { padding: 8px 4px; border-bottom: 1px solid #f2f4f7; }
.region { font-weight: 600; width: 90px; }
.num { font-variant-numeric: tabular-nums; font-size: 18px; font-weight: 700; width: 80px; }
.total { font-weight: 700; margin: 10px 0 4px; }
.muted { color: #98a2b3; }
.small { font-size: 12px; }
.ok-text { color: #067647; font-size: 13px; }
.bad-text { color: #b42318; font-size: 13px; }
.scope > div { margin-bottom: 10px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.label { font-size: 12px; padding: 2px 8px; border-radius: 6px; margin-right: 4px; }
.label.done { background: #dcfae6; color: #067647; }
.label.pending { background: #fffaeb; color: #b54708; }
.chip { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 1px solid #e4e7ec; }
.chip.done { background: #f6fef9; border-color: #abefc6; }
.chip.pending { background: #fffcf5; border-color: #fedf89; }
.action-row { display: flex; gap: 8px; margin-top: 12px; }
.btn { border: 1px solid #d0d5dd; background: #fff; color: #344054; border-radius: 8px; padding: 8px 14px; font-size: 13px; cursor: pointer; }
.btn:hover:not(:disabled) { border-color: #98a2b3; }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.btn.primary { background: #175cd3; border-color: #175cd3; color: #fff; }
.btn.primary:hover:not(:disabled) { background: #1849a9; }
.btn.ghost { background: #fff; }
.btn.mini { padding: 4px 10px; font-size: 12px; }
.checks { list-style: none; padding: 0; margin: 8px 0 0; display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; }
.checks li { font-size: 13px; line-height: 1.5; }
@media (max-width: 960px) {
  .eb-grid { grid-template-columns: 1fr; }
  .eb-steps { grid-template-columns: 1fr 1fr; }
  .checks { grid-template-columns: 1fr; }
}
</style>
