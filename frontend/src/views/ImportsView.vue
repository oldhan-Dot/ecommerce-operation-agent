<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, ApiError, downloadFile, errorMessage, type Page, type Store } from '../api'
import { useAuth } from '../stores/auth'

interface Batch {
  id: string
  type: string
  store_id: string | null
  status: string
  row_count: number
  new_count: number
  overwrite_count: number
  error_count: number
  error_message: string | null
  created_at: string
  preview_expires_at: string | null
}
interface PreviewRow {
  row_no: number
  business_key: string
  action: string
  before: unknown
  after: unknown
  errors: { field: string; message: string }[]
  warnings: string[]
}
interface Detail extends Batch {
  rows: PreviewRow[]
  page: number
  page_size: number
}
const auth = useAuth()
const templates = [
  { value: 'stores', label: '店铺资料' },
  { value: 'products', label: '商品与 SKU' },
  { value: 'competitors', label: '竞品资料' },
  { value: 'store', label: '店铺日经营' },
  { value: 'product', label: '商品日经营' },
  { value: 'ad', label: '投放日经营' },
]
const allowedTypes = computed(() =>
  templates.filter((t) => t.value !== 'stores' || auth.user?.role === 'ADMIN'),
)
const statuses: Record<string, string> = {
  UPLOADED: '等待预检',
  VALIDATING: '预检中',
  PREVIEWED: '预检通过',
  INVALID: '数据有误',
  FAILED: '预检失败',
}
const actions: Record<string, string> = { NEW: '新增', OVERWRITE: '覆盖', ERROR: '错误' }
const type = ref('store'),
  storeId = ref(''),
  file = ref<File | null>(null),
  input = ref<HTMLInputElement | null>(null)
const stores = ref<Store[]>([]),
  batches = ref<Batch[]>([]),
  page = ref(1),
  total = ref(0)
const loading = ref(false),
  uploading = ref(false),
  error = ref(''),
  opened = ref(false),
  detail = ref<Detail | null>(null),
  rowPage = ref(1)
const busy = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let alive = true
const typeName = (value: string) => templates.find((t) => t.value === value)?.label ?? value
const storeName = (id: string | null) =>
  id ? (stores.value.find((s) => s.id === id)?.name ?? id) : '企业店铺资料'
function pick(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
}
async function load() {
  const data = await api<Page<Batch>>(`/imports?page=${page.value}`)
  if (!alive) return
  batches.value = data.items
  total.value = data.total
}
async function refresh() {
  if (busy.value) return
  busy.value = true
  loading.value = true
  error.value = ''
  try {
    await load()
  } catch (e) {
    if (alive) error.value = errorMessage(e)
  } finally {
    busy.value = false
    loading.value = false
  }
}
async function show(batch?: Batch) {
  const id = batch?.id ?? detail.value?.id
  if (!id) return
  if (batch) rowPage.value = 1
  try {
    const data = await api<Detail>(`/imports/${id}?page=${rowPage.value}`)
    if (alive && (batch || opened.value)) {
      detail.value = data
      opened.value = true
    }
  } catch (e) {
    ElMessage.error(errorMessage(e))
    detail.value = null
    opened.value = false
  }
}
async function downloadTemplate() {
  try {
    await downloadFile(`/import-templates/${type.value}`, `${type.value}-template.csv`)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function downloadErrors() {
  if (!detail.value) return
  try {
    await downloadFile(`/imports/${detail.value.id}/errors`, `import-${detail.value.id}-errors.csv`)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
/** 同文件先提示；只有用户明确确认后，才以 allow_duplicate 再次创建批次。 */
async function upload() {
  if (uploading.value || !file.value) return
  if (file.value.size > 10 * 1024 * 1024) {
    ElMessage.warning('文件不能超过 10 MB')
    return
  }
  if (!file.value.name.toLowerCase().endsWith('.csv')) {
    ElMessage.warning('仅支持 CSV 文件')
    return
  }
  if (type.value !== 'stores' && !storeId.value) {
    ElMessage.warning('请选择目标店铺')
    return
  }
  const form = new FormData()
  form.set('type', type.value)
  if (type.value !== 'stores') form.set('store_id', storeId.value)
  form.set('file', file.value)
  uploading.value = true
  try {
    let batch: Batch
    try {
      batch = await api<Batch>('/imports', { method: 'POST', body: form })
    } catch (e) {
      if (!(e instanceof ApiError) || e.code !== 'IMPORT_DUPLICATE_FILE') throw e
      try {
        await ElMessageBox.confirm(
          '同类同店文件已上传过。建议先查看已有批次；仍要再次上传并重新预检吗？',
          '文件重复',
          { type: 'warning' },
        )
      } catch {
        return
      }
      form.set('allow_duplicate', 'true')
      batch = await api<Batch>('/imports', { method: 'POST', body: form })
    }
    file.value = null
    if (input.value) input.value.value = ''
    page.value = 1
    ElMessage.success('已创建预检批次，尚未写入业务数据')
    await refresh()
    await show(batch)
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    uploading.value = false
  }
}
onMounted(async () => {
  try {
    let current = 1
    while (true) {
      const data = await api<Page<Store>>(`/stores?page=${current}&page_size=100`)
      if (!alive) return
      stores.value.push(...data.items)
      if (stores.value.length >= data.total) break
      current += 1
    }
    await refresh()
    timer = setInterval(async () => {
      const running =
        batches.value.some((b) => ['UPLOADED', 'VALIDATING'].includes(b.status)) ||
        (opened.value && detail.value && ['UPLOADED', 'VALIDATING'].includes(detail.value.status))
      if (!running || busy.value || !alive) return
      busy.value = true
      try {
        await load()
        if (opened.value) await show()
      } catch (e) {
        if (alive) error.value = errorMessage(e)
      } finally {
        busy.value = false
      }
    }, 3000)
  } catch (e) {
    error.value = errorMessage(e)
  }
})
onUnmounted(() => {
  alive = false
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="page-heading">
    <div>
      <h1>CSV 导入预检</h1>
      <p>下载模板 → 填写 CSV → 上传预检 → 核对新增、覆盖和错误。</p>
    </div>
    <el-button @click="refresh">刷新批次</el-button>
  </div>
  <el-alert
    title="本阶段仅提供模板与预检，不写入业务表；确认入库将在下一步开放。"
    type="info"
    :closable="false"
  />
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <h2>准备资料</h2>
    <el-form label-position="top" :disabled="uploading"
      ><div class="form-grid">
        <el-form-item label="模板类型"
          ><el-select v-model="type"
            ><el-option
              v-for="t in allowedTypes"
              :key="t.value"
              :value="t.value"
              :label="t.label" /></el-select
        ></el-form-item>
        <el-form-item v-if="type !== 'stores'" label="目标店铺" required
          ><el-select v-model="storeId"
            ><el-option
              v-for="s in stores"
              :key="s.id"
              :value="s.id"
              :label="s.name"
              :disabled="s.status !== 'ACTIVE'" /></el-select
        ></el-form-item></div
    ></el-form>
    <p>
      UTF-8（兼容 BOM），文件 ≤10 MB、数据 ≤10,000
      行。表头列名须完整保留，可调整顺序；空白不代表零。
    </p>
    <p v-if="type === 'products'">一行一个 SKU；同商品各行的商品字段须一致。SKU 编码全店唯一。</p>
    <p v-if="['competitors', 'product'].includes(type)">
      product_code 填写所选店铺内的商品编码；竞品可不关联本品。
    </p>
    <div class="actions">
      <el-button :disabled="uploading" @click="downloadTemplate">下载模板</el-button
      ><input
        ref="input"
        type="file"
        accept=".csv,text/csv"
        :disabled="uploading"
        @change="pick"
      /><el-button type="primary" :loading="uploading" :disabled="!file" @click="upload"
        >上传并预检</el-button
      >
    </div>
  </section>
  <section class="surface">
    <h2>预检批次</h2>
    <el-table v-loading="loading" :data="batches" empty-text="暂无批次，请填写模板后上传">
      <el-table-column prop="id" label="批次" width="90" /><el-table-column
        label="类型"
        min-width="125"
        ><template #default="{ row }">{{ typeName(row.type) }}</template></el-table-column
      >
      <el-table-column label="目标范围" min-width="150"
        ><template #default="{ row }">{{ storeName(row.store_id) }}</template></el-table-column
      >
      <el-table-column label="状态" min-width="120"
        ><template #default="{ row }"
          ><el-tag
            :type="
              row.status === 'PREVIEWED'
                ? 'success'
                : ['INVALID', 'FAILED'].includes(row.status)
                  ? 'danger'
                  : 'info'
            "
            >{{ statuses[row.status] ?? row.status }}</el-tag
          ></template
        ></el-table-column
      >
      <el-table-column prop="row_count" label="数据行" /><el-table-column
        prop="new_count"
        label="新增行"
      /><el-table-column prop="overwrite_count" label="覆盖行" /><el-table-column
        prop="error_count"
        label="错误行"
      />
      <el-table-column label="操作" width="100"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="show(row)">查看预检</el-button></template
        ></el-table-column
      > </el-table
    ><el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="20"
      layout="total, prev, pager, next"
      @current-change="refresh"
    />
  </section>
  <el-dialog v-model="opened" title="预检结果与逐行差异" width="90%">
    <template v-if="detail">
      <p>
        批次 {{ detail.id }} · {{ typeName(detail.type) }} ·
        {{ statuses[detail.status] ?? detail.status }}。新增/覆盖数量按 CSV
        数据行统计，商品文件一行对应一个 SKU。
      </p>
      <el-alert
        v-if="detail.error_message"
        :title="detail.error_message"
        type="error"
        :closable="false"
      />
      <el-alert
        v-if="detail.error_count"
        title="有错误时整批不能入库。请修正全部错误行后重新上传。"
        type="warning"
        :closable="false"
      />
      <p v-if="detail.status === 'PREVIEWED'">
        预检已记录目标及引用版本；该结果不是入库凭证，下一步开放确认入库后仍需复核变化。
      </p>
      <el-table :data="detail.rows" empty-text="暂无预检行；等待 Worker 处理或查看失败提示">
        <el-table-column prop="row_no" label="文件行号" width="95" /><el-table-column
          prop="business_key"
          label="业务键"
          min-width="150"
        />
        <el-table-column label="判断" width="90"
          ><template #default="{ row }">{{
            actions[row.action] ?? row.action
          }}</template></el-table-column
        >
        <el-table-column label="已有值" min-width="240"
          ><template #default="{ row }">
            <pre class="csv-cell">{{
              row.before ? JSON.stringify(row.before, null, 2) : '无已有记录'
            }}</pre>
          </template></el-table-column
        >
        <el-table-column label="本次填写（规范值）" min-width="240"
          ><template #default="{ row }">
            <pre class="csv-cell">{{ JSON.stringify(row.after, null, 2) }}</pre>
          </template></el-table-column
        >
        <el-table-column label="错误字段与原因" min-width="240"
          ><template #default="{ row }"
            ><p v-for="(e, i) in row.errors" :key="i">{{ e.field }}：{{ e.message }}</p>
            <el-tag v-for="(message, i) in row.warnings" :key="`warning-${i}`" type="warning">{{
              message
            }}</el-tag></template
          ></el-table-column
        >
      </el-table>
      <el-pagination
        v-model:current-page="rowPage"
        :total="detail.row_count"
        :page-size="20"
        layout="total, prev, pager, next"
        @current-change="show()"
      />
    </template>
    <template #footer
      ><el-button v-if="detail?.error_count" @click="downloadErrors">下载错误清单</el-button
      ><el-button @click="opened = false">关闭</el-button></template
    >
  </el-dialog>
</template>
<style scoped>
.csv-cell {
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>
