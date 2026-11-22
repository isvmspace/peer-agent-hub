import { onBeforeUnmount, ref } from 'vue'

// true below the phone breakpoint, where wide tables turn into card lists
export function usePhone(query = '(max-width: 760px)') {
  const media = window.matchMedia(query)
  const phone = ref(media.matches)
  const update = e => { phone.value = e.matches }
  media.addEventListener('change', update)
  onBeforeUnmount(() => media.removeEventListener('change', update))
  return phone
}
