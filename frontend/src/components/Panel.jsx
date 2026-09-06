import { useCallback, useRef } from 'react'

/**
 * Angular console panel whose sheen tracks the pointer across it.
 *
 * The gradient position is written to CSS custom properties on the element,
 * so the highlight follows the cursor without re-rendering children.
 */
export default function Panel({ title, tag, actions, children, className = '' }) {
  const ref = useRef(null)

  const onMove = useCallback((event) => {
    const node = ref.current
    if (!node) return
    const rect = node.getBoundingClientRect()
    node.style.setProperty('--px', `${event.clientX - rect.left}px`)
    node.style.setProperty('--py', `${event.clientY - rect.top}px`)
  }, [])

  return (
    <section className={`panel ${className}`} ref={ref} onMouseMove={onMove}>
      {title && (
        <h2 className="panel-title">
          {title}
          {tag && <span className="tag">{tag}</span>}
          {/* Sits after the tag, which carries the margin-left:auto that
              pushes both to the right edge. Centred rather than baselined
              so buttons line up with the rule under the title. */}
          {actions && (
            <span style={{ alignSelf: 'center', marginLeft: tag ? 0 : 'auto' }}>
              {actions}
            </span>
          )}
        </h2>
      )}
      {children}
    </section>
  )
}
