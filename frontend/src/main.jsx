import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const FILTER_DEFAULTS = { server_ip: '', instance: '', filename: '', search: '', start: '', end: '' };
const BROWSER_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'browser local time';
const LOG_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  fractionalSecondDigits: 3, hour12: false, timeZoneName: 'short',
});

function groupBySource(logs) {
  const groups = [];
  for (const log of logs) {
    const source = JSON.stringify([log.server_ip || '', log.instance || '', log.filename || '', log.job || '']);
    const last = groups[groups.length - 1];
    if (last && last.source === source) {
      last.logs.push(log);
    } else {
      groups.push({ source, server_ip: log.server_ip, instance: log.instance, filename: log.filename, job: log.job, logs: [log] });
    }
  }
  return groups;
}

function formatTimestamp(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : LOG_TIME_FORMATTER.format(date);
}

function toUtc(value) {
  return value ? new Date(value).toISOString() : '';
}

function LogViewer({ user, onLogout }) {
  const [filters, setFilters] = useState(FILTER_DEFAULTS);
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [error, setError] = useState('');
  const [lastUpdated, setLastUpdated] = useState(null);
  const [limit, setLimit] = useState('200');
  const [offset, setOffset] = useState(0);
  const [count, setCount] = useState(0);
  const requestRef = useRef(null);
  const groupedLogs = groupBySource(logs);

  const makeParams = useCallback((includePaging = true) => {
    const params = new URLSearchParams();
    for (const key of ['server_ip', 'instance', 'filename', 'search']) {
      if (filters[key].trim()) params.set(key, filters[key].trim());
    }
    if (filters.start) params.set('start', toUtc(filters.start));
    if (filters.end) params.set('end', toUtc(filters.end));
    if (includePaging) {
      params.set('limit', limit);
      params.set('offset', String(offset));
    }
    return params;
  }, [filters, limit, offset]);

  const loadLogs = useCallback(async (showSpinner = true) => {
    if (Boolean(filters.start) !== Boolean(filters.end)) {
      setError('Select both start and end times, or clear both to view the last 15 minutes.');
      return;
    }
    if (filters.start && new Date(filters.start) >= new Date(filters.end)) {
      setError('Start time must be earlier than end time.');
      return;
    }
    if (requestRef.current) requestRef.current.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    if (showSpinner) setLoading(true);
    setError('');
    try {
      const response = await fetch(`/api/logs/query?${makeParams(true).toString()}`, { signal: controller.signal });
      if (response.status === 401) { window.location.reload(); return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      setLogs(data.logs || []);
      setCount(data.count || 0);
      setLastUpdated(new Date());
    } catch (err) {
      if (err.name !== 'AbortError') setError(err.message || 'Could not retrieve logs.');
    } finally {
      if (!controller.signal.aborted && showSpinner) setLoading(false);
    }
  }, [filters, makeParams]);

  useEffect(() => {
    loadLogs(true);
    return () => requestRef.current?.abort();
  }, []); // Initial load only; filter changes are applied by Search.

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => loadLogs(false), 5000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadLogs]);

  function updateFilter(key, value) {
    setFilters(previous => ({ ...previous, [key]: value }));
  }

  function submitSearch(event) {
    event.preventDefault();
    setOffset(0);
    // Query immediately using the first page, without waiting for the offset state update.
    const params = new URLSearchParams();
    for (const key of ['server_ip', 'instance', 'filename', 'search']) {
      if (filters[key].trim()) params.set(key, filters[key].trim());
    }
    if (filters.start) params.set('start', toUtc(filters.start));
    if (filters.end) params.set('end', toUtc(filters.end));
    params.set('limit', limit);
    params.set('offset', '0');
    if (Boolean(filters.start) !== Boolean(filters.end)) {
      setError('Select both start and end times, or clear both to view the last 15 minutes.');
      return;
    }
    if (filters.start && new Date(filters.start) >= new Date(filters.end)) {
      setError('Start time must be earlier than end time.');
      return;
    }
    if (requestRef.current) requestRef.current.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError('');
    fetch(`/api/logs/query?${params.toString()}`, { signal: controller.signal })
      .then(async response => {
        if (response.status === 401) { window.location.reload(); throw new Error('Session expired; sign in again.'); }
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        setLogs(data.logs || []); setCount(data.count || 0); setLastUpdated(new Date());
      })
      .catch(err => { if (err.name !== 'AbortError') setError(err.message || 'Could not retrieve logs.'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
  }

  function downloadLogs() {
    if (!filters.start || !filters.end) {
      setError('Choose both start and end times before exporting.');
      return;
    }
    if (new Date(filters.start) >= new Date(filters.end)) {
      setError('Start time must be earlier than end time.');
      return;
    }
    // Let the browser download the response directly instead of buffering the
    // entire export in a JavaScript Blob. This supports much larger log files.
    setError('');
    const anchor = document.createElement('a');
    anchor.href = `/api/download/export?${makeParams(false).toString()}`;
    anchor.download = 'log-export.txt';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  function clearFilters() {
    setFilters(FILTER_DEFAULTS); setOffset(0); setError('');
    setLoading(true);
    fetch(`/api/logs/query?limit=${encodeURIComponent(limit)}&offset=0`)
      .then(async response => { if (response.status === 401) { window.location.reload(); throw new Error('Session expired; sign in again.'); } const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Query failed'); setLogs(data.logs || []); setCount(data.count || 0); setLastUpdated(new Date()); })
      .catch(err => setError(err.message || 'Could not retrieve logs.'))
      .finally(() => setLoading(false));
  }

  function changePage(nextOffset) {
    setOffset(nextOffset);
    const params = new URLSearchParams();
    for (const key of ['server_ip', 'instance', 'filename', 'search']) if (filters[key].trim()) params.set(key, filters[key].trim());
    if (filters.start) params.set('start', toUtc(filters.start));
    if (filters.end) params.set('end', toUtc(filters.end));
    params.set('limit', limit); params.set('offset', String(nextOffset));
    setLoading(true); setError('');
    fetch(`/api/logs/query?${params.toString()}`)
      .then(async response => { if (response.status === 401) { window.location.reload(); throw new Error('Session expired; sign in again.'); } const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Query failed'); setLogs(data.logs || []); setCount(data.count || 0); setLastUpdated(new Date()); })
      .catch(err => setError(err.message || 'Could not retrieve logs.'))
      .finally(() => setLoading(false));
  }

  return (
    <main className="app-shell">
      <header className="header">
        <div className="brand-mark">LV</div>
        <div className="brand-copy"><p className="eyebrow">Infralyx</p><h1>Log Viewer</h1></div>
        <div className="header-right"><span className="connection-dot" /> Victoria Logs <span className="version-tag">v1</span><span className="signed-in-user" title={user.auth_source === 'local' ? 'Local superadmin account' : 'LDAP account'}>{user.username}</span><button className="button button-quiet logout-button" type="button" onClick={onLogout}>Log out</button></div>
      </header>

      <section className="hero">
        <div><p className="eyebrow hero-eyebrow">CENTRALIZED LOG SEARCH</p><h2>Investigate logs across servers</h2><p>Search and Download any service logs by source, message, and time range.</p></div>
        <div className="hero-stat"><span className="hero-stat-label">MATCHING ENTRIES</span><strong>{count.toLocaleString()}</strong><span>entries returned by this query</span></div>
      </section>

      <section className="filter-panel">
        <div className="section-heading"><div><h2>Search &amp; filters</h2><p>Leave start and end empty to view the latest 15 minutes.</p></div><button className="button button-quiet" type="button" onClick={clearFilters}>Clear filters</button></div>
        <form onSubmit={submitSearch}>
          <div className="filter-grid">
            <label>Server IP<input value={filters.server_ip} onChange={e => updateFilter('server_ip', e.target.value)} placeholder="e.g. 10.10.10.10" /></label>
            <label>Tomcat instance<input value={filters.instance} onChange={e => updateFilter('instance', e.target.value)} placeholder="e.g. apache-tomcat-9.0.1" /></label>
            <label className="wide-field">Log file path<input value={filters.filename} onChange={e => updateFilter('filename', e.target.value)} placeholder="e.g. /opt/apache-tomcat-9.0.1/logs/catalina.out" /></label>
            <label className="wide-field">Message contains<input value={filters.search} onChange={e => updateFilter('search', e.target.value)} placeholder="Search log message text" /></label>
            <label>Start time<input type="datetime-local" value={filters.start} onChange={e => updateFilter('start', e.target.value)} /></label>
            <label>End time<input type="datetime-local" value={filters.end} onChange={e => updateFilter('end', e.target.value)} /></label>
          </div>
          <div className="filter-actions"><label className="limit-control">Rows<select value={limit} onChange={e => setLimit(e.target.value)}><option value="100">100</option><option value="200">200</option><option value="500">500</option><option value="1000">1000</option></select></label><div className="action-buttons"><button className="button button-secondary" type="button" onClick={downloadLogs} >Download time range</button><button className="button button-primary" type="submit" disabled={loading}>{loading ? 'Searching…' : 'Search logs'}</button></div></div>
        </form>
      </section>

      {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}

      <section className="results-panel">
        <div className="results-heading"><div><h2>Log entries</h2><p>{lastUpdated ? `Last refreshed ${lastUpdated.toLocaleTimeString()}` : 'Waiting for first response'}</p></div><div className="results-tools"><label className="refresh-toggle"><input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} /><span className="toggle-track" /><span>Auto-refresh · 5s</span></label><button className="button button-quiet" onClick={() => loadLogs(true)} disabled={loading}>Refresh now</button></div></div>
        <div className="log-results" aria-live="polite">
          <div className="log-columns-heading"><span>Date / time ({BROWSER_TIME_ZONE})</span><span>Message</span></div>
          {groupedLogs.map((group, groupIndex) => <article className="log-group" key={`${group.source}-${groupIndex}`}>
            <header className="log-group-header">
              <div className="source-title"><span className="source-indicator" /><strong>{group.instance || 'Unknown instance'}</strong>{group.job && <span className="job-tag">{group.job}</span>}</div>
              <div className="source-metadata">
                <span><b>Server</b><code>{group.server_ip || '—'}</code></span>
                <span><b>Log file</b><code title={group.filename || ''}>{group.filename || '—'}</code></span>
              </div>
            </header>
            <div className="log-lines">
              {group.logs.map((log, index) => <div className="log-line" key={`${log.time}-${groupIndex}-${index}`}>
                <time className="log-line-time" title={`Original UTC: ${log.time || '—'}`}>{formatTimestamp(log.time)}</time>
                <pre className="log-line-message">{log.message || ''}</pre>
              </div>)}
            </div>
          </article>)}
          {!logs.length && <div className="empty-state">{loading ? 'Loading logs…' : 'No log entries found for these filters.'}</div>}
        </div>
        <footer className="table-footer"><span>Showing {logs.length} entries · Up to 1,000 per page</span><div className="pagination"><button className="button button-quiet" disabled={offset === 0 || loading} onClick={() => changePage(Math.max(0, offset - Number(limit)))}>Previous</button><span>Offset {offset.toLocaleString()}</span><button className="button button-quiet" disabled={logs.length < Number(limit) || loading} onClick={() => changePage(offset + Number(limit))}>Next</button></div></footer>
      </section>
      <footer className="app-footer"><span>Log Viewer V2 · authenticated session</span><span>Grouped by source · timestamps shown in browser timezone</span></footer>
    </main>
  );
}

function LoginPage({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function submitLogin(event) {
    event.preventDefault();
    setLoading(true);
    setError('');
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Login failed (${response.status})`);
      onLogin(data.user);
    } catch (err) {
      setError(err.message || 'Unable to log in.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="login-brand-mark">LV</div>
        <p className="eyebrow">OPERATIONS CONSOLE</p>
        <h1>Log Viewer</h1>
        <p className="login-description">Sign in with your corporate LDAP account or the locally configured superadmin account.</p>
        <form className="login-form" onSubmit={submitLogin}>
          <label>Username<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} required autoFocus /></label>
          <label>Password<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button className="button button-primary login-submit" type="submit" disabled={loading}>{loading ? 'Signing in…' : 'Sign in'}</button>
        </form>
        <p className="login-footnote">Access is available to authenticated users only.</p>
      </section>
    </main>
  );
}

function App() {
  const [user, setUser] = useState(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [sessionError, setSessionError] = useState('');

  useEffect(() => {
    let active = true;
    fetch('/api/auth/me')
      .then(async response => {
        if (response.status === 401) return null;
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not verify session');
        return data.user;
      })
      .then(currentUser => { if (active) setUser(currentUser); })
      .catch(err => { if (active) setSessionError(err.message || 'Could not connect to authentication service.'); })
      .finally(() => { if (active) setCheckingSession(false); });
    return () => { active = false; };
  }, []);

  async function logout() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } finally {
      setUser(null);
    }
  }

  if (checkingSession) {
    return <main className="login-shell"><div className="login-loading">Checking session…</div></main>;
  }
  if (sessionError) {
    return <main className="login-shell"><section className="login-card"><div className="login-brand-mark">LV</div><h1>Log Viewer</h1><div className="login-error" role="alert">{sessionError}</div><button className="button button-primary login-submit" onClick={() => window.location.reload()}>Retry</button></section></main>;
  }
  if (!user) return <LoginPage onLogin={setUser} />;
  return <LogViewer user={user} onLogout={logout} />;
}

createRoot(document.getElementById('root')).render(<App />);
