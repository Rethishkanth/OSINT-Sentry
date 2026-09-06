import { useEffect, useRef } from 'react'

const HOT_SELECTOR = 'button, a, input, [role="tab"], .panel'

/**
 * Custom pointer: a hard dot that tracks exactly, a ring that eases behind it,
 * and full-viewport crosshairs.
 *
 * Position is written straight to the DOM in a rAF loop rather than held in
 * React state — mousemove fires far faster than a component should re-render.
 * The shared --mx/--my custom properties drive the background spotlight and
 * grid mask in CSS.
 */
export default function Cursor() {
  const dotRef = useRef(null)
  const ringRef = useRef(null)
  const target = useRef({ x: 0, y: 0 })
  const ring = useRef({ x: 0, y: 0 })
  const frame = useRef(0)

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const coarse = window.matchMedia('(pointer: coarse)').matches
    if (reduced || coarse) return undefined

    const root = document.documentElement

    const onMove = (event) => {
      target.current = { x: event.clientX, y: event.clientY }
      root.style.setProperty('--mx', `${event.clientX}px`)
      root.style.setProperty('--my', `${event.clientY}px`)

      const hot = event.target?.closest?.(HOT_SELECTOR)
      ringRef.current?.classList.toggle('is-hot', Boolean(hot))
    }

    const tick = () => {
      const { x, y } = target.current
      // Ease the ring toward the pointer so it lags with a bit of weight.
      ring.current.x += (x - ring.current.x) * 0.18
      ring.current.y += (y - ring.current.y) * 0.18

      if (dotRef.current) {
        dotRef.current.style.transform = `translate(${x}px, ${y}px)`
      }
      if (ringRef.current) {
        ringRef.current.style.transform =
          `translate(${ring.current.x}px, ${ring.current.y}px)`
      }
      frame.current = requestAnimationFrame(tick)
    }

    window.addEventListener('mousemove', onMove, { passive: true })
    frame.current = requestAnimationFrame(tick)

    return () => {
      window.removeEventListener('mousemove', onMove)
      cancelAnimationFrame(frame.current)
    }
  }, [])

  return (
    <>
      <div className="cursor-ring" ref={ringRef} aria-hidden="true" />
      <div className="cursor-dot" ref={dotRef} aria-hidden="true" />
      <div className="cursor-axis x" aria-hidden="true" />
      <div className="cursor-axis y" aria-hidden="true" />
    </>
  )
}
