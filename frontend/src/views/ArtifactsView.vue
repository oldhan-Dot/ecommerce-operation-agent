<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, downloadFile, errorMessage, versionHeaders, type Page, type Store } from '../api'
import { useAuth } from '../stores/auth'
import ContentFields, { type Value } from '../components/ContentFields.vue'

interface Content {
  payload: Value
  facts: { text: string }[]
  missing_items: string[]
  metrics?: { values?: Record<string, { label: string; value: string | null; unit: string }> }
}
interface Version {
  id: string
  version_no: number
  title: string
  review_status: string
  version: number
  content: Content
  created_at: string
  review_comment: string | null
  reviewer_id: string | null
}
interface Artifact {
  id: string
  store_id: string
  author_id: string
  task_type: string
  version: number
  latest: Version
}
const auth = useAuth(),
  route = useRoute()
const kinds: Record<string, string> = {
  PRODUCT_ANALYSIS: '商品分析',
  COMPETITOR_ANALYSIS: '竞品分析',
  THEME_IDEAS: '主题创意',
  VIDEO_SCRIPT: '视频脚本',
  AD_STRATEGY: '投放建议',
  BUSINESS_REVIEW: '经营复盘',
}
const states: Record<string, string> = {
  DRAFT: '草稿',
  PENDING_REVIEW: '待审核',
  APPROVED: '已通过',
  REJECTED: '已驳回',
}
const rows = shallowRef<Artifact[]>([]),
  total = ref(0),
  page = ref(1),
  busy = ref(false),
  error = ref('')
const stores = ref<Store[]>([]),
  storeId = ref(''),
  kind = ref(''),
  status = ref(''),
  keyword = ref(''),
  authorId = ref('')
const dates = ref<[string, string] | null>(null)
const selected = shallowRef<Artifact | null>(null),
  version = shallowRef<Version | null>(null),
  versions = shallowRef<Version[]>([])
const historyPage = ref(1),
  historyTotal = ref(0),
  editing = ref(false),
  editTitle = ref(''),
  editPayload = shallowRef<Value>(null),
  reviewComment = ref('')
const owner = computed(() => selected.value?.author_id === auth.user?.id)
const isLatest = computed(() => version.value?.id === selected.value?.latest.id)
const canReview = computed(
  () =>
    !owner.value &&
    ['ADMIN', 'SUPERVISOR'].includes(auth.user?.role ?? '') &&
    isLatest.value &&
    version.value?.review_status === 'PENDING_REVIEW',
)
async function load() {
  error.value = ''
  const params = new URLSearchParams({ page: String(page.value), page_size: '20' })
  for (const [k, v] of Object.entries({
    store_id: storeId.value,
    task_type: kind.value,
    review_status: status.value,
    keyword: keyword.value,
    author_id: authorId.value,
    start_date: dates.value?.[0],
    end_date: dates.value?.[1],
  }))
    if (v) params.set(k, v)
  try {
    const data = await api<Page<Artifact>>(`/artifacts?${params}`)
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = errorMessage(e)
  }
}
function filter() {
  page.value = 1
  void load()
}
function close() {
  selected.value = null
  editing.value = false
}
async function history() {
  if (!selected.value) return
  try {
    const data = await api<Page<Version>>(
      `/artifacts/${selected.value.id}/versions?page=${historyPage.value}&page_size=20`,
    )
    versions.value = data.items
    historyTotal.value = data.total
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function open(id: string) {
  try {
    selected.value = await api<Artifact>(`/artifacts/${id}`)
    version.value = selected.value.latest
    editing.value = false
    reviewComment.value = ''
    historyPage.value = 1
    await history()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function choose(id: string) {
  try {
    version.value = await api<Version>(`/artifact-versions/${id}`)
    editing.value = false
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
function edit() {
  if (!version.value) return
  editTitle.value = version.value.title
  editPayload.value = JSON.parse(JSON.stringify(version.value.content.payload)) as Value
  editing.value = true
}
/** 所有写操作携带读取时的版本；冲突保留编辑内容，交由用户刷新后重新核对。 */
async function save() {
  if (!selected.value || !version.value) return
  busy.value = true
  try {
    await api(`/artifacts/${selected.value.id}/versions`, {
      method: 'POST',
      headers: versionHeaders(selected.value.version),
      body: JSON.stringify({
        base_version_id: version.value.id,
        title: editTitle.value,
        content: { ...version.value.content, payload: editPayload.value },
      }),
    })
    ElMessage.success('已保存为新的草稿版本')
    await open(selected.value.id)
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}
async function action(action: string, decision?: string) {
  if (!selected.value || !version.value) return
  if (decision === 'REJECT' && !reviewComment.value.trim()) {
    ElMessage.warning('请填写驳回原因')
    return
  }
  busy.value = true
  try {
    await api(`/artifact-versions/${version.value.id}/${action}`, {
      method: 'POST',
      headers: { ...versionHeaders(version.value.version), 'Idempotency-Key': crypto.randomUUID() },
      ...(decision
        ? { body: JSON.stringify({ decision, comment: reviewComment.value || null }) }
        : {}),
    })
    ElMessage.success('操作已完成')
    await open(selected.value.id)
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    busy.value = false
  }
}
async function exportFile(mode: string) {
  if (!version.value) return
  try {
    await downloadFile(
      `/artifact-versions/${version.value.id}/export?mode=${mode}`,
      `产出-v${version.value.version_no}-${mode}.md`,
    )
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function copy() {
  if (!version.value) return
  try {
    const response = await fetch(
      `/api/v1/artifact-versions/${version.value.id}/export?mode=draft`,
      { credentials: 'same-origin', signal: AbortSignal.timeout(15000) },
    )
    if (!response.ok) throw new Error('复制失败，请刷新后重试')
    await navigator.clipboard.writeText(await response.text())
    ElMessage.success('已复制带草稿标记的正文')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
onMounted(async () => {
  try {
    stores.value = (await api<Page<Store>>('/stores?page_size=100')).items
    await load()
    if (typeof route.query.id === 'string') await open(route.query.id)
  } catch (e) {
    error.value = errorMessage(e)
  }
})
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>产出与审核</h1>
      <p>沉淀每一次运营判断，明确每个版本的审核结果。</p>
    </div>
    <router-link to="/ai-tasks"><el-button type="primary">创建 AI 任务</el-button></router-link>
  </div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
  <div class="filters">
    <el-input v-model="keyword" placeholder="搜索标题" clearable style="width: 180px" />
    <el-select v-model="storeId" clearable placeholder="全部店铺" style="width: 170px"
      ><el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id"
    /></el-select>
    <el-select v-model="kind" clearable placeholder="全部类型" style="width: 145px"
      ><el-option v-for="(label, key) in kinds" :key="key" :label="label" :value="key"
    /></el-select>
    <el-select v-model="status" clearable placeholder="全部状态" style="width: 130px"
      ><el-option v-for="(label, key) in states" :key="key" :label="label" :value="key"
    /></el-select>
    <el-input
      v-if="auth.user?.role !== 'OPERATOR'"
      v-model="authorId"
      placeholder="作者编号"
      style="width: 120px"
    />
    <el-date-picker
      v-model="dates"
      type="daterange"
      value-format="YYYY-MM-DD"
      start-placeholder="创建起日"
      end-placeholder="创建止日"
    />
    <el-button @click="filter">查询</el-button>
  </div>
  <el-table
    :data="rows"
    @row-click="(row: Artifact) => open(row.id)"
    empty-text="暂无产出，请先创建 AI 任务"
  >
    <el-table-column prop="latest.title" label="标题" min-width="240" /><el-table-column
      label="类型"
      width="120"
      ><template #default="{ row }">{{ kinds[row.task_type] }}</template></el-table-column
    >
    <el-table-column label="版本" width="80"
      ><template #default="{ row }">v{{ row.latest.version_no }}</template></el-table-column
    >
    <el-table-column label="审核状态" width="110"
      ><template #default="{ row }"
        ><el-tag>{{ states[row.latest.review_status] }}</el-tag></template
      ></el-table-column
    >
    <el-table-column prop="author_id" label="作者编号" width="100" /><el-table-column
      prop="latest.created_at"
      label="保存时间（UTC）"
      min-width="190"
    />
  </el-table>
  <el-pagination
    v-model:current-page="page"
    :total="total"
    :page-size="20"
    layout="prev,pager,next,total"
    @current-change="load"
  />
  <el-drawer
    :model-value="!!selected"
    title="产出版本与审核"
    size="min(900px, 95vw)"
    @close="close"
  >
    <template v-if="selected && version">
      <el-alert
        title="AI辅助生成；来源与指标不可改写，建议需人工核实。"
        type="info"
        :closable="false"
      />
      <div class="version-bar">
        <el-select :model-value="version.id" @update:model-value="choose"
          ><el-option
            v-for="v in versions"
            :key="v.id"
            :label="`v${v.version_no} · ${states[v.review_status]}`"
            :value="v.id" /></el-select
        ><el-button @click="open(selected.id)">刷新最新版本</el-button>
      </div>
      <el-pagination
        v-if="historyTotal > 20"
        v-model:current-page="historyPage"
        :total="historyTotal"
        :page-size="20"
        layout="prev,pager,next"
        @current-change="history"
      />
      <p v-if="!isLatest">当前为历史版本；审核结果只对应这个版本。</p>
      <template v-if="editing"
        ><el-input v-model="editTitle" maxlength="200" placeholder="标题" /><ContentFields
          v-model="editPayload"
          editable
        /><el-button type="primary" :loading="busy" @click="save">保存为新草稿</el-button
        ><el-button @click="editing = false">取消编辑</el-button></template
      >
      <template v-else>
        <h2>{{ version.title }}</h2>
        <p>
          作者 #{{ selected.author_id }} · {{ kinds[selected.task_type] }} ·
          {{ states[version.review_status] }}
        </p>
        <h3>来源资料</h3>
        <p v-for="(f, i) in version.content.facts" :key="i">{{ f.text }}</p>
        <template v-if="version.content.metrics?.values"
          ><h3>服务端计算指标</h3>
          <p v-for="(m, key) in version.content.metrics.values" :key="key">
            {{ m.label }}：{{ m.value ?? 'N/A' }} {{ m.unit }}
          </p></template
        >
        <ContentFields :model-value="version.content.payload" />
        <h3>缺失与约束</h3>
        <p v-for="(m, i) in version.content.missing_items" :key="i">{{ m }}</p>
        <p v-if="version.review_comment">审核意见：{{ version.review_comment }}</p>
        <div class="actions">
          <template v-if="owner && isLatest"
            ><el-button v-if="version.review_status !== 'PENDING_REVIEW'" @click="edit"
              >创建修订</el-button
            ><el-button
              v-if="version.review_status === 'DRAFT'"
              type="primary"
              :loading="busy"
              @click="action('submit')"
              >提交审核</el-button
            ><el-button
              v-if="version.review_status === 'PENDING_REVIEW'"
              :loading="busy"
              @click="action('withdraw')"
              >撤回审核</el-button
            ></template
          >
          <el-button @click="exportFile('draft')">下载草稿</el-button
          ><el-button
            v-if="version.review_status === 'APPROVED'"
            type="success"
            @click="exportFile('official')"
            >正式导出</el-button
          ><el-button @click="copy">复制草稿正文</el-button>
        </div>
        <template v-if="canReview"
          ><h3>审核当前版本</h3>
          <el-input
            v-model="reviewComment"
            type="textarea"
            maxlength="1000"
            show-word-limit
            placeholder="审核意见，驳回时必填"
          />
          <div class="actions">
            <el-button type="success" :loading="busy" @click="action('reviews', 'APPROVE')"
              >通过</el-button
            ><el-button type="danger" :loading="busy" @click="action('reviews', 'REJECT')"
              >驳回</el-button
            >
          </div></template
        >
      </template>
    </template>
  </el-drawer>
</template>
<style scoped>
.filters,
.version-bar,
.actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin: 18px 0;
}
</style>
