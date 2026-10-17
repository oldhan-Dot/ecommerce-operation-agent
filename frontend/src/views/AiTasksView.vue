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
  task_type: TaskType
}
type TaskType =
  | 'PRODUCT_ANALYSIS'
  | 'COMPETITOR_ANALYSIS'
  | 'THEME_IDEAS'
  | 'VIDEO_SCRIPT'
  | 'AD_STRATEGY'
  | 'BUSINESS_REVIEW'
const taskNames: Record<TaskType, string> = {
  PRODUCT_ANALYSIS: '商品分析',
  COMPETITOR_ANALYSIS: '竞品分析',
  THEME_IDEAS: '主题创意',
  VIDEO_SCRIPT: '视频脚本',
  AD_STRATEGY: '投放建议',
  BUSINESS_REVIEW: '经营复盘',
}
interface Detail extends Task {
  snapshot: {
    input_params: { goal?: string }
    data_copy: {
      product?: { name: string }
      products?: { product: { name: string } }[]
      store?: { name: string }
    }
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
      payload?: {
        ideas?: { name: string; insight: string; angle: string; direction: string; cta: string }[]
        shots?: {
          start: number
          end: number
          visual: string
          voiceover: string
          subtitle: string
          props: string[]
        }[]
        closing?: string
        comparison_rows?: {
          ref: string
          name: string
          platform: string
          spec: string | null
          price: string | null
          source_note: string
          source_url: string | null
          collected_at: string
          price_comparable: boolean
        }[]
        opportunities?: string[]
        risks?: string[]
        allocations?: { channel: string; amount: string; percent: string }[]
        tests?: string[]
        stop_rules?: string[]
        anomalies?: { metric_ref: string; reasoning: string }[]
        assumptions?: string[]
        next_actions?: string[]
      }
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
  task_type: TaskType
  input: Record<string, unknown>
}
const stores = ref<Store[]>([]),
  products = ref<Product[]>([])
const storeId = ref(''),
  productId = ref(''),
  goal = ref('')
const period = ref<[string, string] | null>(null)
const taskType = ref<TaskType>('PRODUCT_ANALYSIS'),
  productIds = ref<string[]>([])
const competitorIds = ref<string[]>([]),
  competitors = ref<{ id: string; name: string }[]>([])
const audience = ref(''),
  tone = ref(''),
  constraints = ref(''),
  theme = ref(''),
  style = ref('')
const ideaCount = ref(5),
  duration = ref(30),
  forbiddenTerms = ref<string[]>([])
const budget = ref(''),
  channels = ref<string[]>([]),
  targets = ref<string[]>([])
const historyPeriod = ref<[string, string] | null>(null)
const dimensions = ref(['price', 'selling_points'])
const multiProducts = computed(() => ['THEME_IDEAS', 'AD_STRATEGY'].includes(taskType.value))
const needsPeriod = computed(() => ['AD_STRATEGY', 'BUSINESS_REVIEW'].includes(taskType.value))
const canCreate = computed(
  () =>
    !!storeId.value &&
    validPeriod.value &&
    (!needsPeriod.value || !!period.value) &&
    (taskType.value === 'BUSINESS_REVIEW' ||
      (multiProducts.value ? productIds.value.length > 0 : !!productId.value)) &&
    (taskType.value === 'COMPETITOR_ANALYSIS'
      ? competitorIds.value.length > 0 && dimensions.value.length > 0
      : !!goal.value.trim()) &&
    (!['THEME_IDEAS', 'VIDEO_SCRIPT'].includes(taskType.value) || !!audience.value.trim()) &&
    (taskType.value !== 'AD_STRATEGY' || (Number(budget.value) > 0 && channels.value.length > 0)),
)
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
async function searchCompetitors(keyword = '') {
  if (!storeId.value) return
  try {
    const result = await api<Page<{ id: string; name: string }>>(
      `/competitors?store_id=${storeId.value}&status=ACTIVE&page_size=100&keyword=${encodeURIComponent(keyword)}`,
    )
    competitors.value = result.items
  } catch (e) {
    error.value = errorMessage(e)
  }
}
watch(storeId, () => {
  productId.value = ''
  productIds.value = []
  competitorIds.value = []
  competitors.value = []
  products.value = []
  searchProducts()
  searchCompetitors()
})
watch(taskType, () => {
  error.value = ''
  productIds.value = []
  competitorIds.value = []
  period.value = null
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
  if (submitting.value || (!pending.value && !canCreate.value)) return
  submitting.value = true
  error.value = ''
  try {
    if (!pending.value) {
      await ElMessageBox.confirm(
        '将冻结选定资料并调用模型生成草稿，执行时消耗企业额度，可能产生费用。',
        `生成${taskNames[taskType.value]}`,
      )
      pending.value = {
        key: crypto.randomUUID(),
        body: {
          store_id: storeId.value,
          task_type: taskType.value,
          input: {},
        },
      }
      const input = pending.value.body.input
      if (taskType.value !== 'BUSINESS_REVIEW') {
        if (multiProducts.value) input.product_ids = productIds.value
        else input.product_id = productId.value
      }
      if (taskType.value !== 'COMPETITOR_ANALYSIS') input.goal = goal.value.trim()
      if (taskType.value === 'COMPETITOR_ANALYSIS') {
        input.competitor_ids = competitorIds.value
        input.dimensions = dimensions.value
      }
      if (['THEME_IDEAS', 'VIDEO_SCRIPT'].includes(taskType.value))
        input.audience = audience.value.trim()
      if (taskType.value === 'THEME_IDEAS') {
        input.count = ideaCount.value
        if (tone.value) input.tone = tone.value
        if (constraints.value) input.constraints = constraints.value
      }
      if (taskType.value === 'VIDEO_SCRIPT') {
        input.duration_seconds = duration.value
        input.forbidden_terms = forbiddenTerms.value
        if (theme.value) input.theme = theme.value
        if (style.value) input.style = style.value
      }
      if (taskType.value === 'AD_STRATEGY') {
        input.budget = budget.value
        input.channels = channels.value
        input.target_metrics = targets.value
        if (audience.value) input.audience = audience.value
        if (historyPeriod.value)
          input.history_period = {
            start_date: historyPeriod.value[0],
            end_date: historyPeriod.value[1],
          }
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
        'AI_INSUFFICIENT_DATA',
        'AI_INVALID_INPUT',
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
      <h1>AI 运营工作台</h1>
      <p>冻结有权限的资料，生成可追溯、待人工审核的运营草稿。</p>
    </div>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface narrow">
    <el-radio-group v-model="taskType" :disabled="submitting || !!pending" class="task-types">
      <el-radio-button v-for="(label, kind) in taskNames" :key="kind" :value="kind">{{
        label
      }}</el-radio-button>
    </el-radio-group>
    <el-alert
      :title="
        taskType === 'BUSINESS_REVIEW'
          ? '本期和紧邻等长上期须至少各有一条店铺经营记录；数据不完整时不输出正式环比。'
          : '所选来源须属于当前店铺；结果均为待审核草稿。商品分析可选最多90天经营数据，无数据时仅作资料分析。'
      "
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
      <el-form-item
        v-if="taskType !== 'BUSINESS_REVIEW'"
        :label="multiProducts ? '商品（1–5个）' : '商品'"
        required
      >
        <el-select
          v-if="multiProducts"
          v-model="productIds"
          multiple
          :multiple-limit="5"
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
            :value="product.id"
        /></el-select>
        <el-select
          v-else
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
      <template v-if="taskType === 'COMPETITOR_ANALYSIS'">
        <el-form-item label="竞品（1–5个）" required
          ><el-select
            v-model="competitorIds"
            multiple
            :multiple-limit="5"
            filterable
            remote
            :remote-method="searchCompetitors"
            :disabled="!storeId"
            placeholder="选择有来源的竞品"
            style="width: 100%"
            ><el-option
              v-for="item in competitors"
              :key="item.id"
              :label="item.name"
              :value="item.id" /></el-select
        ></el-form-item>
        <el-form-item label="比较维度" required
          ><el-checkbox-group v-model="dimensions"
            ><el-checkbox value="price">价格</el-checkbox
            ><el-checkbox value="selling_points">卖点</el-checkbox
            ><el-checkbox value="brand">品牌</el-checkbox
            ><el-checkbox value="spec">规格</el-checkbox></el-checkbox-group
          ></el-form-item
        >
      </template>
      <el-form-item
        v-if="['PRODUCT_ANALYSIS', 'AD_STRATEGY', 'BUSINESS_REVIEW'].includes(taskType)"
        :label="
          taskType === 'AD_STRATEGY'
            ? '计划投放日期（1–90天，可含未来日）'
            : needsPeriod
              ? '经营日期（必填，最多90天）'
              : '经营日期（可选，最多90天）'
        "
        :required="needsPeriod"
      >
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
      <el-form-item v-if="taskType !== 'COMPETITOR_ANALYSIS'" label="分析目标" required
        ><el-input
          v-model="goal"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          placeholder="例如：找出商品信息表达中需要验证和改进的方向"
      /></el-form-item>
      <el-form-item
        v-if="['THEME_IDEAS', 'VIDEO_SCRIPT', 'AD_STRATEGY'].includes(taskType)"
        label="目标人群"
        :required="taskType !== 'AD_STRATEGY'"
        ><el-input v-model="audience" maxlength="2000" placeholder="明确草稿面向的人群"
      /></el-form-item>
      <template v-if="taskType === 'THEME_IDEAS'">
        <el-form-item label="创意数量" required
          ><el-input-number v-model="ideaCount" :min="1" :max="10" :precision="0"
        /></el-form-item>
        <el-form-item label="语气（可选）"
          ><el-input v-model="tone" maxlength="100"
        /></el-form-item>
        <el-form-item label="内容约束（可选）"
          ><el-input v-model="constraints" type="textarea" maxlength="5000"
        /></el-form-item>
      </template>
      <template v-if="taskType === 'VIDEO_SCRIPT'">
        <el-form-item label="总时长" required
          ><el-select v-model="duration"
            ><el-option
              v-for="seconds in [15, 30, 60]"
              :key="seconds"
              :label="`${seconds}秒`"
              :value="seconds" /></el-select
        ></el-form-item>
        <el-form-item label="主题（可选）"
          ><el-input v-model="theme" maxlength="100"
        /></el-form-item>
        <el-form-item label="风格（可选）"
          ><el-input v-model="style" maxlength="100"
        /></el-form-item>
        <el-form-item label="禁用词（输入后回车，最多30个）"
          ><el-select
            v-model="forbiddenTerms"
            multiple
            filterable
            allow-create
            :multiple-limit="30"
            style="width: 100%"
        /></el-form-item>
      </template>
      <template v-if="taskType === 'AD_STRATEGY'">
        <el-form-item label="总预算（元）" required
          ><el-input
            v-model="budget"
            inputmode="decimal"
            placeholder="例如 1000.00，最多两位小数"
            maxlength="19"
        /></el-form-item>
        <el-form-item label="渠道（1–5个，可输入新增）" required
          ><el-select
            v-model="channels"
            multiple
            filterable
            allow-create
            :multiple-limit="5"
            style="width: 100%"
            ><el-option
              v-for="channel in ['搜索', '推荐', '内容']"
              :key="channel"
              :label="channel"
              :value="channel" /></el-select
        ></el-form-item>
        <el-form-item label="历史参考期（可选，默认近7个已结束日）"
          ><el-date-picker
            v-model="historyPeriod"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="历史开始日"
            end-placeholder="历史结束日"
        /></el-form-item>
        <el-form-item label="目标指标（可选，输入后回车）"
          ><el-select
            v-model="targets"
            multiple
            filterable
            allow-create
            :multiple-limit="10"
            style="width: 100%"
        /></el-form-item>
      </template>
      <el-button type="primary" native-type="submit" :loading="submitting" :disabled="!canCreate">{{
        taskType === 'PRODUCT_ANALYSIS' ? '生成资料分析' : `生成${taskNames[taskType]}`
      }}</el-button>
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
      <el-table-column label="类型" min-width="110"
        ><template #default="{ row }">{{
          taskNames[row.task_type as TaskType] ?? row.task_type
        }}</template></el-table-column
      >
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
      <h2>
        {{
          selected.snapshot.data_copy.product?.name ??
          (selected.snapshot.data_copy.products?.map((item) => item.product.name).join('、') ||
            selected.snapshot.data_copy.store?.name)
        }}
      </h2>
      <p>分析目标：{{ selected.snapshot.input_params.goal ?? '按选择维度核对竞品资料' }}</p>
      <p class="muted small">
        提交时来源资料版本 {{ selected.snapshot.source_refs[0]?.version }} · 剩余可重试
        {{ selected.retries_remaining }} 次
      </p>
      <el-alert
        v-if="selected.error_code"
        :title="errors[selected.error_code] ?? '任务未完成，请联系管理员检查配置与调用记录。'"
        type="error"
        :closable="false"
      />
      <template v-if="selected.draft">
        <router-link :to="`/artifacts?id=${selected.artifact_id}`"
          ><el-button type="primary">修订、审核与导出</el-button></router-link
        >
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
        <h3 v-if="selected.draft.content.facts.length">已录入的来源资料</h3>
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
        <template v-if="selected.draft.content.payload?.comparison_rows">
          <h3>竞品来源与对比</h3>
          <article v-for="row in selected.draft.content.payload.comparison_rows" :key="row.ref">
            <h4>{{ row.name }} · {{ row.platform }}</h4>
            <p>
              规格：{{ row.spec ?? '未知' }} · 价格：{{ metricText(row.price, 'money') }} ·
              {{ row.price_comparable ? '规格相同，可核对价格' : '规格/价格不可直接比较' }}
            </p>
            <p>来源：{{ row.source_note }} · 采集日期：{{ row.collected_at }}</p>
            <p v-if="row.source_url">{{ row.source_url }}</p>
          </article>
        </template>
        <template v-if="selected.draft.content.payload?.ideas">
          <h3>主题创意</h3>
          <article v-for="(idea, i) in selected.draft.content.payload.ideas" :key="i">
            <h4>{{ i + 1 }}. {{ idea.name }}</h4>
            <p>洞察假设：{{ idea.insight }}</p>
            <p>切入角度：{{ idea.angle }}</p>
            <p>内容方向：{{ idea.direction }}</p>
            <p>行动引导：{{ idea.cta }}</p>
          </article>
        </template>
        <template v-if="selected.draft.content.payload?.shots">
          <h3>视频分镜脚本</h3>
          <article v-for="(shot, i) in selected.draft.content.payload.shots" :key="i">
            <h4>{{ shot.start }}–{{ shot.end }}秒</h4>
            <p>画面：{{ shot.visual }}</p>
            <p>口播：{{ shot.voiceover }}</p>
            <p>字幕：{{ shot.subtitle }}</p>
            <p>道具：{{ shot.props.join('、') || '无' }}</p>
          </article>
          <p>收尾：{{ selected.draft.content.payload.closing }}</p>
        </template>
        <template v-if="selected.draft.content.payload?.allocations">
          <h3>预算分配建议</h3>
          <el-table :data="selected.draft.content.payload.allocations"
            ><el-table-column prop="channel" label="渠道" /><el-table-column label="金额"
              ><template #default="{ row }">{{ row.amount }} 元</template></el-table-column
            ><el-table-column label="占比"
              ><template #default="{ row }">{{ row.percent }}%</template></el-table-column
            ></el-table
          >
          <h3>止损与复核规则（建议）</h3>
          <ul>
            <li v-for="item in selected.draft.content.payload.stop_rules" :key="item">
              {{ item }}
            </li>
          </ul>
        </template>
        <template v-if="selected.draft.content.payload?.anomalies"
          ><h3>待核实的经营异常</h3>
          <p v-for="(item, i) in selected.draft.content.payload.anomalies" :key="i">
            {{
              selected.draft.content.metrics?.values[item.metric_ref]?.label ?? item.metric_ref
            }}：{{ item.reasoning }}
          </p></template
        >
        <h3 v-if="selected.draft.content.hypotheses.length">待验证的判断</h3>
        <ul>
          <li v-for="(item, i) in selected.draft.content.hypotheses" :key="i">{{ item.text }}</li>
        </ul>
        <h3 v-if="selected.draft.content.recommendations.length">建议行动</h3>
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
.task-types {
  display: flex;
  flex-wrap: wrap;
  margin-bottom: 20px;
}
article {
  border-bottom: 1px solid #e8efed;
  padding-bottom: 12px;
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
