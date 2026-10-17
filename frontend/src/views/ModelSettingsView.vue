<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage } from '../api'

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
  connection_status: 'UNTESTED'
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
onMounted(() => load())
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
    <el-form label-position="top" :disabled="saving || loading || !saved" @submit.prevent="save">
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
  </section>
</template>

<style scoped>
.model-settings .el-form {
  margin-top: 24px;
}
</style>
