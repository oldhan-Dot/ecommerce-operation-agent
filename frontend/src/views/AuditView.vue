<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Page, errorMessage } from '../api'
interface Audit {
  id: string
  actor_id: string | null
  action: string
  target_type: string
  target_id: string
  result: string
  created_at: string
  request_id: string
  changes: unknown
}
const rows = ref<Audit[]>([])
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const error = ref('')
const requestId = ref(''),
  action = ref(''),
  targetType = ref(''),
  targetId = ref('')
const labels: Record<string, string> = {
  AI_TASK_CREATED: '创建AI任务',
  AI_TASK_SUCCEEDED: 'AI生成成功',
  AI_TASK_FAILED: 'AI任务失败',
  AI_TASK_CANCELLED: '取消AI任务',
  AI_TASK_RETRIED: '重试AI任务',
  ARTIFACT_VERSION_CREATED: '创建产出修订',
  ARTIFACT_SUBMIT: '提交审核',
  ARTIFACT_WITHDRAW: '撤回审核',
  ARTIFACT_REVIEW: '审核产出',
  ARTIFACT_EXPORTED: '导出产出',
  OPS_MODEL_FAILURE_RATE: '模型失败率告警',
  OPS_QUEUE_WAIT: '排队等待告警',
  LOGIN: '登录',
  LOGOUT: '退出',
  LOGIN_FAILED: '登录失败',
  ADMIN_BOOTSTRAPPED: '初始化管理员',
  USER_CREATED: '创建账号',
  USER_UPDATED: '更新账号',
  PASSWORD_RESET: '重置密码',
  PASSWORD_CHANGED: '修改密码',
  STORE_GRANTS_UPDATED: '调整店铺授权',
  STORE_CREATED: '创建店铺',
  STORE_UPDATED: '更新店铺',
  STORE_ARCHIVED: '归档店铺',
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value) })
    for (const [key, value] of Object.entries({
      request_id: requestId.value,
      action: action.value,
      target_type: targetType.value,
      target_id: targetId.value,
    }))
      if (value) params.set(key, value)
    const data = await api<Page<Audit>>(`/audit-events?${params}`)
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)
function filter() {
  page.value = 1
  void load()
}
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>审计日志</h1>
      <p>关键操作保留关联编号与脱敏变更；应用不提供修改或删除入口。</p>
    </div>
    <el-button @click="load">刷新</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div style="display: flex; gap: 10px; margin: 16px 0; flex-wrap: wrap">
      <el-input v-model="requestId" placeholder="关联请求编号" clearable style="width: 240px" />
      <el-input v-model="action" placeholder="动作编码" clearable style="width: 180px" />
      <el-input
        v-model="targetType"
        placeholder="对象类型，如 ai_tasks"
        clearable
        style="width: 180px"
      />
      <el-input v-model="targetId" placeholder="对象编号" clearable style="width: 140px" />
      <el-button @click="filter">查询</el-button>
    </div>
    <el-table v-loading="loading" :data="rows"
      ><el-table-column type="expand"
        ><template #default="{ row }"
          ><div class="audit-detail">
            <p>关联请求：{{ row.request_id }}</p>
            <pre>{{ JSON.stringify(row.changes, null, 2) }}</pre>
          </div></template
        ></el-table-column
      ><el-table-column label="时间" min-width="180"
        ><template #default="{ row }">{{
          new Date(row.created_at).toLocaleString('zh-CN')
        }}</template></el-table-column
      ><el-table-column prop="actor_id" label="操作人 ID" /><el-table-column
        label="动作"
        min-width="130"
        ><template #default="{ row }">{{
          labels[row.action] ?? row.action
        }}</template></el-table-column
      ><el-table-column prop="target_type" label="对象类型" /><el-table-column
        prop="target_id"
        label="对象 ID"
      /><el-table-column label="结果"
        ><template #default="{ row }"
          ><el-tag
            :type="
              ['SUCCEEDED', 'SUCCESS'].includes(row.result)
                ? 'success'
                : row.result === 'WARNING'
                  ? 'warning'
                  : 'danger'
            "
            >{{
              ['SUCCEEDED', 'SUCCESS'].includes(row.result)
                ? '成功'
                : row.result === 'WARNING'
                  ? '告警'
                  : '失败'
            }}</el-tag
          ></template
        ></el-table-column
      ></el-table
    ><el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="20"
      layout="total, prev, pager, next"
      @current-change="load"
    />
  </section>
</template>
