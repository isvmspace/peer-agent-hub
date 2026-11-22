import { computed, reactive } from 'vue'

// Unsent text per conversation, so switching chats never carries a draft to the wrong recipient.
const drafts = reactive({})

export function useDraft(key) {
  const draft = computed({
    get: () => drafts[key()] || '',
    set: value => { drafts[key()] = value },
  })
  // clear only the conversation the message was sent from, even if the user has moved on, and only the text
  // that was sent: whatever was typed while the request was in flight stays
  const clear = (sentKey, sentText) => {
    const now = drafts[sentKey] || ''
    if (sentText === undefined || now === sentText) delete drafts[sentKey]
    else if (now.startsWith(sentText)) drafts[sentKey] = now.slice(sentText.length).replace(/^\s+/, '')
  }
  return { draft, clear }
}
