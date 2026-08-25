import React, { useState } from 'react'
import api, { setApiKey, setAuthed } from '../api.js'

export default function Login() {
  const [key, setKey] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setApiKey(key.trim())
    try {
      // Any authenticated endpoint works as a key check.
      await api.get('/api/v1/heartbeat/status')
      setAuthed(true)
      // Full reload so the app shell re-evaluates auth state (nav menu etc.)
      window.location.hash = '#/'
      window.location.reload()
    } catch (err) {
      setApiKey('')
      if (err.response?.status === 401) {
        setError('Invalid API key. Ask your admin for the RUNNER_API_KEY value.')
      } else {
        setError('Cannot reach the backend. Is the API running?')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{
      minHeight: '70vh', display: 'flex',
      alignItems: 'center', justifyContent: 'center',
    }}>
      <div className="card" style={{ width: '420px', maxWidth: '90vw' }}>
        <h1 style={{ marginBottom: '0.5rem' }}>Founder OS</h1>
        <p style={{ color: '#666', marginBottom: '1.5rem' }}>
          Enter the team API key to sign in. In development with no
          <code> RUNNER_API_KEY</code> set on the server, leave it blank.
        </p>

        {error && (
          <div style={{
            padding: '0.75rem', marginBottom: '1rem', borderRadius: '6px',
            backgroundColor: '#f8d7da', color: '#721c24',
          }}>
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>API Key</label>
            <input
              type="password"
              value={key}
              onChange={e => setKey(e.target.value)}
              placeholder="team API key (blank in dev mode)"
              autoFocus
            />
          </div>
          <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={busy}>
            {busy ? 'Checking…' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  )
}
