import React, { useEffect, useState } from 'react'
import { api } from '../api/client'
import { formatDate } from '../utils/format'

function bytes(n) {
  if (!n || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export default function SystemStatusPage() {
  const [status, setStatus] = useState(null)
  const [health, setHealth] = useState(null)
  const [error, setError] = useState('')

  const load = async () => {
    try {
      const [s, h] = await Promise.all([api.systemStatus(), api.health()])
      setStatus(s); setHealth(h); setError('')
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  return (
    <div className="page">
      <div className="page-header">
        <div><div className="eyebrow">System</div><h1>Status</h1><p className="muted">Backend, database, storage, and recording capability.</p></div>
        <div className="header-actions"><button className="btn" onClick={load}>Refresh</button></div>
      </div>
      {error && <div className="alert error">{error}</div>}
      <div className="stats-grid">
        <div className="card stat"><div className="stat-label">API</div><div className="stat-value" style={{ fontSize: 22 }}>{health?.status || '—'}</div></div>
        <div className="card stat"><div className="stat-label">Database</div><div className="stat-value" style={{ fontSize: 22 }}>{health?.database ? 'connected' : '—'}</div></div>
        <div className="card stat"><div className="stat-label">Meetings</div><div className="stat-value">{status?.meeting_count ?? '—'}</div></div>
        <div className="card stat"><div className="stat-label">Can record</div><div className="stat-value" style={{ fontSize: 22 }}>{status ? 'Yes' : '—'}</div></div>
      </div>
      <div className="card">
        <div className="section-title">Storage and schema</div>
        <div className="meta-list">
          <div><span className="meta-label">Schema revision</span><div>{status?.schema_version || '—'}</div></div>
          <div><span className="meta-label">DB size</span><div>{bytes(status?.db_size_bytes)}</div></div>
          <div><span className="meta-label">Disk free</span><div>{bytes(status?.disk_free_bytes)}</div></div>
          <div><span className="meta-label">Last backup</span><div>{status?.last_backup ? `${status.last_backup.status} at ${formatDate(status.last_backup.finished_at || status.last_backup.started_at)} (${bytes(status.last_backup.size_bytes)})` : 'No backup recorded'}</div></div>
        </div>
      </div>
    </div>
  )
}
