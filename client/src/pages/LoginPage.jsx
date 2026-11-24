import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApp } from '../App'

export default function LoginPage() {
  const { setAppSession, refreshSystem } = useApp()
  const nav = useNavigate()
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const data = await api.listUsers()
      setUsers(data)
      setError('')
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const continueWith = async (user) => {
    const session = { profileId: String(user.id), displayName: user.display_name }
    setAppSession(session)
    await refreshSystem()
    nav('/')
  }

  const onContinue = async () => {
    setBusy(true); setError('')
    try {
      const name = newName.trim()
      let user = null
      if (name) user = await api.createUser(name)
      else user = users.find(u => String(u.id) === String(selectedId))
      if (!user) throw new Error('Select a profile or create a new one.')
      await continueWith(user)
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="eyebrow">TeamMeetingAI</div>
        <h1>Sign into your meeting workspace</h1>
        <p className="muted">Enter your name to create/select a profile and start recording.</p>
        {error && <div className="alert error">{error}</div>}
        <label className="field">
          <span>Existing profile</span>
          <select value={selectedId} onChange={e => setSelectedId(e.target.value)} disabled={loading || busy}>
            <option value="">Select a profile...</option>
            {users.map(u => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </select>
        </label>
        <div className="divider"><span>or</span></div>
        <label className="field">
          <span>Create new profile</span>
          <input value={newName} onChange={e => setNewName(e.target.value)} placeholder="Your name" disabled={busy} />
        </label>
        <button className="btn primary" onClick={onContinue} disabled={busy || loading}>{busy ? 'Connecting...' : 'Continue'}</button>
        <div className="fineprint">Audio is uploaded to the server. Transcription/analysis may use configured cloud providers; record only with participant consent and policy approval.</div>
      </div>
    </div>
  )
}
