<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errorMessage } from '../api'
interface Summary {
  model_last_5_minutes: {
    completed: number
    failed: number
    failure_rate: number | null
    p95_ms: number | null
    queue_length: number
    oldest_wait_seconds: number
    running_calls: number
    tokens: number
    timeouts: number
    invalid_outputs: number
  }
  http: { sample_count: number; error_count: number; p95_ms: number | null }
  worker: { healthy: boolean; last_seen: string | null }
  imports_last_5_minutes: { completed: number; max_duration_seconds: number | null }
  pool: { checked_out: number; size: number }
  alerts: { code: string; created_at: string; request_id: string }[]
}
const data = ref<Summary | null>(null),
  error = ref(''),
  busy = ref(false)
const labels: Record<string, string> = {
  OPS_MODEL_FAILURE_RATE: '模型失败率达到阈值',
  OPS_QUEUE_WAIT: '任务排队接近超时',
}
async function load() {
  if (busy.value || document.hidden) return
  busy.value = true
  try {
    data.value = await api<Summary>('/operations/summary')
    error.value = ''
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
let timer: ReturnType<typeof setInterval>
onMounted(() => {
  void load()
  timer = setInterval(load, 10000)
  document.addEventListener('visibilitychange', load)
})
onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', load)
})
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>运行监控</h1>
      <p>最近5分钟的调用、队列与服务状态，每10秒刷新。</p>
    </div>
    <el-button :loading="busy" @click="load">刷新</el-button>
  </div>
  <el-alert v-if="error" type="error" :title="error" :closable="false" />
  <template v-if="data">
    <el-alert
      v-if="!data.worker.healthy"
      title="Worker超过30秒没有心跳或尚未启动，请检查Worker服务及数据库连接。"
      type="warning"
      :closable="false"
    />
    <div class="stats">
      <section class="surface">
        <h3>模型调用</h3>
        <p>
          完成 {{ data.model_last_5_minutes.completed }} · 失败
          {{ data.model_last_5_minutes.failed }}
        </p>
        <p>
          失败率
          {{
            data.model_last_5_minutes.failure_rate === null
              ? 'N/A'
              : (data.model_last_5_minutes.failure_rate * 100).toFixed(1) + '%'
          }}
        </p>
        <p>
          P95 {{ data.model_last_5_minutes.p95_ms ?? 'N/A' }} ms · Token
          {{ data.model_last_5_minutes.tokens }}
        </p>
        <p>
          超时 {{ data.model_last_5_minutes.timeouts }} · 输出校验失败
          {{ data.model_last_5_minutes.invalid_outputs }}
        </p>
      </section>
      <section class="surface">
        <h3>执行队列</h3>
        <p>
          等待 {{ data.model_last_5_minutes.queue_length }} · 运行调用
          {{ data.model_last_5_minutes.running_calls }}
        </p>
        <p>最长等待 {{ data.model_last_5_minutes.oldest_wait_seconds }} 秒</p>
        <p>Worker {{ data.worker.healthy ? '正常' : '未就绪' }}</p>
        <p>最后心跳 {{ data.worker.last_seen ?? '无' }}</p>
      </section>
      <section class="surface">
        <h3>API与导入</h3>
        <p>
          请求 {{ data.http.sample_count }} · 服务错误 {{ data.http.error_count }} · P95
          {{ data.http.p95_ms ?? 'N/A' }} ms
        </p>
        <p>当前API进程最近最多10000请求；多副本需汇总日志。</p>
        <p>连接池 {{ data.pool.checked_out }} / {{ data.pool.size }}</p>
        <p>
          完成导入 {{ data.imports_last_5_minutes.completed }} · 最长总耗时
          {{ data.imports_last_5_minutes.max_duration_seconds ?? 'N/A' }} 秒
        </p>
      </section>
    </div>
    <h2>运维事件</h2>
    <p>
      模型5分钟样本≥10且失败率≥20%、排队≥9分钟时记录事件；同类事件5分钟节流。外部通知渠道尚未配置。
    </p>
    <el-table :data="data.alerts" empty-text="暂无运维事件"
      ><el-table-column label="事件"
        ><template #default="{ row }">{{ labels[row.code] ?? row.code }}</template></el-table-column
      ><el-table-column prop="created_at" label="时间（UTC）" /><el-table-column
        prop="request_id"
        label="关联编号"
    /></el-table>
  </template>
</template>
<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.surface {
  padding: 20px;
}
</style>
