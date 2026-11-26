const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message || 'Request failed')
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export class ApiConnectionError extends Error {
  constructor(message) {
    super(message || 'Could not reach the server')
    this.name = 'ApiConnectionError'
  }
}

export function getSession() {
  try {
    const s = JSON.parse(localStorage.getItem('tma_session') || '{}')
    if (typeof s.hostToken === 'string') s.hostToken = s.hostToken.trim()
    return s
  } catch {
    return {}
  }
}

export function setSession(session) {
  const next = { ...(session || {}) }
  if (typeof next.hostToken === 'string') next.hostToken = next.hostToken.trim()
  localStorage.setItem('tma_session', JSON.stringify(next))
}

export function clearSession() {
  localStorage.removeItem('tma_session')
}

export function getApiBase() {
  return API_BASE
}

function buildHeaders({ json = false, headers = {} } = {}) {
  const session = getSession()
  const next = { ...headers }
  if (json) next['Content-Type'] = 'application/json'
  if (session.profileId) next['X-Profile-Id'] = session.profileId
  return next
}

async function parseResponse(response) {
  if (response.status === 204) return null
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function request(path, options = {}) {
  const { method = 'GET', json, body, timeoutMs = 30000, headers } = options
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: buildHeaders({ json, headers }),
      body: body === undefined ? undefined : json ? JSON.stringify(body) : body,
      signal: controller.signal,
    })
    const payload = await parseResponse(response)
    if (!response.ok) {
      const err = payload?.error || payload?.detail?.error || payload?.detail || {}
      const message = err.message || (typeof payload?.detail === 'string' ? payload.detail : response.statusText)
      throw new ApiError(response.status, err.code || 'http_error', message, payload)
    }
    return payload
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err.name === 'AbortError') throw new ApiConnectionError('Request timed out')
    throw new ApiConnectionError(err.message || 'Network error')
  } finally {
    clearTimeout(timer)
  }
}

async function download(path, filename) {
  const response = await fetch(`${API_BASE}${path}`, { headers: buildHeaders() })
  if (!response.ok) {
    const payload = await parseResponse(response)
    const err = payload?.error || {}
    throw new ApiError(response.status, err.code || 'http_error', err.message || 'Download failed', payload)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || 'download'
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export const api = {
  health: () => request('/health'),
  systemStatus: () => request('/system/status'),
  listUsers: () => request('/users'),
  createUser: (display_name) => request('/users', { method: 'POST', json: true, body: { display_name } }),
  listMeetings: ({ query, status, limit = 200, offset = 0 } = {}) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (query) params.set('query', query)
    if (status) params.set('status', status)
    return request(`/meetings?${params.toString()}`)
  },
  getMeeting: (id) => request(`/meetings/${id}`),
  deleteMeeting: (id) => request(`/meetings/${id}`, { method: 'DELETE' }),
  uploadRecording: async ({ title, userId, file, durationMs = 0, sampleRate = 48000, channels = 2, notes }) => {
    const form = new FormData()
    form.append('title', title)
    form.append('user_id', userId)
    form.append('duration_ms', String(durationMs))
    form.append('sample_rate', String(sampleRate))
    form.append('channels', String(channels))
    if (notes) form.append('notes', notes)
    form.append('file', file, file.name || 'recording.wav')
    return request('/meetings', { method: 'POST', body: form, timeoutMs: 3600000 })
  },
  uploadMultiSourceRecording: async ({ title, userId, durationMs = 0, notes, sources = {}, sourceManifest = {} }) => {
    const form = new FormData()
    form.append('title', title)
    form.append('user_id', userId)
    form.append('duration_ms', String(durationMs))
    if (notes) form.append('notes', notes)
    form.append('source_manifest', JSON.stringify(sourceManifest))
    for (const [name, file] of Object.entries(sources)) {
      if (file) form.append(name, file, file.name || `${name}.webm`)
    }
    return request('/meetings/multi-source', { method: 'POST', body: form, timeoutMs: 3600000 })
  },
  listFiles: (meetingId) => request(`/meetings/${meetingId}/files`),
  uploadAttachment: async (meetingId, file) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return request(`/meetings/${meetingId}/files`, { method: 'POST', body: form, timeoutMs: 300000 })
  },
  downloadFile: (fileId, filename) => download(`/files/${fileId}/download`, filename),
  deleteAttachment: (fileId) => request(`/files/${fileId}`, { method: 'DELETE' }),
  listTranscripts: (meetingId) => request(`/meetings/${meetingId}/transcripts`),
  getSummary: (meetingId) => request(`/meetings/${meetingId}/summary`),
  getMetrics: (meetingId) => request(`/meetings/${meetingId}/metrics`),
  listActionItems: (meetingId) => request(`/meetings/${meetingId}/action-items`),
  patchActionItem: (itemId, fields) => request(`/action-items/${itemId}`, { method: 'PATCH', json: true, body: fields }),
  listMeetingSpeakers: (meetingId) => request(`/meetings/${meetingId}/speakers`),
  assignMeetingSpeaker: (meetingId, localLabel, fields) => request(`/meetings/${meetingId}/speakers/${encodeURIComponent(localLabel)}/assign`, { method: 'POST', json: true, body: fields }),
  requestProfiling: (meetingId) => request(`/meetings/${meetingId}/speakers/profile`, { method: 'POST' }),
  listSpeakerProfiles: () => request('/profiles'),
  createSpeakerProfile: (display_name) => request('/profiles', { method: 'POST', json: true, body: { display_name } }),
  renameSpeakerProfile: (profileId, display_name) => request(`/profiles/${profileId}`, { method: 'PATCH', json: true, body: { display_name } }),
  deleteSpeakerProfile: (profileId) => request(`/profiles/${profileId}`, { method: 'DELETE' }),
  enrollSpeakerProfile: async (profileId, file) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return request(`/profiles/${profileId}/enroll`, { method: 'POST', body: form, timeoutMs: 300000 })
  },
  listProfileReferences: (profileId) => request(`/profiles/${profileId}/references`),
  deleteProfileReference: (profileId, referenceId) => request(`/profiles/${profileId}/references/${referenceId}`, { method: 'DELETE' }),
  listJobs: (meetingId) => request(`/meetings/${meetingId}/jobs`),
  getSpeakerProfiles: () => request('/profiles'),
  transcribe: (meetingId) => request(`/meetings/${meetingId}/transcribe`, { method: 'POST' }),
  analyse: (meetingId) => request(`/meetings/${meetingId}/analyse`, { method: 'POST' }),
  cancelJob: (meetingId, jobId) => request(`/meetings/${meetingId}/jobs/${jobId}/cancel`, { method: 'POST' }),
  exportMeeting: (meetingId) => download(`/meetings/${meetingId}/export?include_audio=true`, `meeting-${meetingId}.zip`),
  audioUrl: (fileId) => `${API_BASE}/files/${fileId}`,
}
