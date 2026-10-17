// 路由守卫负责导航体验；真正的角色与数据权限仍由服务端验证。
import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./views/LoginView.vue') },
    {
      path: '/',
      component: () => import('./views/LayoutView.vue'),
      children: [
        { path: '', component: () => import('./views/HomeView.vue'), meta: { title: '工作台' } },
        {
          path: 'stores',
          component: () => import('./views/StoresView.vue'),
          meta: { title: '店铺管理' },
        },
        {
          path: 'products',
          component: () => import('./views/ProductsView.vue'),
          meta: { title: '商品管理' },
        },
        {
          path: 'users',
          component: () => import('./views/UsersView.vue'),
          meta: { title: '团队与权限', admin: true },
        },
        {
          path: 'competitors',
          component: () => import('./views/CompetitorsView.vue'),
          meta: { title: '竞品资料' },
        },
        {
          path: 'audit',
          component: () => import('./views/AuditView.vue'),
          meta: { title: '审计日志', admin: true },
        },
        {
          path: 'daily-data/store',
          component: () => import('./views/StoreDailyView.vue'),
          meta: { title: '店铺日经营' },
        },
        {
          path: 'daily-data/product',
          component: () => import('./views/ProductDailyView.vue'),
          meta: { title: '商品日经营' },
        },
        {
          path: 'daily-data/ad',
          component: () => import('./views/AdDailyView.vue'),
          meta: { title: '投放日经营' },
        },
        {
          path: 'imports',
          component: () => import('./views/ImportsView.vue'),
          meta: { title: 'CSV 导入预检' },
        },
        {
          path: 'password',
          component: () => import('./views/PasswordView.vue'),
          meta: { title: '修改密码' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
router.beforeEach(async (to) => {
  const auth = useAuth()
  if (!auth.initialized) await auth.restore()
  if (!auth.user) return to.path === '/login' ? true : '/login'
  if (auth.user.must_change_password && to.path !== '/password') return '/password'
  if (to.path === '/login') return '/'
  if (to.meta.admin && auth.user.role !== 'ADMIN') return '/'
  return true
})
