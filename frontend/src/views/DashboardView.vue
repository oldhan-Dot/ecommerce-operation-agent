<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api, errorMessage, type Page, type Store } from '../api'

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])
interface Completeness {
  recorded_days: number
  expected_days: number
  missing_dates: string[]
  complete: boolean
  includes_current_day: boolean
}
interface Metric {
  value: string | null
  previous_value: string | null
  change_percent: string | null
  unit: 'money' | 'count' | 'percent' | 'ratio'
}
interface Channel {
  channel: string
  attribution_note: string | null
  previous_attribution_note: string | null
  completeness: Completeness
  previous_completeness: Completeness
  metrics: Record<string, Metric>
  warnings: string[]
}
interface Trend {
  date: string
  is_current_day: boolean
  paid_amount: string | null
  refund_amount: string | null
  paid_orders: string | null
  visitors: string | null
}
interface Dashboard {
  store: { id: string; name: string; status: string }
  period: { start_date: string; end_date: string }
  previous_period: { start_date: string; end_date: string; completeness: Completeness }
  completeness: Completeness
  metrics: Record<string, Metric>
  trend: Trend[]
  channels: Channel[]
  warnings: string[]
}
const labels: Record<string, string> = {
  paid_amount: '支付金额',
  paid_orders: '支付订单数',
  average_order_value: '客单价',
  refund_amount: '退款金额',
  net_amount: '支付减退款净额',
  visitors: '访客数（日 UV 累加）',
  orders_per_visitor: '支付订单/访客比',
  spend: '广告消耗',
  impressions: '曝光量',
  clicks: '点击量',
  ctr: 'CTR',
  cpc: 'CPC',
  attributed_amount: '归因成交金额',
  roas: 'ROAS',
}
const stores = ref<Store[]>([]),
  storeId = ref(''),
  dates = ref<[string, string] | null>(null)
const data = ref<Dashboard | null>(null),
  loading = ref(true),
  error = ref('')
const chartElement = ref<HTMLDivElement | null>(null)
const trendKey = ref<'paid_amount' | 'refund_amount' | 'paid_orders' | 'visitors'>('paid_amount')
const metricKeys = [
  'paid_amount',
  'paid_orders',
  'average_order_value',
  'refund_amount',
  'net_amount',
  'visitors',
  'orders_per_visitor',
]
const adKeys = ['spend', 'impressions', 'clicks', 'ctr', 'cpc', 'attributed_amount', 'roas']
const subtotal = computed(() => data.value && !data.value.completeness.complete)
let chart: echarts.ECharts | undefined, observer: ResizeObserver | undefined
let requestId = 0,
  alive = true

function value(metric?: Metric) {
  if (!metric || metric.value === null) return 'N/A'
  return `${metric.unit === 'money' ? '¥' : ''}${metric.value}${metric.unit === 'percent' ? '%' : ''}`
}
function completeness(item: Completeness) {
  return `${item.recorded_days}/${item.expected_days} 日${item.includes_current_day ? ' · 当日未完结' : ''}`
}
/** 图形位置使用近似数值，卡片、表格及悬浮提示均展示服务端返回的精确字符串。 */
async function draw() {
  await nextTick()
  if (!alive || !chartElement.value || !data.value) return
  if (!chart) {
    chart = echarts.init(chartElement.value)
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(chartElement.value)
  }
  const rows = data.value.trend,
    key = trendKey.value
  chart.setOption(
    {
      color: ['#087f73'],
      grid: { left: 72, right: 24, bottom: 45, top: 30 },
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const index = (params as { dataIndex: number }[])[0]?.dataIndex
          const row = index === undefined ? undefined : rows[index]
          return row
            ? `${row.date}${row.is_current_day ? ' · 当日未完结' : ''}<br/>${labels[key]}：${row[key] ?? '缺失'}`
            : ''
        },
      },
      xAxis: {
        type: 'category',
        data: rows.map((r) => r.date.slice(5)),
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: ['paid_amount', 'refund_amount'].includes(key) ? '元' : '数量',
      },
      series: [
        {
          type: 'bar',
          name: labels[key],
          data: rows.map((r) => (r[key] === null ? null : Number(r[key]))),
          barMaxWidth: 46,
          itemStyle: { borderRadius: [4, 4, 0, 0] },
        },
      ],
    },
    true,
  )
}
async function load() {
  if (!storeId.value) return
  const id = ++requestId
  loading.value = true
  error.value = ''
  data.value = null
  chart?.dispose()
  chart = undefined
  observer?.disconnect()
  const query = dates.value ? `?start_date=${dates.value[0]}&end_date=${dates.value[1]}` : ''
  try {
    const result = await api<Dashboard>(`/stores/${storeId.value}/dashboard${query}`)
    if (!alive || id !== requestId) return
    data.value = result
    await draw()
  } catch (e) {
    if (alive && id === requestId) error.value = errorMessage(e)
  } finally {
    if (alive && id === requestId) loading.value = false
  }
}
function lastDays(days: number) {
  // 明确使用上海日期，避免浏览器所在时区改变默认周期。
  const today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
  const end = new Date(`${today}T00:00:00Z`)
  end.setUTCDate(end.getUTCDate() - 1)
  const start = new Date(end)
  start.setUTCDate(start.getUTCDate() - days + 1)
  dates.value = [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)]
  void load()
}
onMounted(async () => {
  try {
    let page = 1
    while (true) {
      const result = await api<Page<Store>>(`/stores?page=${page}&page_size=100`)
      if (!alive) return
      stores.value.push(...result.items)
      if (stores.value.length >= result.total) break
      page++
    }
    storeId.value = stores.value.find((s) => s.status === 'ACTIVE')?.id ?? stores.value[0]?.id ?? ''
    await load()
  } catch (e) {
    if (alive) error.value = errorMessage(e)
  } finally {
    if (alive) loading.value = false
  }
})
onUnmounted(() => {
  alive = false
  requestId++
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div class="page-heading">
    <div>
      <h1>经营概览</h1>
      <p>查看一家店铺的经营表现，找到下一步行动。</p>
    </div>
    <router-link to="/imports"><el-button type="primary">导入经营数据</el-button></router-link>
  </div>
  <section class="surface filters">
    <el-select v-model="storeId" placeholder="选择店铺" aria-label="店铺" @change="load">
      <el-option
        v-for="s in stores"
        :key="s.id"
        :label="s.name + (s.status === 'ARCHIVED' ? '（已归档）' : '')"
        :value="s.id"
      />
    </el-select>
    <el-button @click="lastDays(7)">最近 7 日</el-button
    ><el-button @click="lastDays(30)">近 30 日</el-button>
    <el-date-picker
      v-model="dates"
      type="daterange"
      value-format="YYYY-MM-DD"
      start-placeholder="开始日期"
      end-placeholder="结束日期"
    />
    <el-button :loading="loading" :disabled="!storeId" @click="load">查询</el-button>
  </section>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <el-empty
    v-if="!stores.length && !loading && !error"
    description="暂无可访问店铺，请先创建店铺或联系管理员授权"
  />
  <div v-loading="loading" class="dashboard-content" aria-live="polite">
    <template v-if="data">
      <p class="muted">
        {{ data.store.name }} · {{ data.period.start_date }} — {{ data.period.end_date }} · CNY
      </p>
      <el-alert
        :type="subtotal ? 'warning' : 'success'"
        :closable="false"
        :title="`店铺数据 ${completeness(data.completeness)} · ${subtotal ? '已录入数据小计' : '周期数据完整'} · 来源：手工录入 / CSV 导入`"
      />
      <el-alert
        v-for="warning in data.warnings"
        :key="warning"
        :title="warning"
        type="warning"
        :closable="false"
      />
      <section class="dashboard-metrics">
        <article v-for="key in metricKeys" :key="key" class="surface metric">
          <span class="muted">{{ labels[key] }}{{ subtotal ? '（小计）' : '' }}</span>
          <strong>{{ value(data.metrics[key]) }}</strong>
          <span class="small"
            >较上期
            {{
              data.metrics[key]?.change_percent === null
                ? 'N/A'
                : `${data.metrics[key]?.change_percent}%`
            }}</span
          >
        </article>
      </section>
      <div class="dashboard-body">
        <section class="surface">
          <div class="chart-heading">
            <h2>经营趋势</h2>
            <el-select v-model="trendKey" aria-label="趋势指标" @change="draw">
              <el-option
                v-for="key in ['paid_amount', 'refund_amount', 'paid_orders', 'visitors']"
                :key="key"
                :value="key"
                :label="labels[key]"
              />
            </el-select>
          </div>
          <p class="muted small">日汇总 · 缺失日期留空，明确录入的零保留为零</p>
          <div
            ref="chartElement"
            class="trend-chart"
            role="img"
            :aria-label="`${labels[trendKey]}趋势，精确值见下方每日数据`"
          />
        </section>
        <section class="surface">
          <h2>完整性与口径</h2>
          <p>
            本期 {{ completeness(data.completeness) }}<br />上期
            {{ completeness(data.previous_period.completeness) }}
          </p>
          <p class="small muted">
            对比区间：{{ data.previous_period.start_date }} — {{ data.previous_period.end_date }}
          </p>
          <p>本期缺日：{{ data.completeness.missing_dates.join('、') || '无' }}</p>
          <p>上期缺日：{{ data.previous_period.completeness.missing_dates.join('、') || '无' }}</p>
          <p class="small muted">
            访客为每日 UV 累加；订单/访客比可超过 100%。净额是支付减退款，不等同利润。分母为零时显示
            N/A。
          </p>
        </section>
      </div>
      <section class="surface">
        <h2>投放渠道表现</h2>
        <p class="muted small">
          各渠道分别统计，归因成交金额不合并为店铺去重成交；缺日为小计，关闭该渠道正式环比。
        </p>
        <el-empty
          v-if="!data.channels.length"
          description="本期及上期均无投放记录，无法判断渠道完整性"
        />
        <article v-for="channel in data.channels" :key="channel.channel" class="channel">
          <h3>
            {{ channel.channel }}
            <el-tag :type="channel.completeness.complete ? 'success' : 'warning'"
              >{{ completeness(channel.completeness)
              }}{{ channel.completeness.complete ? '' : ' · 已录入小计' }}</el-tag
            >
          </h3>
          <p class="small muted">
            上期 {{ completeness(channel.previous_completeness) }}；本期缺日：{{
              channel.completeness.missing_dates.join('、') || '无'
            }}；上期缺日：{{ channel.previous_completeness.missing_dates.join('、') || '无' }}
          </p>
          <el-table :data="adKeys.map((key) => ({ key, ...channel.metrics[key] }))">
            <el-table-column label="指标"
              ><template #default="{ row }">{{ labels[row.key] }}</template></el-table-column
            >
            <el-table-column label="本期"
              ><template #default="{ row }">{{ value(row) }}</template></el-table-column
            >
            <el-table-column label="上期"
              ><template #default="{ row }">{{
                value({ ...row, value: row.previous_value })
              }}</template></el-table-column
            >
            <el-table-column label="环比"
              ><template #default="{ row }">{{
                row.change_percent === null ? 'N/A' : `${row.change_percent}%`
              }}</template></el-table-column
            >
          </el-table>
          <p v-for="warning in channel.warnings" :key="warning" class="small">{{ warning }}</p>
          <p class="small muted">
            本期归因口径：{{ channel.attribution_note ?? '缺失或不一致' }}；上期：{{
              channel.previous_attribution_note ?? '缺失或不一致'
            }}
          </p>
        </article>
      </section>
      <section class="surface">
        <el-collapse
          ><el-collapse-item title="每日数据与缺失日期（精确值）" name="daily">
            <el-table :data="data.trend"
              ><el-table-column prop="date" label="业务日期" />
              <el-table-column label="状态"
                ><template #default="{ row }">{{
                  row.paid_amount === null ? '缺失' : row.is_current_day ? '当日未完结' : '已录入'
                }}</template></el-table-column
              >
              <el-table-column
                v-for="key in ['paid_amount', 'refund_amount', 'paid_orders', 'visitors']"
                :key="key"
                :label="labels[key]"
                ><template #default="{ row }">{{ row[key] ?? '缺失' }}</template></el-table-column
              >
            </el-table>
          </el-collapse-item></el-collapse
        >
      </section>
    </template>
  </div>
</template>

<style scoped>
.dashboard-content {
  min-height: 120px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.filters > .el-select {
  width: 220px;
}
.dashboard-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.metric {
  margin: 0;
  overflow-wrap: anywhere;
}
.metric strong {
  display: block;
  font-size: 26px;
  margin: 20px 0;
}
.dashboard-body {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 16px;
}
.dashboard-body > section {
  min-width: 0;
}
.chart-heading {
  display: flex;
  justify-content: space-between;
  gap: 20px;
}
.chart-heading .el-select {
  width: 170px;
}
.trend-chart {
  height: 320px;
  width: 100%;
}
.channel {
  border-top: 1px solid #e6eded;
  margin-top: 16px;
  padding-top: 8px;
}
@media (max-width: 1100px) {
  .dashboard-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .dashboard-body {
    grid-template-columns: 1fr;
  }
}
</style>
