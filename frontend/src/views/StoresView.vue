<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page, type Store, errorMessage, versionHeaders } from '../api'
import { useAuth } from '../stores/auth'
const auth = useAuth()
const rows = ref<Store[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const loading = ref(false)
const error = ref('')
const opened = ref(false)
const saving = ref(false)
const selected = ref<Store | null>(null)
const form = reactive({
  code: '',
  name: '',
  platform: '',
  external_id: '',
  category: '',
  notes: '',
})
async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api<Page<Store>>(
      `/stores?page=${page.value}&keyword=${encodeURIComponent(keyword.value)}`,
    )
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
function edit(row?: Store) {
  selected.value = row ?? null
  Object.assign(form, {
    code: row?.code ?? '',
    name: row?.name ?? '',
    platform: row?.platform ?? '',
    external_id: row?.external_id ?? '',
    category: row?.category ?? '',
    notes: row?.notes ?? '',
  })
  opened.value = true
}
function search() {
  // 筛选条件变化后从第一页查询，避免页码超出新的结果范围。
  page.value = 1
  void load()
}
async function save() {
  saving.value = true
  try {
    await api(selected.value ? `/stores/${selected.value.id}` : '/stores', {
      method: selected.value ? 'PATCH' : 'POST',
      headers: selected.value ? versionHeaders(selected.value.version) : {},
      body: JSON.stringify(form),
    })
    opened.value = false
    ElMessage.success('店铺已保存')
    await load()
    await auth.restore()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
async function archive(row: Store) {
  try {
    await ElMessageBox.confirm(
      '归档后停止新建资料、经营数据和任务，历史记录保留。确定归档？',
      `归档 ${row.name}`,
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api(`/stores/${row.id}/archive`, { method: 'POST', headers: versionHeaders(row.version) })
    ElMessage.success('店铺已归档')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>店铺管理</h1>
      <p>统一维护店铺资料。平台标签不代表已连接电商平台。</p>
    </div>
    <el-button v-if="auth.user?.role === 'ADMIN'" type="primary" @click="edit()"
      >新建店铺</el-button
    >
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索名称或店铺编码"
        clearable
        @keyup.enter="search"
      /><el-button @click="search">查询</el-button>
    </div>
    <el-table
      v-loading="loading"
      :data="rows"
      empty-text="暂无可访问店铺，请创建店铺或联系管理员分配权限"
    >
      <el-table-column prop="name" label="店铺名称" min-width="180" /><el-table-column
        prop="code"
        label="编码"
        min-width="140"
      /><el-table-column prop="platform" label="平台" /><el-table-column
        prop="category"
        label="主营类目"
      />
      <el-table-column label="状态"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'info'">{{
            row.status === 'ACTIVE' ? '正常' : '已归档'
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column v-if="auth.user?.role === 'ADMIN'" label="操作" width="170"
        ><template #default="{ row }"
          ><el-button link type="primary" :disabled="row.status !== 'ACTIVE'" @click="edit(row)"
            >编辑</el-button
          ><el-button link type="danger" :disabled="row.status !== 'ACTIVE'" @click="archive(row)"
            >归档</el-button
          ></template
        ></el-table-column
      > </el-table
    ><el-pagination
      v-model:current-page="page"
      :total="total"
      :page-size="20"
      layout="total, prev, pager, next"
      @current-change="load"
    />
  </section>
  <el-dialog
    v-model="opened"
    :title="selected ? '编辑店铺' : '新建店铺'"
    width="560px"
    :close-on-click-modal="false"
  >
    <el-form label-position="top" @submit.prevent="save"
      ><div class="form-grid">
        <el-form-item label="店铺名称" required
          ><el-input v-model="form.name" maxlength="100" /></el-form-item
        ><el-form-item label="店铺编码" required
          ><el-input v-model="form.code" :disabled="!!selected" maxlength="64"
        /></el-form-item>
      </div>
      <div class="form-grid">
        <el-form-item label="平台" required
          ><el-input
            v-model="form.platform"
            maxlength="64"
            placeholder="如天猫、京东、抖音" /></el-form-item
        ><el-form-item label="主营类目"
          ><el-input v-model="form.category" maxlength="128"
        /></el-form-item>
      </div>
      <el-form-item label="外部店铺 ID（可选）"
        ><el-input v-model="form.external_id" maxlength="128" /></el-form-item
      ><el-form-item label="备注"
        ><el-input v-model="form.notes" type="textarea" :rows="3" maxlength="2000" /></el-form-item
    ></el-form>
    <template #footer
      ><el-button @click="opened = false">取消</el-button
      ><el-button
        type="primary"
        :loading="saving"
        :disabled="!form.name.trim() || !form.code.trim() || !form.platform.trim()"
        @click="save"
        >保存</el-button
      ></template
    >
  </el-dialog>
</template>
