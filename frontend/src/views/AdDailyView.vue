<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Page, type Store, errorMessage, versionHeaders } from '../api'

interface AdInput {
  store_id: string
  channel: string
  business_date: string
  spend: string
  impressions: string
  clicks: string
  attributed_amount: string | null
  attribution_note: string | null
}
interface AdDaily extends AdInput {
  id: string
  version: number
  is_current_day: boolean
  warnings: string[]
}
const endpoint = '/daily-data/ad'
const rows = ref<AdDaily[]>([]),
  stores = ref<Store[]>([])
const page = ref(1),
  total = ref(0),
  storeId = ref(''),
  channel = ref(''),
  dates = ref<string[]>([])
const loading = ref(false),
  saving = ref(false),
  error = ref(''),
  opened = ref(false)
const selected = ref<AdDaily | null>(null)
const pending = ref<{ previous: AdDaily; body: AdInput } | null>(null)
const form = reactive({
  store_id: '',
  channel: '',
  business_date: '',
  spend: '',
  impressions: '',
  clicks: '',
  attributed_amount: '',
  attribution_note: '',
})
const fields = [
  { key: 'spend', label: '广告消耗（元）' },
  { key: 'impressions', label: '曝光量' },
  { key: 'clicks', label: '点击量' },
  { key: 'attributed_amount', label: '归因成交金额（元，可空）' },
] as const
const activeStores = computed(() => stores.value.filter((s) => s.status === 'ACTIVE'))
const storeName = (id: string) => stores.value.find((s) => s.id === id)?.name ?? id
const anomalous = computed(
  () =>
    /^[0-9]{1,19}$/.test(form.impressions) &&
    /^[0-9]{1,19}$/.test(form.clicks) &&
    BigInt(form.clicks) > BigInt(form.impressions),
)
const changes = computed(() => {
  if (!pending.value) return []
  const { previous, body } = pending.value
  return [...fields, { key: 'attribution_note' as const, label: '归因口径说明' }].map((f) => ({
    label: f.label,
    before: previous[f.key] ?? '未提供',
    after: body[f.key] ?? '未提供',
  }))
})
async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value), channel: channel.value.trim() })
    if (storeId.value) params.set('store_id', storeId.value)
    if (dates.value?.length === 2) {
      params.set('start_date', dates.value[0]!)
      params.set('end_date', dates.value[1]!)
    }
    const data = await api<Page<AdDaily>>(`${endpoint}?${params}`)
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
async function edit(row?: AdDaily) {
  try {
    const data = row ? await api<AdDaily>(`${endpoint}/${row.id}`) : null
    selected.value = data
    Object.assign(form, {
      store_id:
        data?.store_id ??
        activeStores.value.find((s) => s.id === storeId.value)?.id ??
        activeStores.value[0]?.id ??
        '',
      channel: data?.channel ?? '',
      business_date: data?.business_date ?? '',
      spend: data?.spend ?? '',
      impressions: data?.impressions ?? '',
      clicks: data?.clicks ?? '',
      attributed_amount: data?.attributed_amount ?? '',
      attribution_note: data?.attribution_note ?? '',
    })
    opened.value = true
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
async function saved() {
  opened.value = false
  pending.value = null
  ElMessage.success('投放日数据已保存')
  await load()
}

/** 完整业务键预查后展示差异，空归因金额明确转为 null，不能用零冒充未知。 */
async function save() {
  if (saving.value) return
  const money = /^(0|[1-9]\d{0,15})(\.\d{1,2})?$/
  const count = /^(0|[1-9]\d{0,18})$/
  if (
    !form.store_id ||
    !form.channel.trim() ||
    !form.business_date ||
    !money.test(form.spend) ||
    !count.test(form.impressions) ||
    !count.test(form.clicks) ||
    (form.attributed_amount !== '' && !money.test(form.attributed_amount))
  ) {
    ElMessage.warning('请填写店铺、渠道、日期和非负指标；金额最多两位小数，计数为整数')
    return
  }
  if (form.attributed_amount !== '' && !form.attribution_note.trim()) {
    ElMessage.warning('提供归因金额（包括零）时必须填写归因口径说明')
    return
  }
  const body: AdInput = {
    ...form,
    channel: form.channel.trim(),
    attributed_amount: form.attributed_amount === '' ? null : form.attributed_amount,
    attribution_note: form.attribution_note.trim() || null,
  }
  saving.value = true
  try {
    let previous = selected.value
    if (!previous) {
      const params = new URLSearchParams({
        store_id: body.store_id,
        channel: body.channel,
        start_date: body.business_date,
        end_date: body.business_date,
      })
      previous = (await api<Page<AdDaily>>(`${endpoint}?${params}`)).items[0] ?? null
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
/** 保留本次输入，只更新比较基准；仍需用户再次确认，不自动提交。 */
async function refreshComparison() {
  if (!pending.value || saving.value) return
  saving.value = true
  try {
    const previous = await api<AdDaily>(`${endpoint}/${pending.value.previous.id}`)
    pending.value = { previous, body: pending.value.body }
    if (selected.value) selected.value = previous
    ElMessage.success('已读取最新值，请重新核对并确认')
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
      <h1>投放日经营</h1>
      <p>人工记录各渠道历史投放数据 · 人民币 CNY · 上海业务日。</p>
      <p>不同渠道的归因成交金额不可当作去重店铺成交总额。</p>
    </div>
    <el-button type="primary" :disabled="!activeStores.length" @click="edit()"
      >录入投放数据</el-button
    >
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div class="toolbar">
      <el-select v-model="storeId" placeholder="全部店铺" clearable @change="search"
        ><el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id"
      /></el-select>
      <el-input
        v-model="channel"
        maxlength="64"
        placeholder="渠道名称（精确匹配）"
        clearable
        @keyup.enter="search"
      />
      <el-date-picker
        v-model="dates"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        @change="search"
      /><el-button @click="search">查询</el-button>
    </div>
    <el-table v-loading="loading" :data="rows" empty-text="暂无投放记录；未录入不等于零">
      <el-table-column label="店铺" min-width="140"
        ><template #default="{ row }">{{ storeName(row.store_id) }}</template></el-table-column
      >
      <el-table-column prop="channel" label="渠道" min-width="100" /><el-table-column
        prop="business_date"
        label="业务日期"
        width="120"
      />
      <el-table-column prop="spend" label="广告消耗（元）" min-width="130" /><el-table-column
        prop="impressions"
        label="曝光量"
        min-width="110"
      /><el-table-column prop="clicks" label="点击量" min-width="100" />
      <el-table-column label="归因成交（元）" min-width="130"
        ><template #default="{ row }">{{
          row.attributed_amount ?? '未提供'
        }}</template></el-table-column
      >
      <el-table-column
        prop="attribution_note"
        label="归因口径"
        min-width="180"
        show-overflow-tooltip
      />
      <el-table-column label="提示" min-width="140"
        ><template #default="{ row }"
          ><el-tag v-if="row.is_current_day" type="info">当日未完结</el-tag
          ><el-tooltip v-if="row.warnings.length" :content="row.warnings.join('；')"
            ><el-tag type="warning">数据待核对</el-tag></el-tooltip
          ></template
        ></el-table-column
      >
      <el-table-column label="操作" width="85"
        ><template #default="{ row }"
          ><el-button
            link
            type="primary"
            :disabled="!activeStores.some((s) => s.id === row.store_id)"
            @click="edit(row)"
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
    :title="selected ? '编辑投放日数据' : '录入投放日数据'"
    width="680px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
  >
    <el-alert
      title="消耗、曝光和点击必填。归因金额未知请留空；填写零元也须注明归因口径。"
      type="info"
      :closable="false"
    />
    <el-alert
      v-if="anomalous"
      title="点击量大于曝光量，请核对数据口径；保存后仍会保留异常提示。"
      type="warning"
      :closable="false"
    />
    <el-form label-position="top" :disabled="saving || !!pending" @submit.prevent="save"
      ><div class="form-grid">
        <el-form-item label="所属店铺" required
          ><el-select v-model="form.store_id" :disabled="!!selected"
            ><el-option
              v-for="s in stores"
              :key="s.id"
              :value="s.id"
              :label="s.name"
              :disabled="s.status !== 'ACTIVE'" /></el-select
        ></el-form-item>
        <el-form-item label="渠道（名称须保持一致）" required
          ><el-input
            v-model="form.channel"
            maxlength="64"
            :disabled="!!selected"
            placeholder="如巨量千川、直通车"
        /></el-form-item>
        <el-form-item label="业务日期（Asia/Shanghai）" required
          ><el-date-picker
            v-model="form.business_date"
            type="date"
            value-format="YYYY-MM-DD"
            :disabled="!!selected"
        /></el-form-item>
        <el-form-item
          v-for="f in fields"
          :key="f.key"
          :label="f.label"
          :required="f.key !== 'attributed_amount'"
          ><el-input
            v-model="form[f.key]"
            :inputmode="f.key === 'impressions' || f.key === 'clicks' ? 'numeric' : 'decimal'"
            :placeholder="f.key === 'attributed_amount' ? '未知请留空' : '已确认零值请填 0'"
        /></el-form-item>
      </div>
      <el-form-item label="归因口径说明" :required="form.attributed_amount !== ''"
        ><el-input
          v-model="form.attribution_note"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
          placeholder="注明平台归因窗口、成交定义等，如点击后 7 日支付成交；不同口径不可直接合并。" /></el-form-item
    ></el-form>
    <template #footer
      ><el-button :disabled="saving" @click="opened = false">取消</el-button
      ><el-button type="primary" :loading="saving" :disabled="!!pending" @click="save">{{
        selected ? '查看更新差异' : '保存'
      }}</el-button></template
    >
  </el-dialog>
  <el-dialog
    :model-value="!!pending"
    title="确认更新已有投放数据"
    width="680px"
    :close-on-click-modal="false"
    :close-on-press-escape="!saving"
    :show-close="!saving"
    @close="cancelOverwrite"
  >
    <p v-if="pending">
      {{ storeName(pending.body.store_id) }} · {{ pending.body.channel }} ·
      {{ pending.body.business_date }} 已有记录，请核对全部差异。
    </p>
    <el-table :data="changes"
      ><el-table-column prop="label" label="指标/口径" /><el-table-column
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
