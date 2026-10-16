<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuth } from '../stores/auth'
import { roles, errorMessage } from '../api'
const auth = useAuth()
const route = useRoute()
const router = useRouter()
async function logout() {
  try {
    await auth.logout()
    await router.replace('/login')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="logo">
        <b>O</b><span>禾序 · 运营助手<small>OPERATION AGENT</small></span>
      </div>
      <span class="nav-caption">工作空间</span>
      <nav v-if="!auth.user?.must_change_password">
        <router-link to="/" exact-active-class="selected">▦　工作台</router-link>
        <router-link to="/stores" active-class="selected">▤　店铺管理</router-link>
        <router-link to="/products" active-class="selected">▧　商品管理</router-link>
        <router-link to="/competitors" active-class="selected">▥　竞品资料</router-link>
        <template v-if="auth.user?.role === 'ADMIN'">
          <router-link to="/users" active-class="selected">♙　团队与权限</router-link>
          <router-link to="/audit" active-class="selected">☷　审计日志</router-link>
        </template>
      </nav>
      <div class="sidebar-bottom">
        <p>单企业 · 多店铺<br /><small>当前阶段：M1 数据基础</small></p>
        <strong>{{ auth.user?.login_name }}</strong
        ><small>{{ auth.user ? roles[auth.user.role] : '' }}</small>
      </div>
    </aside>
    <div class="app-main">
      <header class="topbar">
        <span>工作空间　/　{{ route.meta.title }}</span>
        <div>
          <router-link to="/password">修改密码</router-link
          ><el-button text @click="logout">退出登录</el-button>
        </div>
      </header>
      <main class="page"><router-view /></main>
    </div>
  </div>
</template>
