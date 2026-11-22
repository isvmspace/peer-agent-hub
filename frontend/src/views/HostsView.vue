<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NSpace, NTag, useMessage } from 'naive-ui'
import { RefreshCw, Search } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

// Admin view of every computer whose connector has registered with the hub.
import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const hosts = ref([])
const loading = ref(false)
const keyword = ref('')

async function load() {
  loading.value = true
  try { hosts.value = (await api('/api/admin/hosts')).hosts } catch (e) { message.error(e.message) } finally { loading.value = false }
}

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return hosts.value
  return hosts.value.filter(r => [r.hostname, r.name, r.username, r.display_name, r.remote_ip, ...r.local_ips, r.os]
    .some(v => String(v || '').toLowerCase().includes(k)))
})

const columns = [
  {
    title: t('主机'), key: 'hostname', minWidth: 190,
    render: r => h('div', { class: 'primary-cell' }, [
      h('strong', null, [h('span', { class: ['dot', r.online ? 'on' : 'off'] }), r.hostname || r.name]),
      h('small', null, `${r.os} · ${t('连接器 {version}', { version: r.version || '—' })}`),
    ]),
  },
  {
    title: t('用户'), key: 'username', width: 150,
    render: r => h('div', { class: 'secondary-cell' }, [h('strong', null, r.display_name || r.username), h('small', null, r.username)]),
  },
  {
    title: 'IP', key: 'remote_ip', minWidth: 200,
    render: r => h('div', { class: 'secondary-cell' }, [
      h('strong', { title: t('连接器上报的本机网卡地址，第一个是访问 Hub 用的') }, r.local_ips[0] || r.remote_ip || '—'),
      r.local_ips.length > 1 ? h('small', null, t('其他网卡 {ips}', { ips: r.local_ips.slice(1).join(t('、')) })) : null,
      r.remote_ip && r.remote_ip !== r.local_ips[0] ? h('small', { title: t('Hub 看到的连接来源地址（经过路由/NAT 后的地址）') }, t('经 {ip} 访问', { ip: r.remote_ip })) : null,
    ]),
  },
  {
    title: t('状态'), key: 'online', width: 170,
    render: r => h('div', { class: 'secondary-cell' }, [
      h(NTag, { size: 'small', bordered: false, type: r.online ? 'success' : 'default' }, () => (r.online ? t('在线') : t('离线'))),
      h('small', null, r.online ? t('连接于 {time}', { time: formatTime(r.connected_at) }) : t('最后在线 {time}', { time: formatTime(r.last_seen_at) })),
    ]),
  },
  {
    title: t('智能体'), key: 'agents', minWidth: 200,
    render: r => r.agents.length
      ? h(NSpace, { size: 4 }, () => r.agents.map(a => h(NTag, { size: 'small', bordered: false, type: a.online ? 'info' : 'default' }, () => a.key)))
      : h('small', { class: 'hint' }, t('未发现智能体')),
  },
  {
    title: t('令牌'), key: 'token_name', width: 150,
    render: r => h('div', { class: 'secondary-cell' }, [h('span', null, r.token_name), r.revoked ? h('small', { style: 'color:var(--err)' }, t('已吊销')) : null]),
  },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('主机管理')" :description="t('所有注册过连接器的电脑、所属用户和网络地址（仅管理员可见）')">
      <template #actions><n-button @click="load"><template #icon><RefreshCw /></template>{{ t('刷新') }}</n-button></template>
    </PageHeader>
    <n-card>
      <div class="table-toolbar">
        <span>{{ t('共 {n} 台，在线 {online} 台', { n: hosts.length, online: hosts.filter(r => r.online).length }) }}</span>
        <n-input v-model:value="keyword" class="search-input" clearable :placeholder="t('搜索主机、用户、IP')">
          <template #prefix><Search :size="16" /></template>
        </n-input>
      </div>
      <ResponsiveTable :columns="columns" :data="filtered" :loading="loading" :row-key="r => r.id" :scroll-x="1160" />
    </n-card>
  </div>
</template>
