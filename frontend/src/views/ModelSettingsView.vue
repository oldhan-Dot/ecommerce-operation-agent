<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, ApiError, errorMessage } from '../api'

interface ModelConfiguration {
  configured: boolean
  id: string | null
  config_version: number
  provider: string
  endpoint: string
  model_name: string
  secret_masked: string | null
  timeout_seconds: number
  daily_limit: number | null
  connection_status: string
}
const saved = ref<ModelConfiguration | null>(null),
  loading = ref(false),
  saving = ref(false)
const error = ref(''),
  conflict = ref(false),
  apiKey = ref('')
const modelName = ref('deepseek-flash'),
  endpoint = ref('https://api.deepseek.com')
const timeout = ref(120),
  dailyLimit = ref(100)
interface ModelCall {
  call_id: string
  config_id: string
  purpose: string
  status: string
  error_code: string | null
  latency_ms: number | null
  created_at: string
}
interface Calls {
  items: ModelCall[]
  usage_date: string
  consumed: number
  reserved: number
  daily_limit: number | null
}
const calls = ref<Calls | null>(null),
  testing = ref(false),
  callsError = ref('')
const pendingKey = ref(''),
  pendingVersion = ref(0)
const activeTest = computed(() =>
  calls.value?.items.some(
    (call) => call.config_id === saved.value?.id && ['RESERVED', 'STARTED'].includes(call.status),
  ),
)
const dirty = computed(
  () =>
    !!saved.value &&
    (!!apiKey.value ||
      modelName.value !== saved.value.model_name ||
      endpoint.value !== saved.value.endpoint ||
      timeout.value !== saved.value.timeout_seconds ||
      dailyLimit.value !== (saved.value.daily_limit ?? 100)),
)
const statuses: Record<string, string> = {
  RESERVED: '等待执行',
  STARTED: '测试中',
  SUCCEEDED: '连接成功',
  FAILED: '连接失败',
  UNKNOWN: '结果不确定，未自动重试',
  RELEASED: '未发送，预留已释放',
}
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

/** 刷新只查询已有记录；网络失败不自动重新提交付费请求。 */
async function refreshCalls() {
  if (disposed) return
  clearTimeout(timer)
  try {
    calls.value = await api<Calls>('/settings/model/calls')
    callsError.value = ''
  } catch (e) {
    callsError.value = errorMessage(e)
  } finally {
    if (!disposed) timer = setTimeout(refreshCalls, 3000)
  }
}

async function testConnection() {
  if (
    !saved.value?.configured ||
    testing.value ||
    dirty.value ||
    (activeTest.value && !pendingKey.value)
  )
    return
  testing.value = true
  try {
    if (!pendingKey.value) {
      await ElMessageBox.confirm(
        '将测试已保存配置，占用企业每日 1 次调用额度，可能产生模型费用。',
        '连接测试',
      )
      pendingKey.value = crypto.randomUUID()
      pendingVersion.value = saved.value.config_version
    }
    const call = await api<ModelCall>('/settings/model/test', {
      method: 'POST',
      headers: { 'Idempotency-Key': pendingKey.value },
      body: JSON.stringify({ config_version: pendingVersion.value }),
    })
    pendingKey.value = ''
    error.value = ''
    ElMessage.success(`调用 ${call.call_id}：${statuses[call.status] ?? call.status}`)
    await refreshCalls()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = errorMessage(e)
    if (
      e instanceof ApiError &&
      [
        'IDEMPOTENCY_CONFLICT',
        'DATA_VERSION_CONFLICT',
        'AI_DAILY_LIMIT',
        'AI_CONCURRENCY_LIMIT',
        'MODEL_NOT_CONFIGURED',
        'MODEL_HOST_NOT_ALLOWED',
        'MODEL_KEY_UNAVAILABLE',
        'MODEL_ENCRYPTION_NOT_CONFIGURED',
        'AUTH_FORBIDDEN',
        'AUTH_CSRF_INVALID',
        'AUTH_SESSION_EXPIRED',
      ].includes(e.code)
    ) {
      // 明确拒绝且未接收的请求允许修正后使用新键；网络及数据库异常仍保留原键。
      pendingKey.value = ''
    }
    // 请求键在失败时保留；再次点击只查询或接收同一调用，不生成新付费请求。
  } finally {
    testing.value = false
  }
}

/** 主动重新读取版本时，保留本次未保存的参数及密钥，避免网络冲突丢失填写。 */
async function load(preserve = false) {
  loading.value = true
  error.value = ''
  try {
    const data = await api<ModelConfiguration>('/settings/model')
    saved.value = data
    if (!preserve) {
      modelName.value = data.model_name
      endpoint.value = data.endpoint
      timeout.value = data.timeout_seconds
      dailyLimit.value = data.daily_limit ?? 100
    }
    conflict.value = false
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
async function save() {
  if (!saved.value || saving.value) return
  error.value = ''
  saving.value = true
  try {
    const data = await api<ModelConfiguration>('/settings/model', {
      method: 'PUT',
      headers: { 'If-Match': `"${saved.value.config_version}"` },
      body: JSON.stringify({
        provider: 'deepseek',
        endpoint: endpoint.value,
        model_name: modelName.value,
        ...(apiKey.value ? { api_key: apiKey.value } : {}),
        timeout_seconds: timeout.value,
        daily_limit: dailyLimit.value,
      }),
    })
    saved.value = data
    apiKey.value = ''
    conflict.value = false
    ElMessage.success('配置已保存，尚未验证连接；本次保存未调用模型')
  } catch (e) {
    error.value = errorMessage(e)
    if (e && typeof e === 'object' && 'code' in e && e.code === 'DATA_VERSION_CONFLICT')
      conflict.value = true
  } finally {
    saving.value = false
  }
}
onMounted(() => {
  load()
  refreshCalls()
})
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
})
</script>

<template>
  <div class="page-heading">
    <div>
      <h1>模型配置</h1>
      <p>为运营分析与内容生成配置 DeepSeek，密钥仅管理员可更新。</p>
    </div>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section v-loading="loading" class="surface narrow model-settings">
    <p v-if="saved?.configured">
      当前配置版本 {{ saved.config_version }} · 密钥 {{ saved.secret_masked }}
    </p>
    <p v-if="saved?.configured" class="muted small">
      已保存参数：{{ saved.model_name }} · 超时 {{ saved.timeout_seconds }} 秒 · 每日上限
      {{ saved.daily_limit }} 次。重新读取版本时请与下方本次填写核对。
    </p>
    <el-alert
      title="保存配置不会发起模型调用，也不代表连接已验证。"
      type="info"
      :closable="false"
    />
    <el-form
      label-position="top"
      :disabled="saving || loading || testing || !saved"
      @submit.prevent="save"
    >
      <el-form-item label="模型服务商"><el-input model-value="DeepSeek" readonly /></el-form-item>
      <el-form-item label="官方服务地址" required
        ><el-input v-model="endpoint" maxlength="2048" placeholder="https://api.deepseek.com"
      /></el-form-item>
      <el-form-item label="模型名" required
        ><el-input v-model="modelName" maxlength="128" placeholder="填写当前账号可用的模型名"
      /></el-form-item>
      <el-form-item
        :label="saved?.configured ? '更新 API Key（留空沿用当前密钥）' : 'API Key'"
        :required="!saved?.configured"
      >
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="512"
          placeholder="输入密钥，保存后不回显原值"
        />
      </el-form-item>
      <div class="form-grid">
        <el-form-item label="单次超时（秒）" required
          ><el-input-number v-model="timeout" :min="30" :max="300" :precision="0"
        /></el-form-item>
        <el-form-item label="企业每日调用上限（次）" required
          ><el-input-number v-model="dailyLimit" :min="1" :max="2147483647" :precision="0"
        /></el-form-item>
      </div>
      <p class="muted small">
        调用上限按企业的上海自然日计算，涵盖连接测试及生成请求。调用次数不是金额预算，已发起的失败或超时请求也可能产生费用。
      </p>
      <div class="actions">
        <el-button
          native-type="submit"
          type="primary"
          :loading="saving"
          :disabled="!saved || !modelName || !endpoint || (!saved.configured && !apiKey)"
          >保存配置</el-button
        >
        <el-button v-if="conflict" :disabled="saving" @click="load(true)"
          >读取最新版本，保留本次填写</el-button
        >
      </div>
    </el-form>
    <div class="actions">
      <el-button
        type="primary"
        plain
        :loading="testing"
        :disabled="
          !saved?.configured || !calls || dirty || saving || loading || (activeTest && !pendingKey)
        "
        @click="testConnection"
        >{{ pendingKey ? '重试确认同一调用' : '测试已保存配置' }}</el-button
      >
    </div>
    <p v-if="dirty" class="muted small">请先保存本次参数，再测试连接。</p>
    <p v-if="pendingKey" class="muted small">
      上次提交未确认，再次点击会复用请求键；也可查看下方记录确认是否已接收。
    </p>
  </section>
  <section class="surface">
    <h2>连接测试与今日额度</h2>
    <p v-if="calls">
      上海自然日 {{ calls.usage_date }} · 已消耗 {{ calls.consumed }} 次 · 已预留
      {{ calls.reserved }} 次 · 每日上限 {{ calls.daily_limit ?? '未配置' }} 次
    </p>
    <p class="muted small">
      展示最近 20 次记录。失败及结果不确定的已发送调用不退回额度；等待执行需要 Worker 正常运行。
    </p>
    <el-alert v-if="callsError" :title="callsError" type="error" :closable="false" />
    <el-table :data="calls?.items ?? []" empty-text="暂无连接测试">
      <el-table-column prop="call_id" label="调用编号" width="110" />
      <el-table-column prop="config_id" label="配置编号" width="110" />
      <el-table-column label="用途" width="110"
        ><template #default="{ row }">{{
          row.purpose === 'AI_GENERATION' ? 'AI 生成' : '连接测试'
        }}</template></el-table-column
      >
      <el-table-column label="状态" min-width="190"
        ><template #default="{ row }">{{
          statuses[row.status] ?? row.status
        }}</template></el-table-column
      >
      <el-table-column prop="error_code" label="错误代码" min-width="210" />
      <el-table-column prop="latency_ms" label="耗时（毫秒）" width="130" />
      <el-table-column prop="created_at" label="创建时间（UTC）" min-width="220" />
    </el-table>
  </section>
</template>

<style scoped>
.model-settings .el-form {
  margin-top: 24px;
}
</style>
