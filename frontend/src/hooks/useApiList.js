import { useCallback, useEffect, useState } from 'react'

import { apiList } from '../api'

/**
 * Load a list endpoint, tracking loading and error state.
 * Returns a reload function so views can refresh after an action.
 */
export function useApiList(path, enabled = true) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    setError(null)
    try {
      setRows(await apiList(path))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [path, enabled])

  useEffect(() => {
    load()
  }, [load])

  return { rows, loading, error, reload: load }
}
