import { createRouter, createWebHashHistory } from 'vue-router'
import ConsoleLayout from './layouts/ConsoleLayout.vue'
import LoginView from './views/LoginView.vue'
import { useAuth } from './composables/useAuth'
import { setUnauthorizedHandler } from './api/client'

const router = createRouter({
  history: createWebHashHistory('/console/'),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    {
      path: '/',
      component: ConsoleLayout,
      children: [
        { path: '', redirect: '/overview' },
        { path: 'overview', name: 'overview', component: () => import('./views/OverviewView.vue') },
        { path: 'chat', name: 'chat', component: () => import('./views/ChatView.vue') },
        { path: 'agents', name: 'agents', component: () => import('./views/AgentsView.vue') },
        { path: 'tasks', name: 'tasks', component: () => import('./views/TasksView.vue') },
        { path: 'tasks/:id', name: 'task-detail', component: () => import('./views/TaskDetailView.vue') },
        { path: 'workflows', name: 'workflows', component: () => import('./views/WorkflowsView.vue') },
        { path: 'work', name: 'work', component: () => import('./views/WorkView.vue') },
        { path: 'collab', name: 'collab', component: () => import('./views/CollabView.vue') },
        { path: 'connect', name: 'connect', component: () => import('./views/ConnectView.vue') },
        { path: 'rules', name: 'rules', component: () => import('./views/RulesView.vue') },
        { path: 'audit', name: 'audit', component: () => import('./views/AuditView.vue') },
        { path: 'releases', name: 'releases', component: () => import('./views/ReleasesView.vue') },
        { path: 'users', name: 'users', component: () => import('./views/UsersView.vue'), meta: { admin: true } },
        { path: 'work-admin', name: 'work-admin', component: () => import('./views/WorkAdminView.vue') },
        { path: 'hosts', name: 'hosts', component: () => import('./views/HostsView.vue'), meta: { admin: true } },
        { path: 'profile', name: 'profile', component: () => import('./views/ProfileView.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/overview' },
  ],
  scrollBehavior: () => ({ left: 0, top: 0 }),
})

setUnauthorizedHandler(async () => {
  const { logout } = useAuth()
  logout()
  const current = router.currentRoute.value
  if (current.name !== 'login') await router.replace({ name: 'login', query: { redirect: current.fullPath } })
})

router.beforeEach(async to => {
  const { state, restoreSession } = useAuth()
  const user = state.checked ? state.user : await restoreSession()
  if (to.meta.public) return to.name === 'login' && user ? { name: 'overview' } : true
  if (!user) return { name: 'login', query: { redirect: to.fullPath } }
  return to.meta.admin && !user.is_admin ? { name: 'overview' } : true
})

export default router
