<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage } from '../api'
import { useAuth } from '../stores/auth'
const auth = useAuth()
const router = useRouter()
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const busy = ref(false)
const error = ref('')
async function submit() {
  error.value = ''
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次新密码不一致'
    return
  }
  busy.value = true
  try {
    await api('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword.value, new_password: newPassword.value }),
    })
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    auth.clear()
    ElMessage.success('密码已更新，请重新登录')
    await router.replace('/login')
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>修改密码</h1>
      <p>更新密码后，当前账号的所有登录会话都会失效。</p>
    </div>
  </div>
  <section class="surface narrow">
    <el-alert
      v-if="auth.user?.must_change_password"
      title="你正在使用临时密码，请先修改密码后继续。"
      type="warning"
      :closable="false"
    /><el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="旧密码" required
        ><el-input
          v-model="oldPassword"
          type="password"
          show-password
          autocomplete="current-password"
          maxlength="64"
      /></el-form-item>
      <el-form-item label="新密码" required
        ><el-input
          v-model="newPassword"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="64"
      /></el-form-item>
      <el-form-item label="确认新密码" required
        ><el-input
          v-model="confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="64"
      /></el-form-item>
      <p class="muted small">12—64 个字符，至少包含大写、小写、数字、其他字符中的两类。</p>
      <el-button
        native-type="submit"
        type="primary"
        :loading="busy"
        :disabled="!oldPassword || !newPassword || !confirmPassword"
        >保存新密码</el-button
      >
    </el-form>
  </section>
</template>
