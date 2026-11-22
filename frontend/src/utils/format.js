import { t } from '../i18n'

export const STATUS = {
  pending_approval: { label: t('待审批'), type: 'warning' },
  queued: { label: t('排队中'), type: 'info' },
  dispatched: { label: t('已下发'), type: 'info' },
  running: { label: t('执行中'), type: 'primary' },
  completed: { label: t('已完成'), type: 'success' },
  failed: { label: t('失败'), type: 'error' },
  canceled: { label: t('已取消'), type: 'default' },
  rejected: { label: t('已拒绝'), type: 'default' },
}

export const ISOLATION = {
  sandbox: { label: t('沙箱隔离'), type: 'success', tip: t('只能写入任务所在的工作区（系统级沙箱）') },
  limited: { label: t('受限'), type: 'warning', tip: t('文件编辑限制在工作区内，命令行没有系统级沙箱') },
  none: { label: t('完全权限'), type: 'error', tip: t('不做隔离，只有归属人本人和被特别允许的人可以派任务') },
}

export const ACTIVE_STATUSES = ['pending_approval', 'queued', 'dispatched', 'running']

export function formatTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function duration(start, end) {
  if (!start) return ''
  const s = Math.max(0, Math.round((end || Date.now() / 1000) - start))
  if (s < 60) return t('{s} 秒', { s })
  if (s < 3600) return t('{m} 分 {s} 秒', { m: Math.floor(s / 60), s: s % 60 })
  return t('{h} 小时 {m} 分', { h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60) })
}
