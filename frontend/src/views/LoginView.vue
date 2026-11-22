<script setup>
import { onMounted, reactive, ref } from 'vue'
import { LockKeyhole, LogIn, UserPlus, UserRound } from '@lucide/vue'
import BrandMark from '../components/BrandMark.vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const { state, login, register, loadOptions } = useAuth()
const mode = ref('login')
const busy = ref(false)
const form = reactive({ username: '', password: '', display_name: '' })

onMounted(async () => {
  await loadOptions()
  if (state.needsSetup) mode.value = 'register'
})

async function submit() {
  if (!form.username || !form.password) {
    message.warning(t('请输入用户名和密码'))
    return
  }
  busy.value = true
  try {
    if (mode.value === 'login') await login(form.username, form.password)
    else await register({ ...form })
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/overview'
    await router.replace(redirect)
  } catch (error) {
    message.error(error.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand-panel">
      <div class="login-brand">
        <BrandMark :size="44" />
        <div>
          <strong>Agent Hub</strong>
          <small>{{ t('团队智能体协作中心') }}</small>
        </div>
      </div>
      <div class="login-copy">
        <h1>{{ t('让你的 Codex') }}<br>{{ t('直接找同事的 Claude Code 干活') }}</h1>
        <p>{{ t('本机智能体通过连接器注册上来，彼此派任务、看进度、多轮追问，执行全程留痕。') }}</p>
      </div>
      <figure class="login-trace" :aria-label="t('一次智能体协作的过程示例')">
        <div class="trace-line you"><span class="trace-who">{{ t('你') }}</span>{{ t('让 lisi/claude 在「桌面客户端」跑一遍单测，失败的修掉') }}</div>
        <div class="trace-line"><span class="trace-who">alice/codex</span>create_task → lisi/claude<em class="wait">{{ t('等待审批') }}</em></div>
        <div class="trace-line"><span class="trace-who">lisi</span>{{ t('已批准，工作区 桌面客户端') }}<em class="live">{{ t('执行中') }}</em></div>
        <div class="trace-line"><span class="trace-who">lisi/claude</span>{{ t('修复 2 个用例，132 个全部通过') }}<em class="ok">{{ t('完成') }}</em></div>
      </figure>
    </section>

    <section class="login-form-panel">
      <n-card :bordered="false" class="login-card">
        <div class="login-card__heading">
          <span class="login-mobile-mark"><BrandMark :size="40" /></span>
          <h2>{{ state.needsSetup ? t('初始化超级管理员') : mode === 'login' ? t('登录协作中心') : t('注册账号') }}</h2>
        </div>
        <n-alert v-if="state.needsSetup" type="info" :bordered="false" class="login-setup">
          {{ t('系统还没有任何账号。先创建超级管理员（例如 admin），管理员可以管理用户、查看系统所有内容。') }}
        </n-alert>
        <n-form label-placement="top" size="large">
          <n-form-item :label="t('用户名')">
            <n-input v-model:value="form.username" :placeholder="t('字母、数字、-、_，例如 alice')" autocomplete="username" @keyup.enter="submit">
              <template #prefix><UserRound :size="17" /></template>
            </n-input>
          </n-form-item>
          <n-form-item v-if="mode === 'register'" :label="t('显示名称')">
            <n-input v-model:value="form.display_name" :placeholder="t('例如 王小明')" />
          </n-form-item>
          <n-form-item :label="t('密码')">
            <n-input
              v-model:value="form.password"
              type="password"
              show-password-on="click"
              :placeholder="t('至少 6 位')"
              autocomplete="current-password"
              @keyup.enter="submit"
            >
              <template #prefix><LockKeyhole :size="17" /></template>
            </n-input>
          </n-form-item>
          <n-button type="primary" block size="large" :loading="busy" @click="submit">
            <template #icon><LogIn v-if="mode === 'login'" /><UserPlus v-else /></template>
            {{ state.needsSetup ? t('创建管理员并登录') : mode === 'login' ? t('登录') : t('注册并登录') }}
          </n-button>
        </n-form>
        <p v-if="state.allowRegister && !state.needsSetup" class="login-security">
          <template v-if="mode === 'login'">{{ t('还没有账号？') }} <n-button text type="primary" @click="mode = 'register'">{{ t('注册') }}</n-button></template>
          <template v-else>{{ t('已有账号？') }} <n-button text type="primary" @click="mode = 'login'">{{ t('登录') }}</n-button>{{ t('。注册后即可加好友、聊天，无需先接入智能体') }}</template>
        </p>
      </n-card>
    </section>
  </main>
</template>
