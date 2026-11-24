import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApp } from '../App'

function Meter({ value, label }) {
  const pct = Math.min(100, Math.round((value || 0) * 100))
  const color = value > 0.85 ? '#dc2626' : value > 0.02 ? '#16a34a' : '#9ca3af'
  return (
    <div style={{ marginBottom: 8 }}>
      <div className="muted small" style={{ display: 'flex', justifyContent: 'space-between' }}><span>{label}</span><span>{pct}%</span></div>
      <div style={{ height: 8, background: '#e5e7eb', borderRadius: 4, overflow: 'hidden' }}><div style={{ width: `${pct}%`, height: '100%', background: color }} /></div>
    </div>
  )
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg', 'audio/mp4']

function extForMime(mime = '') {
  const m = String(mime).toLowerCase()
  if (m.includes('webm')) return 'webm'
  if (m.includes('ogg')) return 'ogg'
  if (m.includes('mp4') || m.includes('m4a')) return 'm4a'
  if (m.includes('mpeg') || m.includes('mp3')) return 'mp3'
  if (m.includes('wav')) return 'wav'
  return 'webm'
}

function trackInfo(stream, label) {
  const tracks = stream?.getAudioTracks?.() || []
  return {
    label,
    trackCount: tracks.length,
    tracks: tracks.map(t => ({ id: t.id, label: t.label, readyState: t.readyState, enabled: t.enabled, muted: t.muted })),
  }
}

function hasLiveAudioTrack(stream) {
  return (stream?.getAudioTracks?.() || []).some(t => t.readyState === 'live')
}

function audioOnlyStream(stream) {
  if (!stream) return null
  const tracks = (stream.getAudioTracks?.() || []).filter(t => t.readyState === 'live')
  return tracks.length ? new MediaStream(tracks) : null
}

function pickRecorderMime() {
  if (typeof MediaRecorder === 'undefined') return { mimeType: '', strategy: 'unsupported' }
  for (const mimeType of MIME_CANDIDATES) {
    try { if (MediaRecorder.isTypeSupported(mimeType)) return { mimeType, strategy: 'supported' } } catch {}
  }
  return { mimeType: '', strategy: 'default' }
}

function createMediaRecorder(stream, preferredMime) {
  const attempts = []
  if (preferredMime) attempts.push({ mimeType: preferredMime })
  for (const mimeType of MIME_CANDIDATES) {
    if (mimeType !== preferredMime) attempts.push({ mimeType })
  }
  attempts.push({})
  const errors = []
  for (const opts of attempts) {
    try {
      const rec = opts.mimeType ? new MediaRecorder(stream, opts) : new MediaRecorder(stream)
      return { recorder: rec, mimeType: rec.mimeType || opts.mimeType || '', errors }
    } catch (e) {
      errors.push({ options: opts, name: e?.name, message: e?.message })
    }
  }
  throw new Error(`No usable MediaRecorder configuration: ${errors.map(e => e.message).join('; ')}`)
}

export default function RecordPage() {
  const { session, system } = useApp()
  const nav = useNavigate()
  const [title, setTitle] = useState('')
  const [notes, setNotes] = useState('')
  const [recording, setRecording] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const [audioUrl, setAudioUrl] = useState('')
  const [lastBlob, setLastBlob] = useState(null)
  const [micDevices, setMicDevices] = useState([])
  const [selectedMic, setSelectedMic] = useState('')
  const [tabAudioEnabled, setTabAudioEnabled] = useState(false)
  const [micLevel, setMicLevel] = useState(0)
  const [tabLevel, setTabLevel] = useState(0)
  const [micStatus, setMicStatus] = useState('idle')
  const [tabStatus, setTabStatus] = useState('idle')

  const recorderRef = useRef(null)
  const chunksRef = useRef([])
  const startRef = useRef(0)
  const timerRef = useRef(null)
  const micStreamRef = useRef(null)
  const tabStreamRef = useRef(null)
  const mixedStreamRef = useRef(null)
  const audioCtxRef = useRef(null)
  const destinationRef = useRef(null)
  const sourceNodesRef = useRef([])
  const analysersRef = useRef([])
  const rafRef = useRef(null)
  const lastBlobRef = useRef(null)
  const startingRef = useRef(false)
  const audioUrlRef = useRef('')

  useEffect(() => { audioUrlRef.current = audioUrl }, [audioUrl])

  const refreshDevices = async () => {
    try {
      if (!navigator.mediaDevices?.enumerateDevices) return
      const devices = await navigator.mediaDevices.enumerateDevices()
      const mics = devices.filter(d => d.kind === 'audioinput')
      setMicDevices(mics)
      if (!selectedMic && mics[0]?.deviceId) setSelectedMic(mics[0].deviceId)
    } catch (e) {
      console.warn('[recording] device enumeration failed', { name: e?.name, message: e?.message })
    }
  }

  useEffect(() => { refreshDevices(); navigator.mediaDevices?.addEventListener?.('devicechange', refreshDevices); return () => navigator.mediaDevices?.removeEventListener?.('devicechange', refreshDevices) }, [])

  const stopTracks = (stream) => { try { stream?.getTracks?.().forEach(t => { try { t.stop() } catch {} }) } catch {} }

  const cleanupRecording = ({ preservePreview = true } = {}) => {
    clearInterval(timerRef.current); timerRef.current = null
    if (rafRef.current) cancelAnimationFrame(rafRef.current); rafRef.current = null
    analysersRef.current = []
    sourceNodesRef.current.forEach(n => { try { n.disconnect() } catch {} })
    sourceNodesRef.current = []
    destinationRef.current = null
    try { recorderRef.current && recorderRef.current.state !== 'inactive' && recorderRef.current.stop() } catch {}
    recorderRef.current = null
    stopTracks(micStreamRef.current); micStreamRef.current = null
    stopTracks(tabStreamRef.current); tabStreamRef.current = null
    stopTracks(mixedStreamRef.current); mixedStreamRef.current = null
    try { audioCtxRef.current?.close?.() } catch {}
    audioCtxRef.current = null
    if (!preservePreview && audioUrlRef.current) { try { URL.revokeObjectURL(audioUrlRef.current) } catch {} }
  }

  useEffect(() => () => { cleanupRecording({ preservePreview: false }) }, [])

  const attachAnalyser = (ctx, stream, onLevel) => {
    try {
      const src = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 2048
      src.connect(analyser)
      sourceNodesRef.current.push(src)
      analysersRef.current.push({ analyser, data: new Uint8Array(analyser.fftSize), onLevel })
    } catch (e) {
      console.warn('[recording] analyser attach failed', { name: e?.name, message: e?.message })
    }
  }

  const startMeters = () => {
    const tick = () => {
      analysersRef.current.forEach(({ analyser, data, onLevel }) => {
        try {
          analyser.getByteTimeDomainData(data)
          let sum = 0
          for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v }
          onLevel(Math.sqrt(sum / data.length))
        } catch {}
      })
      rafRef.current = requestAnimationFrame(tick)
    }
    tick()
  }

  const logDiagnostics = (extra = {}) => {
    const mic = trackInfo(micStreamRef.current, 'microphone')
    const tab = trackInfo(tabStreamRef.current, 'tab')
    const mixed = trackInfo(mixedStreamRef.current, 'mixed')
    const info = {
      mic, tab, mixed,
      audioContextState: audioCtxRef.current?.state || 'none',
      recorderState: recorderRef.current?.state || 'none',
      recorderMime: recorderRef.current?.mimeType || extra.selectedMime || '',
      ...extra,
    }
    console.info('[recording] diagnostics', info)
    return info
  }

  const start = async () => {
    if (startingRef.current || recording) {
      console.warn('[recording] start ignored', { starting: startingRef.current, recording, recorderState: recorderRef.current?.state })
      return
    }
    setError('')
    if (!title.trim()) return setError('Meeting title is required.')
    if (typeof MediaRecorder === 'undefined') return setError('MediaRecorder is not supported in this browser.')

    startingRef.current = true
    cleanupRecording({ preservePreview: true })
    chunksRef.current = []
    lastBlobRef.current = null
    setMicLevel(0); setTabLevel(0); setMicStatus('starting'); setTabStatus(tabAudioEnabled ? 'starting' : 'disabled')

    try {
      const mic = await navigator.mediaDevices.getUserMedia({ audio: selectedMic ? { deviceId: { exact: selectedMic } } : true })
      micStreamRef.current = mic
      if (!hasLiveAudioTrack(mic)) throw new Error('Microphone stream has no live audio track.')

      let tab = null
      if (tabAudioEnabled) {
        if (!navigator.mediaDevices?.getDisplayMedia) {
          setTabStatus('unsupported')
          setError('Browser tab audio capture is not supported in this browser.')
        } else {
          try {
            tab = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true })
            const tabAudio = audioOnlyStream(tab)
            if (!tabAudio) {
              stopTracks(tab)
              setTabStatus('missing-audio')
              setError('Tab share selected but no tab audio detected. Enable "Share tab audio" in the browser picker.')
            } else {
              // Stop video tracks immediately; we only record audio.
              (tab.getVideoTracks?.() || []).forEach(t => { try { t.stop() } catch {} })
              tabStreamRef.current = tabAudio
              tabAudio.getAudioTracks().forEach(t => { t.onended = () => setTabStatus('disconnected') })
              setTabStatus('ready')
            }
          } catch (e) {
            setTabStatus(e?.name === 'NotAllowedError' ? 'denied' : 'error')
            console.warn('[recording] tab capture failed', { name: e?.name, message: e?.message })
          }
        }
      }

      const Ctx = window.AudioContext || window.webkitAudioContext
      if (!Ctx) throw new Error('AudioContext is not supported in this browser.')
      const ctx = new Ctx()
      audioCtxRef.current = ctx
      await ctx.resume()
      if (ctx.state !== 'running') throw new Error(`AudioContext could not start (state: ${ctx.state}).`)

      const destination = ctx.createMediaStreamDestination()
      destinationRef.current = destination

      const micAudio = audioOnlyStream(micStreamRef.current)
      if (!micAudio) throw new Error('Microphone audio track became unavailable.')
      attachAnalyser(ctx, micAudio, setMicLevel)
      const micSource = ctx.createMediaStreamSource(micAudio)
      micSource.connect(destination)
      sourceNodesRef.current.push(micSource)
      setMicStatus('ready')

      const tabAudio = audioOnlyStream(tabStreamRef.current)
      if (tabAudio && hasLiveAudioTrack(tabAudio)) {
        attachAnalyser(ctx, tabAudio, setTabLevel)
        const tabSource = ctx.createMediaStreamSource(tabAudio)
        tabSource.connect(destination)
        sourceNodesRef.current.push(tabSource)
        setTabStatus('ready')
      } else if (tabAudioEnabled && tabStreamRef.current) {
        setTabStatus('missing-audio')
      }

      const mixed = destination.stream
      mixedStreamRef.current = mixed
      if (!hasLiveAudioTrack(mixed)) {
        logDiagnostics({ reason: 'no-live-mixed-track' })
        throw new Error('No live audio tracks available for recording. Check microphone/tab audio permissions and selection.')
      }

      const { mimeType } = pickRecorderMime()
      let recorder
      try {
        ({ recorder } = createMediaRecorder(mixed, mimeType))
      } catch (e) {
        logDiagnostics({ reason: 'recorder-create-failed', selectedMime: mimeType, error: { name: e?.name, message: e?.message } })
        throw e
      }
      recorderRef.current = recorder
      recorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) chunksRef.current.push(e.data) }
      recorder.onerror = (ev) => {
        const err = ev?.error || ev
        console.error('[recording] MediaRecorder error', { name: err?.name, message: err?.message, recorderState: recorder?.state })
        setError(`Recording error: ${err?.message || err?.name || 'unknown error'}`)
      }
      recorder.onstop = () => {
        const type = recorder.mimeType || chunksRef.current[0]?.type || 'audio/webm'
        const blob = new Blob(chunksRef.current, { type })
        lastBlobRef.current = blob
        setLastBlob(blob)
        const url = URL.createObjectURL(blob)
        setAudioUrl(old => { if (old) { try { URL.revokeObjectURL(old) } catch {} } return url })
        logDiagnostics({ event: 'stop', blobSize: blob.size, blobType: blob.type })
      }

      if (recorder.state !== 'inactive') throw new Error(`Recorder is not inactive (state: ${recorder.state}).`)
      try {
        recorder.start(1000)
      } catch (e) {
        logDiagnostics({ reason: 'recorder-start-failed', selectedMime: recorder.mimeType, error: { name: e?.name, message: e?.message } })
        throw new Error(`Failed to start MediaRecorder: ${e?.message || e?.name || 'unknown error'}`)
      }

      startRef.current = Date.now(); setElapsed(0)
      clearInterval(timerRef.current)
      timerRef.current = setInterval(() => setElapsed(Math.floor((Date.now() - startRef.current) / 1000)), 500)
      startMeters()
      setRecording(true)
      setMicStatus('recording')
      if (hasLiveAudioTrack(tabStreamRef.current)) setTabStatus('recording')
      logDiagnostics({ event: 'start', selectedMime: recorder.mimeType })
    } catch (e) {
      console.error('[recording] start failed', { name: e?.name, message: e?.message })
      setError(e?.message || 'Recording failed to start')
      setRecording(false)
      setMicStatus(s => s === 'starting' ? 'error' : s)
      setTabStatus(s => s === 'starting' ? 'error' : s)
      cleanupRecording({ preservePreview: true })
    } finally {
      startingRef.current = false
    }
  }

  const stop = () => {
    clearInterval(timerRef.current); timerRef.current = null
    if (rafRef.current) cancelAnimationFrame(rafRef.current); rafRef.current = null
    const rec = recorderRef.current
    try {
      if (rec && rec.state !== 'inactive') rec.stop()
    } catch (e) {
      console.warn('[recording] stop failed', { name: e?.name, message: e?.message })
    }
    setRecording(false)
    setMicStatus(s => s === 'recording' ? 'stopped' : s)
    setTabStatus(s => s === 'recording' ? 'stopped' : s)
    setTimeout(() => {
      stopTracks(micStreamRef.current); stopTracks(tabStreamRef.current); stopTracks(mixedStreamRef.current)
    }, 800)
  }

  const discard = () => {
    if (recording) stop()
    cleanupRecording({ preservePreview: false })
    setLastBlob(null); lastBlobRef.current = null
    setElapsed(0); setMicLevel(0); setTabLevel(0); setMicStatus('idle'); setTabStatus(tabAudioEnabled ? 'idle' : 'idle')
    setAudioUrl(''); audioUrlRef.current = ''
    setError('')
  }

  const upload = async () => {
    const blob = lastBlobRef.current || lastBlob
    if (!blob) return setError('Record audio before uploading.')
    if (tabAudioEnabled && !hasLiveAudioTrack(tabStreamRef.current) && !chunksRef.current.length) {
      // non-blocking warning already shown; keep confirm
    }
    if (tabAudioEnabled && tabStatus !== 'recording' && tabStatus !== 'stopped' && tabStatus !== 'ready') {
      if (!confirm('Tab/system browser audio may be missing. This may be a microphone-only recording missing remote participants. Continue?')) return
    }
    setUploading(true); setError('')
    try {
      const type = blob.type || recorderRef.current?.mimeType || 'audio/webm'
      const file = new File([blob], `recording.${extForMime(type)}`, { type })
      const meeting = await api.uploadRecording({ title: title.trim(), userId: session.profileId, file, durationMs: elapsed * 1000, notes: notes.trim() || undefined })
      nav(`/meetings/${meeting.id}`)
    } catch (e) { setError(e.message) }
    finally { setUploading(false) }
  }

  const fmt = (s) => `${String(Math.floor(s/3600)).padStart(2,'0')}:${String(Math.floor((s%3600)/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`
  const healthWarning = recording && micStatus !== 'recording' ? 'Microphone is not recording.' : (recording && tabAudioEnabled && tabStatus !== 'recording' ? 'Browser/tab audio missing; remote participants may be missing.' : '')

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="eyebrow">Record</div>
          <h1>New meeting recording</h1>
          <p className="muted">Captures microphone audio in browser. For complete Google Meet capture, also enable browser tab audio sharing or use the local Windows capture agent for true WASAPI system+mic capture.</p>
        </div>
      </div>
      {error && <div className="alert error">{error}</div>}
      {healthWarning && <div className="alert warning">{healthWarning}</div>}
      <div className="grid-2">
        <div className="card">
          <label className="field"><span>Meeting title</span><input value={title} onChange={e => setTitle(e.target.value)} placeholder="Weekly planning" disabled={recording || uploading} /></label>
          <label className="field"><span>Microphone</span>
            <select value={selectedMic} onChange={e => setSelectedMic(e.target.value)} disabled={recording || uploading}>
              {micDevices.length === 0 ? <option value="">Default microphone</option> : micDevices.map((d, i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Microphone ${i+1}`}</option>)}
            </select>
          </label>
          <label className="field" style={{ flexDirection: 'row', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={tabAudioEnabled} onChange={e => setTabAudioEnabled(e.target.checked)} disabled={recording || uploading} />
            <span>Capture browser tab audio (Google Meet tab via screen/tab share; fallback for system audio)</span>
          </label>
          <label className="field"><span>Notes (optional)</span><textarea value={notes} onChange={e => setNotes(e.target.value)} rows="4" disabled={recording || uploading} /></label>

          <div className="card" style={{ padding: 12, marginBottom: 12 }}>
            <div className="section-title">Capture health</div>
            <Meter value={micLevel} label={`Microphone (${micStatus})`} />
            <Meter value={tabLevel} label={`Browser tab audio (${tabStatus})`} />
            <div className="muted small">Selected mic: {micDevices.find(d=>d.deviceId===selectedMic)?.label || 'Default'}</div>
            <div className="muted small">Mixed output is recorded as a single audio stream; tab audio is optional fallback, not true WASAPI system capture.</div>
          </div>

          <div className="record-status">
            <div className={`record-dot ${recording ? 'live' : ''}`}></div>
            <div>
              <div className="record-state">{recording ? 'Recording' : lastBlob ? 'Ready to upload' : 'Idle'}</div>
              <div className="record-time">{fmt(elapsed)}</div>
            </div>
          </div>
          <div className="actions-row">
            {!recording ? <button className="btn primary" onClick={start} disabled={uploading || startingRef.current}>Start recording</button> : <button className="btn danger" onClick={stop}>Stop</button>}
            <button className="btn" onClick={discard} disabled={recording || uploading || (!lastBlob && elapsed===0)}>Discard</button>
            <button className="btn primary" onClick={upload} disabled={recording || uploading || !lastBlob}>{uploading ? 'Uploading...' : 'Upload to server'}</button>
          </div>
          <div className="fineprint">Consent required. Browser recordings are uploaded and transcoded on server when needed. Microphone-only recordings may miss remote participants.</div>
        </div>
        <div className="card">
          <div className="section-title">Preview</div>
          {audioUrl ? <audio controls src={audioUrl} className="audio-player" /> : <div className="empty">No recording yet.</div>}
          <div className="meta-list">
            <div><span className="meta-label">Can record</span><div>Yes</div></div>
            <div><span className="meta-label">Profile</span><div>{session?.displayName}</div></div>
            <div><span className="meta-label">Upload format</span><div>{lastBlob?.type || 'audio/webm'}</div></div>
            <div><span className="meta-label">Capture mode</span><div>{tabStreamRef.current || tabStatus === 'recording' || tabStatus === 'stopped' ? 'mic + tab audio (mixed)' : (lastBlob ? 'microphone' : '—')}</div></div>
          </div>
        </div>
      </div>
    </div>
  )
}
