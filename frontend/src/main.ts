// 前端入口：统一注册状态管理、路由与根组件。

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import { router } from './router'
import { useAuth } from './stores/auth'

// 会话失效时统一清空内存身份并返回登录页，不保留过期的权限导航。
const app = createApp(App)
app.use(createPinia()).use(router).use(ElementPlus, { locale: zhCn })
window.addEventListener('auth:expired', () => {
  useAuth().clear()
  if (router.currentRoute.value.path !== '/login') void router.replace('/login')
})
app.mount('#app')
