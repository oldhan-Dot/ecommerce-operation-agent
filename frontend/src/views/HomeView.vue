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
      <p>欢迎回来，{{ auth.user?.login_name }}。查看经营表现，生成建议并协作审核。</p>
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
    <h2>运营工作空间</h2>
    <p>
      账号登录、修改密码、店铺管理、商品及 SKU
      管理、竞品资料、三类日经营数据、CSV导入、经营概览、六类AI生成、产出修订与人工审核。
    </p>
    <el-alert
      title="先维护来源资料与经营数据，再创建AI任务；生成内容由另一名管理员或主管审核后正式导出。"
      type="info"
      :closable="false"
    />
    <div class="actions">
      <router-link to="/ai-tasks"><el-button type="primary">进入 AI 工作台</el-button></router-link>
      <router-link to="/artifacts"><el-button>产出与审核</el-button></router-link>
      <router-link to="/dashboard"><el-button type="primary">查看经营概览</el-button></router-link>
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
