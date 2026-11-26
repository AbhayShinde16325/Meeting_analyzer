import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useApp } from '../App'
import { formatDate, formatDurationMs, statusLabel } from '../utils/format'

const TABS = ['summary', 'transcript', 'action-items', 'speakers', 'attachments', 'metrics', 'jobs']

export default function MeetingDetailsPage() {
  const { meetingId } = useParams()
  const nav = useNavigate()
  const { system } = useApp()
  const [data, setData] = useState({ meeting: null, files: [], transcripts: [], summary: null, actionItems: [], jobs: [], speakers: [], metrics: null, profiles: [] })
  const [tab, setTab] = useState('summary')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [newSpeakerNames, setNewSpeakerNames] = useState({})
  const [renameProfile, setRenameProfile] = useState({})
  const [sampleAudio, setSampleAudio] = useState({})
  const [profileRefs, setProfileRefs] = useState({})
  const [refsOpen, setRefsOpen] = useState({})
  const canManage = true

  const load = async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const [meeting, files, transcripts, summary, actionItems, jobs, speakers, metrics, profiles] = await Promise.all([
        api.getMeeting(meetingId), api.listFiles(meetingId), api.listTranscripts(meetingId), api.getSummary(meetingId),
        api.listActionItems(meetingId), api.listJobs(meetingId), api.listMeetingSpeakers(meetingId).catch(() => []), api.getMetrics(meetingId).catch(() => null),
        api.listSpeakerProfiles().catch(() => []),
      ])
      setData({ meeting, files, transcripts, summary, actionItems, jobs, speakers, metrics, profiles })
      setError('')
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [meetingId])

  const primary = useMemo(() => data.transcripts.find(t => t.is_primary) || data.transcripts[0], [data.transcripts])
  const audio = useMemo(() => data.files.find(f => f.kind === 'original_audio_48k') || data.files.find(f => (f.kind || '').startsWith('original_audio')), [data.files])
  const attachments = useMemo(() => data.files.filter(f => f.kind === 'attachment'), [data.files])
  const activeJob = data.jobs.find(j => ['queued', 'running'].includes(j.status))
  const profilingJob = useMemo(() => data.jobs.find(j => j.job_type === 'profiling') || null, [data.jobs])
  const lastProfilingStatus = useRef(null)

  useEffect(() => {
    const active = data.jobs?.some(j => ['queued', 'running'].includes(j.status)) || ['recorded', 'transcribed', 'analyzing'].includes(data.meeting?.status)
    if (!active) return
    const t = setInterval(() => load(true), 4000)
    return () => clearInterval(t)
  }, [data.jobs, data.meeting?.status, meetingId])
  useEffect(() => {
    const s = profilingJob?.status || null
    const prev = lastProfilingStatus.current
    if (prev && ['queued', 'running'].includes(prev) && s && ['succeeded', 'failed', 'cancelled'].includes(s)) {
      load(true)
    }
    lastProfilingStatus.current = s
  }, [profilingJob?.status, profilingJob?.id])

  const act = async (name, fn) => { setBusy(name); setError(''); try { await fn(); await load(true) } catch (e) { setError(e.message) } finally { setBusy('') } }

  const onUploadAttachment = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    await act('attach', () => api.uploadAttachment(meetingId, file))
    e.target.value = ''
  }

  const onAssignSpeaker = async (localLabel, fields) => {
    await act(`speaker:${localLabel}`, () => api.assignMeetingSpeaker(meetingId, localLabel, fields))
  }

  const fmtPct = (v) => (v === null || v === undefined ? '—' : `${Math.round(Number(v) * 100)}%`)
  const fmtNum = (v) => (v === null || v === undefined ? '—' : String(v))
  const fmtDur = (job) => {
    if (!job?.started_at) return '—'
    const start = new Date(job.started_at).getTime()
    const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now()
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—'
    return `${((end - start) / 1000).toFixed(1)}s`
  }
  const decisionLabel = (d) => ({
    matched: 'Matched',
    below_threshold: 'Below threshold',
    ambiguous: 'Ambiguous',
    no_reference: 'No reference',
    no_embedding: 'No embedding',
    error: 'Error',
  }[d] || (d ? d : '—'))

  const loadRefs = async (profileId) => {
    try {
      const refs = await api.listProfileReferences(profileId)
      setProfileRefs(s => ({ ...s, [profileId]: refs }))
    } catch (e) { setError(e.message) }
  }
  const toggleRefs = async (profileId) => {
    const next = !refsOpen[profileId]
    setRefsOpen(s => ({ ...s, [profileId]: next }))
    if (next && !profileRefs[profileId]) await loadRefs(profileId)
  }

  const speakerDisplay = (segSpeaker) => {
    if (!segSpeaker) return { name: 'Unknown Speaker', status: 'unknown' }
    const match = data.speakers.find(s => s.local_label === segSpeaker)
    if (match?.status === 'confirmed' && match?.profile_name) return { name: match.profile_name, status: 'identified' }
    if (match?.status === 'suggested' && match?.profile_name) return { name: `${match.profile_name} (suggested)`, status: 'suggested' }
    // Stable Unknown Speaker N based on sorted diarization labels
    const labels = Array.from(new Set((primary?.segments || []).map(s => s.speaker).filter(Boolean))).sort()
    const idx = labels.indexOf(segSpeaker)
    return { name: idx >= 0 ? `Unknown Speaker ${idx + 1}` : segSpeaker, status: match?.status || 'unknown' }
  }

  if (loading) return <div className="page"><div className="empty card">Loading meeting...</div></div>
  if (!data.meeting) {
    return <div className="page"><div className="alert error">{error || 'Meeting could not be loaded.'}</div><button className="btn" onClick={() => nav('/')}>Back to meetings</button></div>
  }
  const m = data.meeting
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <button className="btn ghost" onClick={() => nav('/')}>← Back</button>
          <div className="eyebrow">Meeting details</div>
          <h1>{m.title}</h1>
          <p className="muted">{formatDate(m.recorded_at)} • {formatDurationMs(m.duration_ms)} • <span className={`badge status-${m.status}`}>{statusLabel(m.status)}</span></p>
        </div>
        <div className="header-actions wrap">
          <button className="btn" disabled={!canManage || busy === 'transcribe'} onClick={() => act('transcribe', () => api.transcribe(meetingId))}>{busy === 'transcribe' ? '...' : 'Transcribe'}</button>
          <button className="btn" disabled={!canManage || busy === 'analyse' || !primary} title={!primary ? 'Transcribe first' : 'Extract summary and action items'} onClick={() => act('analyse', () => api.analyse(meetingId))}>{busy === 'analyse' ? '...' : 'Analyse'}</button>
          <button className="btn" disabled={!canManage || busy === 'profile'} onClick={() => act('profile', () => api.requestProfiling(meetingId))}>{busy === 'profile' ? '...' : 'Profile speakers'}</button>
          <button className="btn" disabled={!canManage || !activeJob || busy === 'cancel'} onClick={() => act('cancel', () => api.cancelJob(meetingId, activeJob.id))}>Cancel job</button>
          <button className="btn" onClick={() => api.exportMeeting(meetingId).catch(e => setError(e.message))}>Export ZIP</button>
          {canManage && <button className="btn danger" disabled={busy === 'delete'} onClick={() => { if (confirm(`Delete "${m.title}" permanently?`)) act('delete', async () => { await api.deleteMeeting(meetingId); nav('/') }) }}>Delete</button>}
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {activeJob && <div className="alert info">Job {activeJob.job_type}: {activeJob.status} {Math.round((activeJob.progress || 0) * 100)}% {activeJob.message ? `— ${activeJob.message}` : ''}</div>}

      <div className="card">
        <div className="section-title">Audio</div>
        {audio ? <audio controls className="audio-player" src={api.audioUrl(audio.id)} /> : <div className="empty">No audio file available.</div>}
      </div>

      <div className="tabs">
        {TABS.map(t => (
          <button key={t} className={tab === t ? 'tab active' : 'tab'} onClick={() => setTab(t)}>{t.replace('-', ' ')}</button>
        ))}
      </div>

      {tab === 'summary' && <div className="card">
        {!data.summary ? <div className="empty">No summary yet. Run transcription and analysis.</div> : <>
          <div className="section-title">Overview</div>
          <p>{data.summary.overview}</p>
          {['key_points', 'decisions', 'open_questions', 'risks_or_blockers', 'follow_ups'].map(k => (
            <div key={k} className="subsection"><div className="sub-title">{k.replaceAll('_', ' ')}</div>
              <ul>{(data.summary.payload?.[k] || []).length ? data.summary.payload[k].map((v, i) => <li key={i}>{String(v)}</li>) : <li className="muted">None</li>}</ul>
            </div>
          ))}
        </>}
      </div>}

      {tab === 'transcript' && <div className="card">
        {!primary ? <div className="empty">No transcript yet.</div> : <>
          <div className="muted small">Provider: {primary.provider} • Model: {primary.model} {primary.fallback_from ? `• Fallback from ${primary.fallback_from}` : ''}</div>
          <div className="transcript">{(primary.segments || []).length ? primary.segments.map((s, i) => {
            const d = speakerDisplay(s.speaker)
            return (
            <div key={i} className="segment">
              <span className="seg-time">{fmtSec(s.start)}–{fmtSec(s.end)}</span>
              <span className="seg-speaker">{d.name}</span>
              <span className="muted small" style={{marginRight:8}}>[{d.status}{s.language || s.language_code ? `, ${s.language || s.language_code}` : ''}]</span>
              <span>{s.text}</span>
            </div>
          )}) : <pre className="prewrap">{primary.text}</pre>}</div>
        </>}
      </div>}

      {tab === 'action-items' && <div className="card">
        <div className="section-title">Action items</div>
        <p className="muted small">Extracted from transcript analysis. Run Analyse after transcription; requires analysis backend configured on server.</p>
        <div className="table-wrap"><table><thead><tr><th>Task</th><th>Owner</th><th>Deadline</th><th>Status</th><th>Evidence</th><th></th></tr></thead><tbody>
          {data.actionItems.length === 0 ? <tr><td colSpan="6" className="empty">{primary ? 'No action items extracted. Click Analyse (and check analysis backend config/logs).' : 'No transcript yet. Transcribe, then Analyse.'}</td></tr> : data.actionItems.map(item => (
            <tr key={item.id}><td>{item.task}</td><td>{item.owner || '—'}</td><td>{item.deadline || '—'}</td><td><span className={`badge status-${item.status}`}>{item.status}</span></td><td className="small">{item.evidence || '—'}</td>
              <td className="actions">{canManage && <button className="btn small" onClick={() => act(`ai:${item.id}`, () => api.patchActionItem(item.id, { status: item.status === 'open' ? 'done' : 'open' }))}>{item.status === 'open' ? 'Mark done' : 'Reopen'}</button>}</td></tr>
          ))}
        </tbody></table></div>
      </div>}

      {tab === 'speakers' && <div className="card">
        <div className="section-title">Speaker review</div>
        <p className="muted small">Reference-audio-based profiling. Clear, unambiguous matches at or above threshold are auto-confirmed; ambiguous/below-threshold/no-reference speakers remain Unknown. Manual assignments are preserved.</p>
        <div className="subsection" style={{ marginBottom: 16 }}>
          <div className="sub-title">Profiling job</div>
          {!profilingJob ? <div className="muted small">No profiling job yet.</div> : <div className="muted small">
            Status: <span className={`badge status-${profilingJob.status}`}>{profilingJob.status}</span>
            {' • '}Started: {profilingJob.started_at ? formatDate(profilingJob.started_at) : '—'}
            {' • '}Duration: {fmtDur(profilingJob)}
            {' • '}Error: {profilingJob.error || '—'}
          </div>}
        </div>
        <div className="speaker-list">
          {data.speakers.length === 0 ? <div className="empty">No speaker data. Run transcription and speaker profiling.</div> : data.speakers.map(sp => {
            const candidates = sp.candidates || []
            const selected = sp.profile_id || ''
            return (
              <div key={sp.local_label} className="speaker-row" style={{ alignItems: 'flex-start' }}>
                <div style={{ minWidth: 320 }}>
                  <div className="speaker-name">{sp.local_label} {sp.profile_name ? <span className="muted">→ {sp.profile_name}</span> : <span className="muted">→ Unknown</span>}</div>
                  <div className="muted small">
                    Segments: {fmtNum(sp.segment_count)} • Speech: {(sp.speech_seconds || 0).toFixed(1)}s • Assignment: {sp.status || 'unassigned'}{sp.origin ? ` (${sp.origin})` : ''}
                  </div>
                  <div className="muted small">
                    Reference available: {sp.reference_available ? 'Yes' : 'No'} • Best ref: {sp.best_profile_name || '—'} • Similarity: {fmtPct(sp.best_score)} • Threshold: {fmtPct(sp.threshold)}
                  </div>
                  <div className="muted small">
                    Decision: {decisionLabel(sp.decision)} • Profiles evaluated: {fmtNum(sp.profiles_evaluated)} • Evaluated: {sp.evaluated_at ? formatDate(sp.evaluated_at) : '—'}
                  </div>
                  <div className="muted small">
                    Job: {sp.job_status || profilingJob?.status || '—'} • Processing time: {fmtDur(profilingJob)} • Error: {sp.job_error || profilingJob?.error || '—'}
                  </div>
                  {(sp.same_voice_labels || []).length > 0 && <div className="muted small">Same voice as: {sp.same_voice_labels.join(', ')}</div>}
                </div>
                <div className="speaker-actions" style={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                  <select disabled={!canManage || busy === `speaker:${sp.local_label}`} value={selected} onChange={e => onAssignSpeaker(sp.local_label, { profile_id: e.target.value || null, status: 'confirmed' })}>
                    <option value="">Select profile...</option>
                    {data.profiles.map(p => <option key={p.id} value={p.id}>{p.display_name}</option>)}
                    {candidates.filter(c => !data.profiles.some(p => p.id === c.profile_id)).map(c => <option key={c.profile_id} value={c.profile_id}>{c.display_name} ({Math.round((c.confidence || 0) * 100)}%)</option>)}
                  </select>
                  <input placeholder="New profile name" value={newSpeakerNames[sp.local_label] || ''} disabled={!canManage} onChange={e => setNewSpeakerNames(s => ({ ...s, [sp.local_label]: e.target.value }))} />
                  <button className="btn small" disabled={!canManage || !(newSpeakerNames[sp.local_label] || '').trim()} onClick={() => onAssignSpeaker(sp.local_label, { new_profile_name: newSpeakerNames[sp.local_label].trim(), status: 'confirmed' })}>Create & assign</button>
                  <button className="btn small" disabled={!canManage} onClick={() => onAssignSpeaker(sp.local_label, { status: 'rejected' })}>Reject</button>
                  <button className="btn small" disabled={!canManage} onClick={() => onAssignSpeaker(sp.local_label, { status: 'suggested' })}>Reset</button>
                </div>
              </div>
            )
          })}
        </div>
        <div className="subsection" style={{ marginTop: 24 }}>
          <div className="sub-title">Speaker profiles</div>
          <p className="muted small">Upload explicit reference audio samples for each profile. Only reference samples are used for matching; meeting embeddings are diagnostics only. Profiles with no reference cannot be matched.</p>
          <div className="table-wrap"><table><thead><tr><th>Name</th><th>Reference samples</th><th>Updated</th><th>Enroll sample</th><th></th></tr></thead><tbody>
            {data.profiles.length === 0 ? <tr><td colSpan="5" className="empty">No profiles.</td></tr> : data.profiles.map(p => (
              <React.Fragment key={p.id}>
                <tr>
                  <td>{p.display_name}{(p.embedding_count ?? 0) === 0 ? <div className="muted small">No reference uploaded</div> : null}</td>
                  <td>{p.embedding_count ?? '—'}</td>
                  <td className="small">{formatDate(p.updated_at || p.created_at)}</td>
                  <td>{canManage && <input type="file" accept="audio/*" onChange={e => setSampleAudio(s => ({ ...s, [p.id]: e.target.files?.[0] || null }))} />}</td>
                  <td className="actions">{canManage && <>
                    <button className="btn small" disabled={!sampleAudio[p.id] || busy===`enroll:${p.id}`} onClick={() => act(`enroll:${p.id}`, async () => { await api.enrollSpeakerProfile(p.id, sampleAudio[p.id]); setSampleAudio(s => ({ ...s, [p.id]: null })); if (refsOpen[p.id]) await loadRefs(p.id) })}>Enroll</button>
                    <button className="btn small" onClick={() => toggleRefs(p.id)}>{refsOpen[p.id] ? 'Hide refs' : 'References'}</button>
                    <input style={{ width: 140, marginRight: 6 }} placeholder="Rename" value={renameProfile[p.id] || ''} onChange={e => setRenameProfile(s => ({ ...s, [p.id]: e.target.value }))} />
                    <button className="btn small" disabled={!(renameProfile[p.id] || '').trim()} onClick={() => act(`rp:${p.id}`, async () => { await api.renameSpeakerProfile(p.id, renameProfile[p.id].trim()); setRenameProfile(s => ({ ...s, [p.id]: '' })) })}>Rename</button>
                    <button className="btn small danger" onClick={() => { if (confirm(`Delete profile "${p.display_name}"?`)) act(`dp:${p.id}`, () => api.deleteSpeakerProfile(p.id)) }}>Delete</button>
                  </>}</td>
                </tr>
                {refsOpen[p.id] && <tr><td colSpan="5">
                  <div className="muted small" style={{ marginBottom: 6 }}>Reference samples</div>
                  {!(profileRefs[p.id] || []).length ? <div className="muted small">No reference samples.</div> : <div className="table-wrap"><table><thead><tr><th>File</th><th>Duration</th><th>Provider</th><th>Created</th><th></th></tr></thead><tbody>
                    {(profileRefs[p.id] || []).map(r => <tr key={r.id}><td>{r.original_filename || '—'}</td><td>{r.source_seconds != null ? `${Number(r.source_seconds).toFixed(1)}s` : '—'}</td><td>{r.provider || '—'} {r.model || ''}</td><td className="small">{formatDate(r.created_at)}</td><td>{canManage && <button className="btn small danger" onClick={() => act(`dr:${r.id}`, async () => { await api.deleteProfileReference(p.id, r.id); await loadRefs(p.id) })}>Delete</button>}</td></tr>)}
                  </tbody></table></div>}
                </td></tr>}
              </React.Fragment>
            ))}
          </tbody></table></div>
        </div>
      </div>}

      {tab === 'attachments' && <div className="card">
        <div className="actions-row"><label className={`btn ${!canManage ? 'disabled' : ''}`}>Upload attachment<input type="file" hidden onChange={onUploadAttachment} disabled={!canManage} /></label></div>
        <div className="table-wrap"><table><thead><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead><tbody>
          {attachments.length === 0 ? <tr><td colSpan="4" className="empty">No attachments.</td></tr> : attachments.map(f => (
            <tr key={f.id}><td>{f.original_filename}</td><td>{f.mime_type}</td><td>{Math.round((f.size_bytes || 0) / 1024)} KB</td>
              <td className="actions"><button className="btn small" onClick={() => api.downloadFile(f.id, f.original_filename || 'attachment').catch(e => setError(e.message))}>Download</button>{canManage && <button className="btn small danger" onClick={() => act(`del:${f.id}`, () => api.deleteAttachment(f.id))}>Delete</button>}</td></tr>
          ))}
        </tbody></table></div>
      </div>}

      {tab === 'metrics' && <div className="card">{!data.metrics ? <div className="empty">No metrics yet.</div> : <pre className="prewrap">{JSON.stringify(data.metrics.metrics || data.metrics, null, 2)}</pre>}</div>}
      {tab === 'jobs' && <div className="card"><div className="table-wrap"><table><thead><tr><th>Type</th><th>Status</th><th>Progress</th><th>Message</th><th>Updated</th></tr></thead><tbody>
        {data.jobs.length === 0 ? <tr><td colSpan="5" className="empty">No jobs.</td></tr> : data.jobs.map(j => (
          <tr key={j.id}><td>{j.job_type}</td><td><span className={`badge status-${j.status}`}>{j.status}</span></td><td>{Math.round((j.progress || 0) * 100)}%</td><td className="small">{j.message || j.error || '—'}</td><td className="small">{formatDate(j.finished_at || j.created_at)}</td></tr>
        ))}
      </tbody></table></div></div>}
    </div>
  )
}

function fmtSec(s) { const n = Math.max(0, Math.floor(Number(s) || 0)); return `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}` }
