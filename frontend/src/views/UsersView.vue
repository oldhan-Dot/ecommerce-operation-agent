<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Page, type Store, type User, errorMessage, roles, versionHeaders } from '../api'
const rows = ref<User[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')
const opened = ref(false)
const mode = ref<'create' | 'edit' | 'reset' | 'grant'>('create')
const target = ref<User | null>(null)
const saving = ref(false)
const stores = ref<Store[]>([])
const ids = ref<string[]>([])
const form = reactive({ login_name: '', password: '', role: 'OPERATOR' as User['role'] })
async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api<Page<User>>(`/users?page=${page.value}`)
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
async function open(action: typeof mode.value, row?: User) {
  mode.value = action
  target.value = row ?? null
  form.login_name = row?.login_name ?? ''
  form.role = row?.role ?? 'OPERATOR'
  form.password = ''
  if (action === 'grant') {
    try {
      const all: Store[] = []
      let current = 1,
        count = 0
      do {
        const data = await api<Page<Store>>(`/stores?page=${current}&page_size=100`)
        all.push(...data.items)
        count = data.total
        current++
      } while (all.length < count)
      stores.value = all
      ids.value = [...(row?.store_ids ?? [])]
    } catch (e) {
      ElMessage.error(errorMessage(e))
      return
    }
  }
  opened.value = true
}
async function save() {
  saving.value = true
  try {
    if (mode.value === 'create') {
      await api('/users', { method: 'POST', body: JSON.stringify(form) })
    } else if (target.value) {
      const id = target.value.id,
        headers = versionHeaders(target.value.version)
      if (mode.value === 'edit')
        await api(`/users/${id}`, {
          method: 'PATCH',
          headers,
          body: JSON.stringify({ role: form.role }),
        })
      if (mode.value === 'reset')
        await api(`/users/${id}/password-reset`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ new_password: form.password }),
        })
      if (mode.value === 'grant')
        await api(`/users/${id}/store-grants`, {
          method: 'PUT',
          headers,
          body: JSON.stringify({ store_ids: ids.value }),
        })
    }
    form.password = ''
    opened.value = false
    ElMessage.success('设置已保存')
    await load()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    saving.value = false
  }
}
async function toggle(row: User) {
  const status = row.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await ElMessageBox.confirm(
      status === 'DISABLED' ? '停用后该账号的现有登录会话立即失效。' : '确定重新启用该账号？',
      status === 'DISABLED' ? '停用账号' : '启用账号',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api(`/users/${row.id}`, {
      method: 'PATCH',
      headers: versionHeaders(row.version),
      body: JSON.stringify({ status }),
    })
    ElMessage.success('账号状态已更新')
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
      <h1>团队与权限</h1>
      <p>按角色及店铺授权协作，账号停用和密码重置都会撤销登录会话。</p>
    </div>
    <el-button type="primary" @click="open('create')">创建账号</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="surface">
    <el-table v-loading="loading" :data="rows"
      ><el-table-column prop="login_name" label="账号" min-width="140" /><el-table-column
        label="角色"
        ><template #default="{ row }">{{
          roles[row.role as User['role']]
        }}</template></el-table-column
      ><el-table-column label="店铺范围"
        ><template #default="{ row }">{{
          row.role === 'OPERATOR' ? `${row.store_ids.length} 家授权店铺` : '企业全部店铺'
        }}</template></el-table-column
      ><el-table-column label="状态"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'info'">{{
            row.status === 'ACTIVE' ? '正常' : '已停用'
          }}</el-tag></template
        ></el-table-column
      ><el-table-column label="操作" width="300"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="open('edit', row)">角色</el-button
          ><el-button v-if="row.role === 'OPERATOR'" link type="primary" @click="open('grant', row)"
            >店铺授权</el-button
          ><el-button link type="primary" @click="open('reset', row)">重置密码</el-button
          ><el-button
            link
            :type="row.status === 'ACTIVE' ? 'danger' : 'primary'"
            @click="toggle(row)"
            >{{ row.status === 'ACTIVE' ? '停用' : '启用' }}</el-button
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
  <el-dialog
    v-model="opened"
    :title="{ create: '创建账号', edit: '修改角色', reset: '重置密码', grant: '店铺授权' }[mode]"
    width="520px"
    :close-on-click-modal="false"
    @closed="form.password = ''"
  >
    <el-form label-position="top"
      ><el-form-item v-if="mode !== 'grant'" label="账号"
        ><el-input v-model="form.login_name" :disabled="mode !== 'create'" maxlength="64"
      /></el-form-item>
      <el-form-item v-if="mode === 'create' || mode === 'edit'" label="角色"
        ><el-select v-model="form.role"
          ><el-option
            v-for="(label, value) in roles"
            :key="value"
            :label="label"
            :value="value" /></el-select
      ></el-form-item>
      <template v-if="mode === 'create' || mode === 'reset'"
        ><el-form-item label="临时密码" required
          ><el-input
            v-model="form.password"
            type="password"
            autocomplete="new-password"
            show-password
            maxlength="64" /></el-form-item
        ><el-alert
          title="12—64字符、至少两类字符。账号首次登录后必须修改临时密码。请通过安全渠道交给使用者。"
          type="info"
          :closable="false"
      /></template>
      <template v-if="mode === 'grant'"
        ><p>{{ target?.login_name }} · 运营人员</p>
        <el-checkbox-group v-model="ids"
          ><div v-for="store in stores" :key="store.id">
            <el-checkbox :value="store.id"
              >{{ store.name
              }}{{ store.status === 'ARCHIVED' ? '（已归档，仅历史）' : '' }}</el-checkbox
            >
          </div></el-checkbox-group
        ><el-empty v-if="!stores.length" description="请先创建店铺" />
        <p class="muted small">取消授权后，该店铺的历史任务和下载入口也不再可访问。</p></template
      > </el-form
    ><template #footer
      ><el-button @click="opened = false">取消</el-button
      ><el-button
        type="primary"
        :loading="saving"
        :disabled="
          (mode === 'create' && !form.login_name) ||
          ((mode === 'create' || mode === 'reset') && !form.password)
        "
        @click="save"
        >保存</el-button
      ></template
    >
  </el-dialog>
</template>
