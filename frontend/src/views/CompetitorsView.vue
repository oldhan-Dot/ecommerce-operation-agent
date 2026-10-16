<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page, type Store, errorMessage, versionHeaders } from '../api'

interface ProductOption {
  id: string
  name: string
  code: string
  status: string
}
interface Competitor {
  id: string
  store_id: string
  code: string
  product_id: string | null
  product_name: string | null
  name: string
  platform: string
  brand: string | null
  spec: string | null
  price: string | null
  selling_points: string | null
  excerpt: string | null
  source_note: string
  source_url: string | null
  collected_at: string
  status: 'ACTIVE' | 'ARCHIVED'
  version: number
}
const rows = ref<Competitor[]>([]),
  stores = ref<Store[]>([]),
  products = ref<ProductOption[]>([])
const page = ref(1),
  total = ref(0),
  keyword = ref(''),
  storeId = ref(''),
  status = ref('ACTIVE')
const loading = ref(false),
  saving = ref(false),
  opened = ref(false),
  readonly = ref(false)
const error = ref(''),
  productLoading = ref(false),
  productError = ref('')
const selected = ref<Competitor | null>(null)
const form = reactive({
  store_id: '',
  code: '',
  product_id: '',
  name: '',
  platform: '',
  brand: '',
  spec: '',
  price: '',
  selling_points: '',
  excerpt: '',
  source_note: '',
  source_url: '',
  collected_at: '',
})
const activeStores = computed(() => stores.value.filter((s) => s.status === 'ACTIVE'))
const storeName = (id: string) => stores.value.find((s) => s.id === id)?.name ?? id
const editable = (row: Competitor) =>
  row.status === 'ACTIVE' && activeStores.value.some((s) => s.id === row.store_id)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value), keyword: keyword.value })
    if (storeId.value) params.set('store_id', storeId.value)
    if (status.value) params.set('status', status.value)
    const data = await api<Page<Competitor>>(`/competitors?${params}`)
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
function search() {
  page.value = 1
  void load()
}

let productRequest = 0
/** 服务端搜索最多 50 条候选商品；序号防止切店/搜索时旧请求覆盖新结果。 */
async function searchProducts(query = '') {
  const sequence = ++productRequest
  products.value = []
  productError.value = ''
  if (!form.store_id) {
    productLoading.value = false
    return
  }
  productLoading.value = true
  try {
    const params = new URLSearchParams({
      store_id: form.store_id,
      status: 'ACTIVE',
      keyword: query,
      page_size: '50',
    })
    const data = await api<Page<ProductOption>>(`/products?${params}`)
    if (sequence === productRequest) products.value = data.items
  } catch (e) {
    if (sequence === productRequest) productError.value = errorMessage(e)
  } finally {
    if (sequence === productRequest) productLoading.value = false
  }
}
function storeChanged() {
  form.product_id = ''
  void searchProducts()
}
async function edit(row?: Competitor, viewOnly = false) {
  try {
    const data = row ? await api<Competitor>(`/competitors/${row.id}`) : null
    selected.value = data
    readonly.value = viewOnly
    Object.assign(form, {
      store_id:
        data?.store_id ??
        activeStores.value.find((s) => s.id === storeId.value)?.id ??
        activeStores.value[0]?.id ??
        '',
      code: data?.code ?? '',
      product_id: data?.product_id ?? '',
      name: data?.name ?? '',
      platform: data?.platform ?? '',
      brand: data?.brand ?? '',
      spec: data?.spec ?? '',
      price: data?.price ?? '',
      selling_points: data?.selling_points ?? '',
      excerpt: data?.excerpt ?? '',
      source_note: data?.source_note ?? '',
      source_url: data?.source_url ?? '',
      collected_at: data?.collected_at ?? '',
    })
    opened.value = true
    await searchProducts()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function save() {
  if (saving.value) return
  if (
    ![
      form.store_id,
      form.code,
      form.name,
      form.platform,
      form.source_note,
      form.collected_at,
    ].every((v) => v.trim())
  ) {
    ElMessage.warning('请填写店铺、编码、名称、平台、采集日期和来源说明')
    return
  }
  if (form.price !== '' && !/^(0|[1-9]\d{0,15})(\.\d{1,2})?$/.test(form.price)) {
    ElMessage.warning('价格须非负、最多两位小数；未知请留空')
    return
  }
  saving.value = true
  try {
    await api(selected.value ? `/competitors/${selected.value.id}` : '/competitors', {
      method: selected.value ? 'PATCH' : 'POST',
      headers: selected.value ? versionHeaders(selected.value.version) : {},
      body: JSON.stringify({
        ...form,
        product_id: form.product_id || null,
        price: form.price === '' ? null : form.price,
      }),
    })
    opened.value = false
    ElMessage.success('竞品资料已保存')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
async function archive(row: Competitor) {
  try {
    await ElMessageBox.confirm('归档后仅可查看，来源与历史资料保留。', `归档 ${row.name}`, {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api(`/competitors/${row.id}/archive`, {
      method: 'POST',
      headers: versionHeaders(row.version),
    })
    ElMessage.success('竞品已归档')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
onMounted(async () => {
  try {
    let current = 1
    while (true) {
      const data = await api<Page<Store>>(`/stores?page=${current}&page_size=100`)
      stores.value.push(...data.items)
      if (stores.value.length >= data.total) break
      current += 1
    }
    await load()
  } catch (e) {
    error.value = errorMessage(e)
  }
})
</script>

<template>
  <div class="page-heading">
    <div>
      <h1>竞品资料</h1>
      <p>整理人工采集资料，保留来源与时点，为后续对比分析提供依据。</p>
    </div>
    <el-button type="primary" :disabled="!activeStores.length" @click="edit()">新建竞品</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div class="toolbar">
      <el-select v-model="storeId" placeholder="全部店铺" clearable @change="search"
        ><el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name"
      /></el-select>
      <el-input
        v-model="keyword"
        placeholder="搜索竞品名称或编码"
        clearable
        @keyup.enter="search"
      />
      <el-select v-model="status" placeholder="全部状态" clearable @change="search"
        ><el-option value="ACTIVE" label="正常" /><el-option value="ARCHIVED" label="已归档"
      /></el-select>
      <el-button @click="search">查询</el-button>
    </div>
    <el-table v-loading="loading" :data="rows" empty-text="暂无竞品资料，请新建或调整筛选条件">
      <el-table-column prop="name" label="竞品名称" min-width="160" />
      <el-table-column prop="code" label="编码" min-width="110" />
      <el-table-column label="所属店铺" min-width="120"
        ><template #default="{ row }">{{ storeName(row.store_id) }}</template></el-table-column
      >
      <el-table-column prop="platform" label="平台" width="100" />
      <el-table-column label="采集价格（元）" min-width="125"
        ><template #default="{ row }">{{ row.price ?? '未提供' }}</template></el-table-column
      >
      <el-table-column prop="collected_at" label="采集日期" width="115" />
      <el-table-column label="关联本品" min-width="120"
        ><template #default="{ row }">{{ row.product_name ?? '未关联' }}</template></el-table-column
      >
      <el-table-column label="状态" width="90"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'info'">{{
            row.status === 'ACTIVE' ? '正常' : '已归档'
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="操作" width="185"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="edit(row, true)">查看</el-button
          ><el-button link type="primary" :disabled="!editable(row)" @click="edit(row)"
            >编辑</el-button
          ><el-button link type="danger" :disabled="!editable(row)" @click="archive(row)"
            >归档</el-button
          ></template
        ></el-table-column
      >
    </el-table>
    <el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="20"
      layout="total, prev, pager, next"
      @current-change="load"
    />
  </section>
  <el-dialog
    v-model="opened"
    :title="readonly ? '竞品详情' : selected ? '编辑竞品' : '新建竞品'"
    width="800px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
  >
    <el-alert
      title="资料仅代表人工采集时点。URL 只保存、不抓取；未提供的销量等信息视为未知。"
      type="info"
      :closable="false"
    />
    <el-form label-position="top" :disabled="readonly || saving" @submit.prevent="save">
      <div class="form-grid">
        <el-form-item label="所属店铺" required
          ><el-select v-model="form.store_id" :disabled="!!selected" @change="storeChanged"
            ><el-option
              v-for="s in stores"
              :key="s.id"
              :value="s.id"
              :label="s.name"
              :disabled="s.status !== 'ACTIVE'" /></el-select
        ></el-form-item>
        <el-form-item label="竞品编码（创建后固定）" required
          ><el-input v-model="form.code" maxlength="64" :disabled="!!selected"
        /></el-form-item>
        <el-form-item label="竞品名称" required
          ><el-input v-model="form.name" maxlength="200"
        /></el-form-item>
        <el-form-item label="平台" required
          ><el-input v-model="form.platform" maxlength="64"
        /></el-form-item>
        <el-form-item label="品牌"><el-input v-model="form.brand" maxlength="100" /></el-form-item>
        <el-form-item label="规格"
          ><el-input v-model="form.spec" maxlength="200" placeholder="如 500g/袋、2袋装"
        /></el-form-item>
        <el-form-item label="采集价格（元，可空）"
          ><el-input v-model="form.price" inputmode="decimal" placeholder="未知请留空"
        /></el-form-item>
        <el-form-item label="采集日期" required
          ><el-date-picker
            v-model="form.collected_at"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择实际采集日期"
        /></el-form-item>
      </div>
      <el-form-item label="关联同店本品（可选，输入名称或编码搜索）">
        <el-select
          v-model="form.product_id"
          clearable
          filterable
          remote
          :remote-method="searchProducts"
          :loading="productLoading"
          placeholder="可不关联"
        >
          <el-option
            v-if="selected?.product_id && !products.some((p) => p.id === selected?.product_id)"
            :value="selected.product_id"
            :label="selected.product_name ?? selected.product_id"
          />
          <el-option
            v-for="p in products"
            :key="p.id"
            :value="p.id"
            :label="`${p.name}（${p.code}）`"
          />
        </el-select>
      </el-form-item>
      <el-alert v-if="productError" :title="productError" type="error" :closable="false" />
      <el-form-item label="来源说明（注明价格口径与规格可比性）" required
        ><el-input
          v-model="form.source_note"
          type="textarea"
          :rows="3"
          maxlength="5000"
          show-word-limit
          placeholder="填写资料出处、采集方式；如页面标价/券后价、规格是否与本品一致，未知请注明。"
      /></el-form-item>
      <el-form-item label="来源 URL（可选）"
        ><el-input v-model="form.source_url" maxlength="2048" placeholder="https://"
      /></el-form-item>
      <el-form-item label="卖点"
        ><el-input v-model="form.selling_points" type="textarea" :rows="2" maxlength="5000"
      /></el-form-item>
      <el-form-item label="人工摘录"
        ><el-input
          v-model="form.excerpt"
          type="textarea"
          :rows="4"
          maxlength="10000"
          show-word-limit
      /></el-form-item>
    </el-form>
    <template #footer
      ><el-button :disabled="saving" @click="opened = false">{{
        readonly ? '关闭' : '取消'
      }}</el-button
      ><el-button v-if="!readonly" type="primary" :loading="saving" @click="save"
        >保存竞品资料</el-button
      ></template
    >
  </el-dialog>
</template>
