<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Page, type Store, errorMessage, versionHeaders } from '../api'
// 店铺与商品模式分别由页面入口创建，复用表单及覆盖确认流程。

const props = defineProps<{ kind: 'store' | 'product' }>()
const isProduct = props.kind === 'product'
const label = isProduct ? '商品' : '店铺'
const endpoint = `/daily-data/${props.kind}`
interface ProductOption {
  id: string
  name: string
  code: string
  status: string
}
interface DailyInput {
  store_id: string
  product_id?: string
  business_date: string
  paid_amount: string
  paid_orders: string
  visitors: string
  refund_amount: string
}
interface Daily extends DailyInput {
  id: string
  version: number
  is_current_day: boolean
  product_name?: string
  product_code?: string
  product_status?: string
}
const rows = ref<Daily[]>([]),
  stores = ref<Store[]>([])
const page = ref(1),
  total = ref(0),
  storeId = ref(''),
  productId = ref(''),
  dates = ref<string[]>([])
const loading = ref(false),
  saving = ref(false),
  error = ref(''),
  opened = ref(false)
const selected = ref<Daily | null>(null)
const pending = ref<{ previous: Daily; body: DailyInput } | null>(null)
const form = reactive<DailyInput>({
  store_id: '',
  ...(isProduct ? { product_id: '' } : {}),
  business_date: '',
  paid_amount: '',
  paid_orders: '',
  visitors: '',
  refund_amount: '',
})
const fields = [
  { key: 'paid_amount', label: '支付金额（元）' },
  { key: 'paid_orders', label: '支付订单数' },
  { key: 'visitors', label: '访客数' },
  { key: 'refund_amount', label: '退款金额（元）' },
] as const
const changes = computed(() =>
  fields.map((f) => ({
    label: f.label,
    before: pending.value?.previous[f.key],
    after: pending.value?.body[f.key],
  })),
)
const activeStores = computed(() => stores.value.filter((s) => s.status === 'ACTIVE'))
const storeName = (id: string) => stores.value.find((s) => s.id === id)?.name ?? id
const canEdit = (row: Daily) =>
  activeStores.value.some((s) => s.id === row.store_id) &&
  (!isProduct || row.product_status === 'ACTIVE')

/** 两组选项各自控制请求顺序，避免筛选或录入切店时旧响应污染候选商品。 */
function productSearch(getStore: () => string, activeOnly: boolean) {
  const options = ref<ProductOption[]>([]),
    busy = ref(false),
    failure = ref('')
  let request = 0
  async function search(keyword = '') {
    const sequence = ++request
    options.value = []
    failure.value = ''
    if (!getStore()) {
      busy.value = false
      return
    }
    busy.value = true
    try {
      const params = new URLSearchParams({ store_id: getStore(), keyword, page_size: '50' })
      if (activeOnly) params.set('status', 'ACTIVE')
      const data = await api<Page<ProductOption>>(`/products?${params}`)
      if (sequence === request) options.value = data.items
    } catch (e) {
      if (sequence === request) failure.value = errorMessage(e)
    } finally {
      if (sequence === request) busy.value = false
    }
  }
  return { options, busy, failure, search }
}
const {
  options: formProducts,
  busy: formProductLoading,
  failure: formProductError,
  search: searchFormProducts,
} = productSearch(() => form.store_id, true)
const {
  options: filterProducts,
  busy: filterProductLoading,
  failure: filterProductError,
  search: searchFilterProducts,
} = productSearch(() => storeId.value, false)
function storeChanged() {
  productId.value = ''
  if (isProduct) void searchFilterProducts()
  search()
}
function formStoreChanged() {
  if (!isProduct) return
  form.product_id = ''
  void searchFormProducts()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value) })
    if (storeId.value) params.set('store_id', storeId.value)
    if (isProduct && productId.value) params.set('product_id', productId.value)
    if (dates.value?.length === 2) {
      params.set('start_date', dates.value[0]!)
      params.set('end_date', dates.value[1]!)
    }
    const data = await api<Page<Daily>>(`${endpoint}?${params}`)
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
async function edit(row?: Daily) {
  try {
    const data = row ? await api<Daily>(`${endpoint}/${row.id}`) : null
    selected.value = data
    Object.assign(form, {
      store_id:
        data?.store_id ??
        activeStores.value.find((s) => s.id === storeId.value)?.id ??
        activeStores.value[0]?.id ??
        '',
      business_date: data?.business_date ?? '',
      paid_amount: data?.paid_amount ?? '',
      paid_orders: data?.paid_orders ?? '',
      visitors: data?.visitors ?? '',
      refund_amount: data?.refund_amount ?? '',
      ...(isProduct ? { product_id: data?.product_id ?? '' } : {}),
    })
    opened.value = true
    if (isProduct) await searchFormProducts()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function saved() {
  opened.value = false
  pending.value = null
  ElMessage.success(`${label}日经营数据已保存`)
  await load()
}

/** 新建前按完整业务键查询；已有记录只展示差异，待用户确认后才覆盖。 */
async function save() {
  if (saving.value) return
  const money = /^(0|[1-9]\d{0,15})(\.\d{1,2})?$/
  const count = /^(0|[1-9]\d{0,18})$/
  if (
    !form.store_id ||
    !form.business_date ||
    (isProduct && !form.product_id) ||
    !money.test(form.paid_amount) ||
    !money.test(form.refund_amount) ||
    !count.test(form.paid_orders) ||
    !count.test(form.visitors)
  ) {
    ElMessage.warning(
      `请填写店铺${isProduct ? '、商品' : ''}、日期及四项非负指标；金额最多两位小数，计数为整数`,
    )
    return
  }
  const body = { ...form }
  saving.value = true
  try {
    let previous = selected.value
    if (!previous) {
      const params = new URLSearchParams({
        store_id: body.store_id,
        start_date: body.business_date,
        end_date: body.business_date,
      })
      if (isProduct) params.set('product_id', body.product_id!)
      const data = await api<Page<Daily>>(`${endpoint}?${params}`)
      previous = data.items[0] ?? null
    }
    if (previous) {
      pending.value = { previous, body }
      return
    }
    await api(endpoint, { method: 'POST', body: JSON.stringify(body) })
    await saved()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}

/** 绑定确认时看到的版本及输入；冲突时保留输入，要求用户重新读取记录。 */
async function confirmOverwrite() {
  if (!pending.value || saving.value) return
  saving.value = true
  try {
    await api(endpoint, {
      method: 'PATCH',
      headers: versionHeaders(pending.value.previous.version),
      body: JSON.stringify({ ...pending.value.body, overwrite: true }),
    })
    await saved()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
/** 只刷新比较基准并保留填写值，不自动重试或提交覆盖。 */
async function refreshComparison() {
  if (!pending.value || saving.value) return
  saving.value = true
  try {
    const previous = await api<Daily>(`${endpoint}/${pending.value.previous.id}`)
    pending.value = { previous, body: pending.value.body }
    if (selected.value) selected.value = previous
    ElMessage.success('已读取最新值，请核对差异后重新确认')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
function cancelOverwrite() {
  pending.value = null
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
      <h1>{{ label }}日经营</h1>
      <p>人民币 CNY · 上海业务日。缺失日期不补零，退款按实际发生日记录。</p>
      <p v-if="isProduct">商品汇总与店铺汇总分别维护，不可相加作为店铺总量。</p>
    </div>
    <el-button type="primary" :disabled="!activeStores.length" @click="edit()"
      >录入日数据</el-button
    >
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div class="toolbar">
      <el-select v-model="storeId" placeholder="全部店铺" clearable @change="storeChanged"
        ><el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id"
      /></el-select>
      <el-select
        v-if="isProduct"
        v-model="productId"
        clearable
        filterable
        remote
        :remote-method="searchFilterProducts"
        :loading="filterProductLoading"
        :disabled="!storeId"
        placeholder="先选店铺，再搜索商品"
        @change="search"
      >
        <el-option
          v-for="p in filterProducts"
          :key="p.id"
          :value="p.id"
          :label="`${p.name}（${p.code}）${p.status === 'ARCHIVED' ? ' · 已归档' : ''}`"
        />
      </el-select>
      <el-date-picker
        v-model="dates"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        @change="search"
      />
      <el-button @click="search">查询</el-button>
    </div>
    <el-alert
      v-if="isProduct && filterProductError"
      :title="filterProductError"
      type="error"
      :closable="false"
    />
    <el-table v-loading="loading" :data="rows" empty-text="暂无经营记录；未录入表示缺失，不等于零">
      <el-table-column label="店铺" min-width="150"
        ><template #default="{ row }">{{ storeName(row.store_id) }}</template></el-table-column
      >
      <el-table-column prop="business_date" label="业务日期" width="120" />
      <el-table-column v-if="isProduct" prop="product_name" label="商品名称" min-width="160" />
      <el-table-column v-if="isProduct" prop="product_code" label="商品编码" min-width="120" />
      <el-table-column prop="paid_amount" label="支付金额（元）" min-width="135" />
      <el-table-column prop="paid_orders" label="支付订单数" min-width="110" />
      <el-table-column prop="visitors" label="访客数" min-width="100" />
      <el-table-column prop="refund_amount" label="退款金额（元）" min-width="135" />
      <el-table-column label="业务日状态" min-width="120"
        ><template #default="{ row }"
          ><el-tag v-if="row.is_current_day" type="warning">当日未完结</el-tag
          ><span v-else>已过业务日</span></template
        ></el-table-column
      >
      <el-table-column label="操作" width="90"
        ><template #default="{ row }"
          ><el-button link type="primary" :disabled="!canEdit(row)" @click="edit(row)"
            >编辑</el-button
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
    :title="`${selected ? '编辑' : '录入'}${label}日数据`"
    width="620px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
  >
    <el-alert
      title="四项指标均必填，确认没有发生时填 0；退款允许高于当天支付。"
      type="info"
      :closable="false"
    />
    <el-form label-position="top" :disabled="saving || !!pending" @submit.prevent="save">
      <div class="form-grid">
        <el-form-item label="所属店铺" required
          ><el-select v-model="form.store_id" :disabled="!!selected" @change="formStoreChanged"
            ><el-option
              v-for="s in stores"
              :key="s.id"
              :value="s.id"
              :label="s.name"
              :disabled="s.status !== 'ACTIVE'" /></el-select
        ></el-form-item>
        <el-form-item label="业务日期（Asia/Shanghai）" required
          ><el-date-picker
            v-model="form.business_date"
            type="date"
            value-format="YYYY-MM-DD"
            :disabled="!!selected"
        /></el-form-item>
        <el-form-item v-if="isProduct" label="同店商品（输入名称或编码搜索）" required>
          <el-select
            v-model="form.product_id"
            filterable
            remote
            :remote-method="searchFormProducts"
            :loading="formProductLoading"
            :disabled="!!selected || !form.store_id"
            placeholder="选择商品"
          >
            <el-option
              v-if="
                selected?.product_id && !formProducts.some((p) => p.id === selected?.product_id)
              "
              :value="selected.product_id"
              :label="selected.product_name ?? selected.product_id"
            />
            <el-option
              v-for="p in formProducts"
              :key="p.id"
              :value="p.id"
              :label="`${p.name}（${p.code}）`"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-for="f in fields" :key="f.key" :label="f.label" required
          ><el-input
            v-model="form[f.key]"
            :inputmode="f.key === 'paid_orders' || f.key === 'visitors' ? 'numeric' : 'decimal'"
            placeholder="必填；已确认零值请填 0"
        /></el-form-item>
      </div>
    </el-form>
    <el-alert
      v-if="isProduct && formProductError"
      :title="formProductError"
      type="error"
      :closable="false"
    />
    <template #footer
      ><el-button :disabled="saving" @click="opened = false">取消</el-button
      ><el-button type="primary" :loading="saving" :disabled="!!pending" @click="save">{{
        selected ? '查看更新差异' : '保存'
      }}</el-button></template
    >
  </el-dialog>
  <el-dialog
    :model-value="!!pending"
    title="确认更新已有日数据"
    width="620px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
    @close="cancelOverwrite"
  >
    <p v-if="pending">
      {{ storeName(pending.body.store_id) }} ·
      <span v-if="isProduct">{{ pending.previous.product_name }} · </span>
      {{ pending.body.business_date }} 已有记录。确认后更新四项指标，保留变更审计。
    </p>
    <el-table :data="changes"
      ><el-table-column prop="label" label="指标" /><el-table-column
        prop="before"
        label="已有值" /><el-table-column prop="after" label="本次填写"
    /></el-table>
    <template #footer
      ><el-button :disabled="saving" @click="cancelOverwrite">返回修改</el-button
      ><el-button :disabled="saving" @click="refreshComparison">读取最新值</el-button
      ><el-button type="primary" :loading="saving" @click="confirmOverwrite"
        >确认覆盖</el-button
      ></template
    >
  </el-dialog>
</template>
