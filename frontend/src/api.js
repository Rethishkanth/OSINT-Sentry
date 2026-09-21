// Thin API client. Vite proxies /api to Django on :8000 in development.

const ACCESS = 'osint_access'
const REFRESH = 'osint_refresh'

// localStorage throws in some privacy contexts, so every access is guarded.
function readToken(key) {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeTokens(access, refresh) {
  try {
    if (access) localStorage.setItem(ACCESS, access)
    if (refresh) localStorage.setItem(REFRESH, refresh)
  } catch {
    /* session-only auth is an acceptable fallback */
  }
}

export function clearTokens() {
  try {
    localStorage.removeItem(ACCESS)
    localStorage.removeItem(REFRESH)
  } catch {
    /* nothing to clear */
  }
}

export function hasToken() {
  return Boolean(readToken(ACCESS))
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function parseError(res) {
  try {
    const data = await res.json()
    // The backend normalises everything to {"error": {...}}, putting per-field
    // messages under .fields. Surfacing the first one matters: the generic
    // wrapper just says "Validation failed", which tells the user nothing
    // about which field it disliked or why.
    const fields = data?.error?.fields
    if (fields) {
      const first = Object.values(fields).flat()[0]
      if (first) return String(first)
    }
    return data?.error?.message || data?.detail || `Request failed (${res.status})`
  } catch {
    return `Request failed (${res.status})`
  }
}

async function refreshAccess() {
  const refresh = readToken(REFRESH)
  if (!refresh) return false

  const res = await fetch('/api/auth/token/refresh/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh }),
  })
  if (!res.ok) return false

  const data = await res.json()
  writeTokens(data.access, data.refresh)
  return true
}

export async function login(username, password) {
  const res = await fetch('/api/auth/token/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    throw new ApiError(
      res.status === 401 ? 'Credentials rejected.' : await parseError(res),
      res.status,
    )
  }
  const data = await res.json()
  writeTokens(data.access, data.refresh)
  return data
}

export async function apiFetch(path, options = {}, _retried = false) {
  const token = readToken(ACCESS)
  // A file upload must go as multipart, and the boundary is only known to the
  // browser — setting Content-Type by hand here would send a header with no
  // boundary in it and the upload would arrive empty.
  const isUpload = typeof FormData !== 'undefined' && options.body instanceof FormData
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(isUpload ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  })

  // Access tokens live 30 minutes; refresh once, transparently, then retry.
  if (res.status === 401 && !_retried && (await refreshAccess())) {
    return apiFetch(path, options, true)
  }

  if (!res.ok) throw new ApiError(await parseError(res), res.status)
  if (res.status === 204) return null
  return res.json()
}

// DRF paginates by default; most views here just want the rows.
export async function apiList(path) {
  const data = await apiFetch(path)
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.results)) return data.results
  if (Array.isArray(data?.features)) return data.features // GeoJSON
  return []
}
