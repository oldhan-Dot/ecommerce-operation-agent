<script setup lang="ts">
import { onMounted, reactive, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page, type Store, errorMessage, versionHeaders } from '../api'

interface Sku {
  id?: string
  code: string
  spec_name: string
  price: string
  cost: string | null
}
interface Product {
  id: string
  store_id: string
  code: string
  name: string
  category: string
  selling_points: string
  brand: string | null
  audience: string | null
  source_url: string | null
  status: 'ACTIVE' | 'ARCHIVED'
  version: number
  skus: Sku[]
}
const rows = ref<Product[]>([])
const stores = ref<Store[]>([])
const page = ref(1),
  total = ref(0),
  keyword = ref(''),
  storeId = ref(''),
  status = ref('ACTIVE')
const loading = ref(false),
  saving = ref(false),
  opened = ref(false),
  error = ref('')
const selected = ref<Product | null>(null),
  readonly = ref(false)
const blankSku = (): Sku => ({ code: '', spec_name: '默认规格', price: '0.00', cost: null })
const form = reactive({
  store_id: '',
  code: '',
  name: '',
  category: '',
  selling_points: '',
  brand: '',
  audience: '',
  source_url: '',
  skus: [blankSku()],
})
const activeStores = computed(() => stores.value.filter((s) => s.status === 'ACTIVE'))
const storeName = (id: string) => stores.value.find((s) => s.id === id)?.name ?? id
const editable = (row: Product) =>
  row.status === 'ACTIVE' && activeStores.value.some((s) => s.id === row.store_id)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value), keyword: keyword.value })
    if (storeId.value) params.set('store_id', storeId.value)
    if (status.value) params.set('status', status.value)
    const data = await api<Page<Product>>(`/products?${params}`)
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

/** 每次编辑读取最新详情，以其版本提交；发生冲突时保留用户尚未保存的输入。 */
async function edit(row?: Product, viewOnly = false) {
  try {
    const data = row ? await api<Product>(`/products/${row.id}`) : null
    selected.value = data
    readonly.value = viewOnly
    Object.assign(form, {
      store_id: data?.store_id ?? (storeId.value || activeStores.value[0]?.id || ''),
      code: data?.code ?? '',
      name: data?.name ?? '',
      category: data?.category ?? '',
      selling_points: data?.selling_points ?? '',
      brand: data?.brand ?? '',
      audience: data?.audience ?? '',
      source_url: data?.source_url ?? '',
      skus: data ? data.skus.map((s) => ({ ...s })) : [blankSku()],
    })
    opened.value = true
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
function addSku() {
  form.skus.push(blankSku())
}
function removeSku(index: number) {
  form.skus.splice(index, 1)
}

async function save() {
  const money = /^(0|[1-9]\d{0,15})(\.\d{1,2})?$/
  if (
    ![form.store_id, form.code, form.name, form.category, form.selling_points].every((x) =>
      x.trim(),
    )
  ) {
    ElMessage.warning('请填写店铺、商品编码、名称、类目和卖点')
    return
  }
  if (
    form.skus.some(
      (s) =>
        !s.code.trim() ||
        !s.spec_name.trim() ||
        !money.test(s.price) ||
        (s.cost !== null && s.cost !== '' && !money.test(s.cost)),
    )
  ) {
    ElMessage.warning('请填写 SKU 编码、规格；金额须非负、最多两位小数')
    return
  }
  saving.value = true
  try {
    await api(selected.value ? `/products/${selected.value.id}` : '/products', {
      method: selected.value ? 'PATCH' : 'POST',
      headers: selected.value ? versionHeaders(selected.value.version) : {},
      body: JSON.stringify({
        ...form,
        skus: form.skus.map((s) => ({ ...s, cost: s.cost === '' ? null : s.cost })),
      }),
    })
    opened.value = false
    ElMessage.success('商品及 SKU 已保存')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
async function archive(row: Product) {
  try {
    await ElMessageBox.confirm(
      '归档后商品仅可查看，已有 SKU 和历史资料保留。',
      `归档 ${row.name}`,
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api(`/products/${row.id}/archive`, {
      method: 'POST',
      headers: versionHeaders(row.version),
    })
    ElMessage.success('商品已归档')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
onMounted(async () => {
  try {
    // 分页获取全部授权店铺，不依赖团队规模上限或只读取第一页。
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
      <h1>商品管理</h1>
      <p>维护商品卖点与 SKU 价格，为经营分析建立准确的资料基础。</p>
    </div>
    <el-button type="primary" :disabled="!activeStores.length" @click="edit()">新建商品</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div class="toolbar">
      <el-select v-model="storeId" placeholder="全部店铺" clearable @change="search"
        ><el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name"
      /></el-select>
      <el-input
        v-model="keyword"
        placeholder="搜索商品名称或编码"
        clearable
        @keyup.enter="search"
      />
      <el-select v-model="status" placeholder="全部状态" clearable @change="search"
        ><el-option value="ACTIVE" label="正常" /><el-option value="ARCHIVED" label="已归档"
      /></el-select>
      <el-button @click="search">查询</el-button>
    </div>
    <el-table v-loading="loading" :data="rows" empty-text="暂无商品，请新建商品或调整筛选条件">
      <el-table-column prop="name" label="商品名称" min-width="180" />
      <el-table-column prop="code" label="编码" min-width="120" />
      <el-table-column label="所属店铺" min-width="140"
        ><template #default="{ row }">{{ storeName(row.store_id) }}</template></el-table-column
      >
      <el-table-column prop="category" label="类目" />
      <el-table-column label="规格数" width="80"
        ><template #default="{ row }">{{ row.skus.length }}</template></el-table-column
      >
      <el-table-column label="状态" width="100"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'info'">{{
            row.status === 'ACTIVE' ? '正常' : '已归档'
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="操作" width="190"
        ><template #default="{ row }">
          <el-button link type="primary" @click="edit(row, true)">查看</el-button>
          <el-button link type="primary" :disabled="!editable(row)" @click="edit(row)"
            >编辑</el-button
          >
          <el-button link type="danger" :disabled="!editable(row)" @click="archive(row)"
            >归档</el-button
          >
        </template></el-table-column
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
    :title="readonly ? '商品详情' : selected ? '编辑商品' : '新建商品'"
    width="860px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
  >
    <el-form label-position="top" :disabled="readonly || saving" @submit.prevent="save">
      <div class="form-grid">
        <el-form-item label="所属店铺" required
          ><el-select v-model="form.store_id" :disabled="!!selected"
            ><el-option
              v-for="s in activeStores"
              :key="s.id"
              :value="s.id"
              :label="s.name" /></el-select
          ><span v-if="readonly">{{ storeName(form.store_id) }}</span></el-form-item
        >
        <el-form-item label="商品编码（创建后固定）" required
          ><el-input v-model="form.code" maxlength="64" :disabled="!!selected"
        /></el-form-item>
        <el-form-item label="商品名称" required
          ><el-input v-model="form.name" maxlength="200"
        /></el-form-item>
        <el-form-item label="类目" required
          ><el-input v-model="form.category" maxlength="128"
        /></el-form-item>
      </div>
      <el-form-item label="卖点描述" required
        ><el-input
          v-model="form.selling_points"
          type="textarea"
          :rows="3"
          maxlength="5000"
          show-word-limit
      /></el-form-item>
      <div class="form-grid">
        <el-form-item label="品牌"><el-input v-model="form.brand" maxlength="100" /></el-form-item
        ><el-form-item label="详情来源 URL"
          ><el-input v-model="form.source_url" maxlength="2048" placeholder="https://"
        /></el-form-item>
      </div>
      <el-form-item label="目标人群"
        ><el-input v-model="form.audience" type="textarea" maxlength="5000"
      /></el-form-item>
      <h3>SKU 规格</h3>
      <p>至少保留一个规格。已有 SKU 保留编码和归属；成本为空表示未知。</p>
      <el-table :data="form.skus">
        <el-table-column label="SKU 编码" min-width="150"
          ><template #default="{ row }"
            ><el-input v-model="row.code" maxlength="64" :disabled="!!row.id" /></template
        ></el-table-column>
        <el-table-column label="规格名称" min-width="150"
          ><template #default="{ row }"
            ><el-input v-model="row.spec_name" maxlength="200" /></template
        ></el-table-column>
        <el-table-column label="售价（元）" min-width="120"
          ><template #default="{ row }"
            ><el-input v-model="row.price" inputmode="decimal" /></template
        ></el-table-column>
        <el-table-column label="成本（元，可空）" min-width="140"
          ><template #default="{ row }"
            ><el-input v-model="row.cost" inputmode="decimal" placeholder="未知" /></template
        ></el-table-column>
        <el-table-column v-if="!readonly" label="操作" width="80"
          ><template #default="{ row, $index }"
            ><el-button
              link
              type="danger"
              :disabled="!!row.id || form.skus.length <= 1"
              @click="removeSku($index)"
              >移除</el-button
            ></template
          ></el-table-column
        >
      </el-table>
      <el-button v-if="!readonly" :disabled="form.skus.length >= 100" @click="addSku"
        >添加规格</el-button
      >
    </el-form>
    <template #footer
      ><el-button :disabled="saving" @click="opened = false">{{
        readonly ? '关闭' : '取消'
      }}</el-button
      ><el-button v-if="!readonly" type="primary" :loading="saving" @click="save"
        >保存商品及规格</el-button
      ></template
    >
  </el-dialog>
</template>
