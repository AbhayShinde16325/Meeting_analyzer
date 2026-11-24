import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, clearSession, getApiBase, getSession, setSession } from './api/client'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import RecordPage from './pages/RecordPage'
import MeetingDetailsPage from './pages/MeetingDetailsPage'
import SystemStatusPage from './pages/SystemStatusPage'

const AppContext = createContext(null)
export const useApp = () => useContext(AppContext)

function Shell({ children }) {
  const { session, system, onLogout, refreshSystem } = useApp()
  const nav = useNavigate()
  const loc = useLocation()
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand" onClick={() => nav('/')}>
          <div className="brand-mark">TM</div>
          <div>
            <div className="brand-title">TeamMeetingAI</div>
            <div className="brand-sub">Meeting intelligence</div>
          </div>
        </div>
        <nav className="nav">
          <button className={loc.pathname === '/' ? 'nav-item active' : 'nav-item'} onClick={() => nav('/')}>Dashboard</button>
          <button className={loc.pathname === '/record' ? 'nav-item active' : 'nav-item'} onClick={() => nav('/record')}>Record</button>
          <button className={loc.pathname === '/status' ? 'nav-item active' : 'nav-item'} onClick={() => nav('/status')}>System status</button>
        </nav>
        <div className="sidebar-foot">
          <div className="meta-card">
            <div className="meta-label">Profile</div>
            <div className="meta-value">{session?.displayName || 'Unknown'}</div>
            <div className="meta-label">Server</div>
            <div className="meta-value small">{getApiBase()}</div>
            <div className="meta-label">Mode</div>
            <div className="meta-value">Ready to record</div>
          </div>
          <button className="btn ghost" onClick={refreshSystem}>Refresh status</button>
          <button className="btn ghost danger-text" onClick={onLogout}>Switch profile</button>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  )
}

function Protected({ children }) {
  const { session } = useApp()
  if (!session?.profileId) return <Navigate to="/login" replace />
  return <Shell>{children}</Shell>
}

export default function App() {
  const [session, setSessionState] = useState(() => getSession())
  const [system, setSystem] = useState(null)
  const [systemError, setSystemError] = useState('')
  const navigate = useNavigate()

  const refreshSystem = async () => {
    try {
      const status = await api.systemStatus()
      setSystem(status)
      setSystemError('')
    } catch (e) {
      setSystemError(e.message || 'Unable to load system status')
    }
  }

  useEffect(() => { if (session?.profileId) refreshSystem() }, [session?.profileId])

  const value = useMemo(() => ({
    session, system, systemError, refreshSystem,
    setAppSession: (next) => { setSession(next); setSessionState(next || {}); },
    onLogout: () => { clearSession(); setSessionState({}); setSystem(null); navigate('/login') },
  }), [session, system, systemError])

  return (
    <AppContext.Provider value={value}>
      <Routes>
        <Route path="/login" element={session?.profileId ? <Navigate to="/" replace /> : <LoginPage />} />
        <Route path="/" element={<Protected><DashboardPage /></Protected>} />
        <Route path="/record" element={<Protected><RecordPage /></Protected>} />
        <Route path="/status" element={<Protected><SystemStatusPage /></Protected>} />
        <Route path="/meetings/:meetingId" element={<Protected><MeetingDetailsPage /></Protected>} />
        <Route path="*" element={<Navigate to={session?.profileId ? '/' : '/login'} replace />} />
      </Routes>
    </AppContext.Provider>
  )
}
