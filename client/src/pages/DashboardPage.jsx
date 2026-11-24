import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApp } from '../App'
import { formatDate, formatDurationMs, statusLabel } from '../utils/format'

export default function DashboardPage() {
  const nav = useNavigate()
  const { system, refreshSystem } = useApp()
  const [meetings, setMeetings] = useState([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyDelete, setBusyDelete] = useState('')

  const load = async (q = query) => {
    setLoading(true)
    try {
      const data = await api.listMeetings({ query: q || undefined, limit: 200 })
      setMeetings(data)
      setError('')
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { load(''); refreshSystem() }, [])
  useEffect(() => {
    const t = setTimeout(() => load(query), 250)
    return () => clearTimeout(t)
  }, [query])

  const stats = useMemo(() => {
    const total = meetings.length
    const completed = meetings.filter(m => m.status === 'completed').length
    const processing = meetings.filter(m => ['recorded','transcribed','analyzing'].includes(m.status)).length
    const failed = meetings.filter(m => m.status === 'failed').length
    return { total, completed, processing, failed }
  }, [meetings])

  const onDelete = async (id, title) => {
    if (!confirm(`Delete "${title}" permanently? This removes audio, transcripts, summaries, and attachments.`)) return
    setBusyDelete(id)
    try { await api.deleteMeeting(id); await load(query) }
    catch (e) { alert(e.message) }
    finally { setBusyDelete('') }
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="eyebrow">Dashboard</div>
          <h1>Meetings</h1>
          <p className="muted">Search, review, process, and export meetings stored on the TeamMeetingAI server.</p>
        </div>
        <div className="header-actions">
          <button className="btn" onClick={() => load(query)}>Refresh</button>
          <button className="btn primary" onClick={() => nav('/record')} >New recording</button>
        </div>
      </div>

      <div className="stats-grid">
        <Stat label="Total meetings" value={stats.total} />
        <Stat label="Completed" value={stats.completed} />
        <Stat label="Processing" value={stats.processing} />
        <Stat label="Failed" value={stats.failed} tone={stats.failed ? 'danger' : undefined} />
      </div>

      <div className="toolbar card">
        <input className="search" placeholder="Search title, transcript, summary..." value={query} onChange={e => setQuery(e.target.value)} />
        <div className="muted small">{system ? `${system.meeting_count} on server • DB ${Math.round((system.db_size_bytes||0)/1024/1024)} MB • ready` : 'Loading system status...'}</div>
      </div>

      {error && <div className="alert error">{error}</div>}
      <div className="card table-card">
        <div className="table-wrap">
          <table>
            <thead><tr><th>Date</th><th>Title</th><th>Duration</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {loading ? <tr><td colSpan="5" className="empty">Loading meetings...</td></tr> :
               meetings.length === 0 ? <tr><td colSpan="5" className="empty">No meetings found.</td></tr> :
               meetings.map(m => (
                <tr key={m.id} className="row-click" onClick={() => nav(`/meetings/${m.id}`)}>
                  <td>{formatDate(m.recorded_at)}</td>
                  <td><div className="title-cell">{m.title}</div></td>
                  <td>{formatDurationMs(m.duration_ms)}</td>
                  <td><span className={`badge status-${m.status}`}>{statusLabel(m.status)}</span></td>
                  <td className="actions" onClick={e => e.stopPropagation()}>
                    <button className="btn small" onClick={() => nav(`/meetings/${m.id}`)}>Open</button>
                    <button className="btn small danger" disabled={busyDelete===m.id} onClick={() => onDelete(m.id, m.title)}>{busyDelete===m.id?'...':'Delete'}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, tone }) {
  return <div className={`card stat ${tone||''}`}><div className="stat-label">{label}</div><div className="stat-value">{value}</div></div>
}
