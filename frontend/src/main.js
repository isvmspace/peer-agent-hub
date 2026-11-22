import { createApp } from 'vue'
import naive from 'naive-ui'
import App from './App.vue'
import router from './router'
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/500.css'
import '@fontsource/space-grotesk/600.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './styles/theme.css'
import './styles/app.css'

// after a deploy, a tab opened earlier asks for page chunks that no longer exist: reload once to get the new build
router.onError((error, to) => {
  if (!/dynamically imported module|Importing a module script failed/.test(error?.message || '')) return
  try {
    if (sessionStorage.getItem('chunk-reload') === to.fullPath) return
    sessionStorage.setItem('chunk-reload', to.fullPath)
  } catch { /* storage blocked: reload anyway */ }
  location.assign(router.resolve(to).href)
  location.reload()
})
router.afterEach(() => { try { sessionStorage.removeItem('chunk-reload') } catch { /* ignore */ } })

createApp(App).use(naive).use(router).mount('#app')
