<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Page, type Store, errorMessage, roles } from '../api'
import { useAuth } from '../stores/auth'
const auth = useAuth()
const total = ref<number | null>(null)
const error = ref('')
onMounted(async () => {
  try {
    total.value = (await api<Page<Store>>('/stores?page_size=1')).total
  } catch (e) {
    error.value = errorMessage(e)
  }
})
</script>
<template>
  <div class="page-heading">
    <div>
      <h1>工作台</h1>
      <p>欢迎回来，{{ auth.user?.login_name }}。从维护店铺和团队权限开始。</p>
    </div>
    <router-link to="/stores"><el-button type="primary">进入店铺管理</el-button></router-link>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <section class="metric-grid">
    <article class="surface">
      <span class="muted">当前角色</span>
      <h2>{{ auth.user ? roles[auth.user.role] : '—' }}</h2>
      <p>权限由服务端实时验证</p>
    </article>
    <article class="surface">
      <span class="muted">可访问店铺</span>
      <h2>{{ total ?? '—' }}</h2>
      <p>仅统计当前账号可见范围</p>
    </article>
    <article class="surface">
      <span class="muted">登录保护</span>
      <h2>已启用</h2>
      <p>安全会话 · CSRF · 失败锁定</p>
    </article>
  </section>
  <section class="surface">
    <h2>本阶段已开放</h2>
    <p>账号登录、修改密码、店铺管理、角色权限、店铺授权与操作审计。</p>
    <el-alert
      title="商品、日经营数据、CSV 导入和经营指标仍在后续 M1 开发范围内，当前不展示模拟经营结果。"
      type="info"
      :closable="false"
    />
    <div class="actions">
      <router-link to="/stores"><el-button>查看店铺</el-button></router-link
      ><router-link v-if="auth.user?.role === 'ADMIN'" to="/users"
        ><el-button>管理团队</el-button></router-link
      >
    </div>
  </section>
  <section v-if="total === 0" class="surface empty">
    <h2>暂时没有可访问的店铺</h2>
    <p>
      {{
        auth.user?.role === 'ADMIN'
          ? '先创建一家店铺，再为运营人员分配访问权限。'
          : '请联系管理员创建店铺或为你分配店铺权限。'
      }}
    </p>
  </section>
</template>
