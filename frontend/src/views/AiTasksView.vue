<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, ApiError, errorMessage, type Page, type Store } from '../api'

interface Task {
  id: string
  store_id: string
  status: string
  retries_remaining: number
  error_code: string | null
  queued_at: string
  artifact_id: string | null
}
interface Detail extends Task {
  snapshot: {
    input_params: { goal: string }
    data_copy: { product: { name: string } }
    source_refs: { version: number }[]
    missing_items: string[]
  }
  draft: {
    title: string
    review_status: string
    content: {
      analysis_mode: string
      metrics?: {
        period: { start_date: string; end_date: string }
        completeness: { recorded_days: number; expected_days: number }
        values: Record<string, Metric>
      }
      sku_facts?: {
        ref: string
        spec_name: string
        price: string
        cost: string | null
        gross_margin_percent: string | null
      }[]
      facts: { text: string; field: string }[]
      hypotheses: { text: string }[]
      recommendations: { text: string }[]
      missing_items: string[]
    }
  } | null
}
interface Metric {
  label: string
  value: string | null
  previous_value: string | null
  change_percent: string | null
  unit: string
}
interface Product {
  id: string
  name: string
}
interface TaskBody {
  store_id: string
  task_type: 'PRODUCT_ANALYSIS'
  input: { product_id: string; goal: string; start_date?: string; end_date?: string }
}
const stores = ref<Store[]>([]),
  products = ref<Product[]>([])
const storeId = ref(''),
  productId = ref(''),
  goal = ref('')
const period = ref<[string, string] | null>(null)
const validPeriod = computed(
  () =>
    !period.value ||
    (period.value.length === 2 &&
      (Date.parse(period.value[1]) - Date.parse(period.value[0])) / 86400000 >= 0 &&
      (Date.parse(period.value[1]) - Date.parse(period.value[0])) / 86400000 < 90),
)
const error = ref(''),
  listError = ref(''),
  submitting = ref(false),
  actionBusy = ref('')
const tasks = ref<Task[]>([]),
  total = ref(0),
  page = ref(1)
const selected = ref<Detail | null>(null),
  drawer = ref(false)
const metricRows = computed(() =>
  Object.entries(selected.value?.draft?.content.metrics?.values ?? {}).map(([ref, metric]) => ({
    ref,
    ...metric,
  })),
)
function metricText(value: string | null, unit: string) {
  return value === null
    ? 'N/A'
    : `${value}${unit === 'money' ? ' 元' : unit === 'percent' ? '%' : ''}`
}
const pending = ref<{ key: string; body: TaskBody } | null>(null)
const retryKeys = new Map<string, string>()
const statuses: Record<string, string> = {
  QUEUED: '等待执行',
  RUNNING: '生成中',
  SUCCEEDED: '草稿已生成',
  FAILED: '生成失败',
  CANCELLED: '已取消',
}
const errors: Record<string, string> = {
  AI_QUEUE_TIMEOUT: '排队超过十分钟，请检查 Worker 后重试。',
  AI_INTERRUPTED: '执行中断，未自动重发。手动重试可能产生新的模型费用。',
  AI_DAILY_LIMIT: '企业今日调用额度已用完。',
  AI_PERMISSION_REVOKED: '账号或店铺权限已变化，结果未保存。',
  AI_PERMISSION_OR_CONFIG_REVOKED: '账号、店铺或冻结模型配置已失效。',
  AI_OUTPUT_INVALID: '模型返回内容未通过校验，可以检查配置后重试。',
  MODEL_TIMEOUT: '模型响应超时，调用次数已消耗。',
  MODEL_PROVIDER_ERROR: '模型服务连接失败或拒绝请求，请联系管理员检查配置。',
  AI_SNAPSHOT_INVALID: '历史资料校验失败，请联系管理员。',
}
const fieldNames: Record<string, string> = {
  name: '商品名称',
  category: '商品分类',
  selling_points: '已录入卖点',
  brand: '品牌',
  audience: '已录入人群',
}
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false,
  productRequest = 0

async function searchStores(keyword = '') {
  try {
    const result = await api<Page<Store>>(
      `/stores?page_size=100&keyword=${encodeURIComponent(keyword)}`,
    )
    stores.value = result.items.filter((store) => store.status === 'ACTIVE')
  } catch (e) {
    error.value = errorMessage(e)
  }
}
async function searchProducts(keyword = '') {
  const sequence = ++productRequest
  if (!storeId.value) {
    products.value = []
    return
  }
  try {
    const result = await api<Page<Product>>(
      `/products?store_id=${storeId.value}&status=ACTIVE&page_size=100&keyword=${encodeURIComponent(keyword)}`,
    )
    if (sequence === productRequest) products.value = result.items
  } catch (e) {
    if (sequence === productRequest) error.value = errorMessage(e)
  }
}
watch(storeId, () => {
  productId.value = ''
  products.value = []
  searchProducts()
})

/** 查询失败不清空历史，也不会自动重发付费任务；离开页面停止轮询。 */
async function refresh() {
  clearTimeout(timer)
  if (disposed) return
  try {
    const result = await api<Page<Task>>(`/ai-tasks?page=${page.value}&page_size=20`)
    tasks.value = result.items
    total.value = result.total
    listError.value = ''
    if (drawer.value && selected.value)
      selected.value = await api<Detail>(`/ai-tasks/${selected.value.id}`)
  } catch (e) {
    listError.value = errorMessage(e)
  } finally {
    if (!disposed) timer = setTimeout(refresh, 5000)
  }
}
async function open(task: Task) {
  try {
    selected.value = await api<Detail>(`/ai-tasks/${task.id}`)
    drawer.value = true
  } catch (e) {
    error.value = errorMessage(e)
  }
}
async function create() {
  if (submitting.value || (!pending.value && !validPeriod.value)) return
  submitting.value = true
  error.value = ''
  try {
    if (!pending.value) {
      await ElMessageBox.confirm(
        '将冻结当前商品资料并调用模型生成草稿，执行时消耗企业额度，可能产生费用。',
        '生成商品资料分析',
      )
      pending.value = {
        key: crypto.randomUUID(),
        body: {
          store_id: storeId.value,
          task_type: 'PRODUCT_ANALYSIS',
          input: { product_id: productId.value, goal: goal.value.trim() },
        },
      }
      if (period.value) {
        pending.value.body.input.start_date = period.value[0]
        pending.value.body.input.end_date = period.value[1]
      }
    }
    const task = await api<Task>('/ai-tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': pending.value.key },
      body: JSON.stringify(pending.value.body),
    })
    pending.value = null
    ElMessage.success(`任务 ${task.id} 已接收`)
    page.value = 1
    await refresh()
    await open(task)
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = errorMessage(e)
    if (
      e instanceof ApiError &&
      [
        'MODEL_NOT_CONFIGURED',
        'AI_DAILY_LIMIT',
        'DATA_INVALID',
        'RESOURCE_NOT_FOUND',
        'STORE_ARCHIVED',
        'PRODUCT_ARCHIVED',
        'IDEMPOTENCY_CONFLICT',
        'AUTH_FORBIDDEN',
        'MODEL_HOST_NOT_ALLOWED',
        'MODEL_KEY_UNAVAILABLE',
      ].includes(e.code)
    )
      pending.value = null
  } finally {
    submitting.value = false
  }
}
async function cancel(task: Task) {
  actionBusy.value = task.id
  try {
    await ElMessageBox.confirm(
      '取消后不保存生成结果。已发出的模型调用仍会消耗次数并可能计费。',
      '取消任务',
    )
    await api(`/ai-tasks/${task.id}/cancel`, { method: 'POST' })
    await refresh()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = errorMessage(e)
  } finally {
    actionBusy.value = ''
  }
}
async function retry(task: Task) {
  actionBusy.value = task.id
  try {
    await ElMessageBox.confirm(
      '将使用原任务的商品资料和模型配置重新生成，可能再次计费。每条任务链最多重试两次。',
      '重试任务',
    )
    const key = retryKeys.get(task.id) ?? crypto.randomUUID()
    retryKeys.set(task.id, key)
    const child = await api<Task>(`/ai-tasks/${task.id}/retries`, {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
    })
    retryKeys.delete(task.id)
    page.value = 1
    await refresh()
    await open(child)
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = errorMessage(e)
  } finally {
    actionBusy.value = ''
  }
}
onMounted(() => {
  searchStores()
  refresh()
})
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>

<template>
  <div class="page-heading">
    <div>
      <h1>AI 商品分析</h1>
      <p>从已录入的商品资料出发，生成有来源、待验证的运营建议。</p>
    </div>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface narrow">
    <el-alert
      title="可选择最多90天的商品经营数据；不选日期或本期无数据时仅作资料分析。结果均为待审核草稿。"
      type="info"
      :closable="false"
    />
    <el-form label-position="top" :disabled="submitting || !!pending" @submit.prevent="create">
      <el-form-item label="店铺" required
        ><el-select
          v-model="storeId"
          filterable
          remote
          :remote-method="searchStores"
          placeholder="选择或搜索店铺"
          style="width: 100%"
          ><el-option
            v-for="store in stores"
            :key="store.id"
            :label="store.name"
            :value="store.id" /></el-select
      ></el-form-item>
      <el-form-item label="商品" required
        ><el-select
          v-model="productId"
          :disabled="!storeId"
          filterable
          remote
          :remote-method="searchProducts"
          placeholder="选择或搜索商品"
          style="width: 100%"
          ><el-option
            v-for="product in products"
            :key="product.id"
            :label="product.name"
            :value="product.id" /></el-select
      ></el-form-item>
      <el-form-item label="经营日期（可选，最多90天）">
        <el-date-picker
          v-model="period"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
        />
      </el-form-item>
      <p v-if="!validPeriod" class="muted small">请选择不超过90天的有效日期区间。</p>
      <el-form-item label="分析目标" required
        ><el-input
          v-model="goal"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          placeholder="例如：找出商品信息表达中需要验证和改进的方向"
      /></el-form-item>
      <el-button
        type="primary"
        native-type="submit"
        :loading="submitting"
        :disabled="!storeId || !productId || !goal.trim() || !validPeriod"
        >生成资料分析</el-button
      >
    </el-form>
    <div v-if="pending" class="actions">
      <el-button :loading="submitting" @click="create">确认上次提交</el-button
      ><span class="muted small">使用原请求键对账，避免网络重试重复排队。</span>
    </div>
  </section>
  <section class="surface">
    <h2>任务记录</h2>
    <p class="muted small">
      生成期间每五秒刷新；历史结果保留提交时的资料，后续修改商品不会覆盖它。
    </p>
    <el-alert v-if="listError" :title="listError" type="error" :closable="false" />
    <el-table :data="tasks" empty-text="暂无任务，选择商品开始分析">
      <el-table-column prop="id" label="任务编号" width="100" />
      <el-table-column label="状态" min-width="140"
        ><template #default="{ row }">{{
          statuses[row.status] ?? row.status
        }}</template></el-table-column
      >
      <el-table-column prop="queued_at" label="提交时间（UTC）" min-width="230" />
      <el-table-column label="操作" min-width="230"
        ><template #default="{ row }">
          <el-button text @click="open(row)">查看详情</el-button>
          <el-button
            v-if="['QUEUED', 'RUNNING'].includes(row.status)"
            text
            :disabled="!!actionBusy"
            @click="cancel(row)"
            >取消</el-button
          >
          <el-button
            v-if="row.status === 'FAILED' && row.retries_remaining > 0"
            text
            :disabled="!!actionBusy"
            @click="retry(row)"
            >重试</el-button
          >
        </template></el-table-column
      >
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :page-size="20"
      :total="total"
      layout="prev, pager, next, total"
      @current-change="refresh"
    />
  </section>
  <el-drawer v-model="drawer" title="商品分析任务" size="min(760px, 95vw)">
    <template v-if="selected">
      <p>任务 {{ selected.id }} · {{ statuses[selected.status] }}</p>
      <h2>{{ selected.snapshot.data_copy.product.name }}</h2>
      <p>分析目标：{{ selected.snapshot.input_params.goal }}</p>
      <p class="muted small">
        提交时商品资料版本 {{ selected.snapshot.source_refs[0]?.version }} · 剩余可重试
        {{ selected.retries_remaining }} 次
      </p>
      <el-alert
        v-if="selected.error_code"
        :title="errors[selected.error_code] ?? '任务未完成，请联系管理员检查配置与调用记录。'"
        type="error"
        :closable="false"
      />
      <template v-if="selected.draft">
        <h2>{{ selected.draft.title }}</h2>
        <el-alert
          :title="
            selected.draft.content.analysis_mode === 'DATA_SUPPORTED'
              ? '草稿 · 已引用商品经营数据；不完整周期仅为已录入小计，判断仍需人工核实。'
              : '草稿 · 仅作资料分析，未使用本期经营数据，不输出销量结论。'
          "
          type="warning"
          :closable="false"
        />
        <h3>已录入的商品资料</h3>
        <p v-for="fact in selected.draft.content.facts" :key="fact.field">
          {{ fieldNames[fact.field] ?? fact.field }}：{{ fact.text }}
        </p>
        <template v-if="selected.draft.content.sku_facts?.length">
          <h3>SKU 资料</h3>
          <p class="muted small">
            参考毛利率按录入价格和成本计算，未扣除投放、运费等费用，不代表周期利润率。
          </p>
          <el-table :data="selected.draft.content.sku_facts">
            <el-table-column prop="spec_name" label="规格" min-width="110" />
            <el-table-column label="价格"
              ><template #default="{ row }">{{ row.price }} 元</template></el-table-column
            >
            <el-table-column label="成本"
              ><template #default="{ row }">{{
                metricText(row.cost, 'money')
              }}</template></el-table-column
            >
            <el-table-column label="参考毛利率" min-width="110"
              ><template #default="{ row }">{{
                metricText(row.gross_margin_percent, 'percent')
              }}</template></el-table-column
            >
          </el-table>
        </template>
        <template v-if="selected.draft.content.metrics?.period">
          <h3>服务端计算的经营指标</h3>
          <p>
            {{ selected.draft.content.metrics.period.start_date }} 至
            {{ selected.draft.content.metrics.period.end_date }} · 已录入
            {{ selected.draft.content.metrics.completeness.recorded_days }}/{{
              selected.draft.content.metrics.completeness.expected_days
            }}
            天
          </p>
          <el-table :data="metricRows">
            <el-table-column prop="label" label="指标" min-width="160" />
            <el-table-column label="本期已录入值" min-width="110"
              ><template #default="{ row }">{{
                metricText(row.value, row.unit)
              }}</template></el-table-column
            >
            <el-table-column label="上期已录入值" min-width="110"
              ><template #default="{ row }">{{
                metricText(row.previous_value, row.unit)
              }}</template></el-table-column
            >
            <el-table-column label="正式环比" min-width="100"
              ><template #default="{ row }">{{
                metricText(row.change_percent, 'percent')
              }}</template></el-table-column
            >
          </el-table>
        </template>
        <h3>待验证的判断</h3>
        <ul>
          <li v-for="(item, i) in selected.draft.content.hypotheses" :key="i">{{ item.text }}</li>
        </ul>
        <h3>建议行动</h3>
        <ul>
          <li v-for="(item, i) in selected.draft.content.recommendations" :key="i">
            {{ item.text }}
          </li>
        </ul>
      </template>
      <p v-for="item in selected.snapshot.missing_items" :key="item" class="muted small">
        {{ item }}
      </p>
    </template>
  </el-drawer>
</template>

<style scoped>
.el-form {
  margin-top: 24px;
}
.el-pagination {
  margin-top: 20px;
}
.el-drawer p,
li {
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
