<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../stores/auth'
import { errorMessage } from '../api'
const auth = useAuth()
const router = useRouter()
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
async function submit() {
  if (busy.value || !username.value || !password.value) return
  busy.value = true
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    password.value = ''
    await router.replace(auth.user?.must_change_password ? '/password' : '/')
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <main class="login-page">
    <section class="login-brand">
      <span class="eyebrow">OPERATION / ASSISTANT</span>
      <h1>运营有章法，<br />创意有依据。</h1>
      <p>统一管理店铺资料，让每一步运营行动都有可靠的数据基础。</p>
      <div class="brand-foot">
        禾序 · 电商运营智能助手<br /><small>M1 · 身份权限与店铺管理</small>
      </div>
    </section>
    <section class="login-form">
      <div>
        <span class="eyebrow">禾序 · 运营助手</span>
        <h2>登录工作空间</h2>
        <p class="muted">使用管理员分配的账号继续，无公开注册入口。</p>
        <el-alert
          v-if="error || auth.connectionError"
          :title="error || auth.connectionError"
          type="error"
          :closable="false"
          show-icon
        />
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="账号" required
            ><el-input
              v-model="username"
              autocomplete="username"
              maxlength="64"
              placeholder="请输入账号"
          /></el-form-item>
          <el-form-item label="密码" required
            ><el-input
              v-model="password"
              type="password"
              autocomplete="current-password"
              maxlength="64"
              show-password
              placeholder="请输入密码"
          /></el-form-item>
          <el-button
            native-type="submit"
            type="primary"
            :loading="busy"
            :disabled="!username || !password"
            class="full"
            >登录</el-button
          >
        </el-form>
        <p class="muted small">忘记密码？请联系管理员重置。连续输错 5 次锁定 15 分钟。</p>
        <details>
          <summary>首次使用，尚无账号？</summary>
          <p class="muted small">在 backend 目录执行命令，交互设置管理员密码：</p>
          <code>uv run python -m app.cli create-admin --username admin</code>
        </details>
      </div>
    </section>
  </main>
</template>
