<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
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
      facts: { text: string; field: string }[]
      hypotheses: { text: string }[]
      recommendations: { text: string }[]
      missing_items: string[]
    }
  } | null
}
interface Product {
  id: string
  name: string
}
interface TaskBody {
  store_id: string
  task_type: 'PRODUCT_ANALYSIS'
  input: { product_id: string; goal: string }
}
const stores = ref<Store[]>([]),
  products = ref<Product[]>([])
const storeId = ref(''),
  productId = ref(''),
  goal = ref('')
const error = ref(''),
  listError = ref(''),
  submitting = ref(false),
  actionBusy = ref('')
const tasks = ref<Task[]>([]),
  total = ref(0),
  page = ref(1)
const selected = ref<Detail | null>(null),
  drawer = ref(false)
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
  if (submitting.value) return
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
      title="本版仅分析商品资料，未纳入经营数据。生成结果为待人工审核的草稿。"
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
        :disabled="!storeId || !productId || !goal.trim()"
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
          title="草稿 · 尚未审核；资料分析未使用经营数据，待验证判断不代表已证实的经营结论。"
          type="warning"
          :closable="false"
        />
        <h3>已录入的商品资料</h3>
        <p v-for="fact in selected.draft.content.facts" :key="fact.field">
          {{ fieldNames[fact.field] ?? fact.field }}：{{ fact.text }}
        </p>
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
