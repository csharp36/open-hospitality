// The shell's modal contract, exercised where the panel tests cannot reach:
// children with several tabbables for the trap, and a live trigger whose
// focus must come back on every close path.

import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import SlideOverShell from './SlideOverShell'

function Harness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open panel
      </button>
      {open && (
        <SlideOverShell label="Test panel" title="Test panel" onClose={() => setOpen(false)}>
          <button type="button">alpha</button>
          <button type="button">omega</button>
        </SlideOverShell>
      )}
    </>
  )
}

function openPanel() {
  render(<Harness />)
  const trigger = screen.getByRole('button', { name: 'open panel' })
  // Focus before clicking: fireEvent.click does not move focus in jsdom, and
  // the restore contract is about where focus WAS, not where the click landed.
  trigger.focus()
  fireEvent.click(trigger)
  return trigger
}

describe('SlideOverShell', () => {
  it('opens with focus on Close; Escape closes and hands focus back to the trigger', () => {
    const trigger = openPanel()
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closing via the Close button also hands focus back to the trigger', () => {
    const trigger = openPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('claims the Escape it acts on, so one keypress cannot close a stack', () => {
    openPanel()
    // fireEvent returns false when preventDefault was called on the event.
    expect(fireEvent.keyDown(document, { key: 'Escape' })).toBe(false)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('a close after the opener left the DOM falls back to body, not a dead node', () => {
    // The opener unmounts the moment the panel opens — closing must not try
    // to restore focus into a disconnected element.
    function VanishingOpener() {
      const [open, setOpen] = useState(false)
      return (
        <>
          {!open && (
            <button type="button" onClick={() => setOpen(true)}>
              open panel
            </button>
          )}
          {open && (
            <SlideOverShell label="Test panel" title="Test panel" onClose={() => setOpen(false)}>
              <button type="button">alpha</button>
            </SlideOverShell>
          )}
        </>
      )
    }
    render(<VanishingOpener />)
    const trigger = screen.getByRole('button', { name: 'open panel' })
    trigger.focus()
    fireEvent.click(trigger)
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger.isConnected).toBe(false)
    expect(document.body).toHaveFocus()
  })

  it('an Escape something closer to the key already consumed does not close the panel', () => {
    openPanel()
    const alpha = screen.getByRole('button', { name: 'alpha' })
    alpha.addEventListener('keydown', (e) => e.preventDefault())
    fireEvent.keyDown(alpha, { key: 'Escape' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('Tab wraps from the last tabbable to the first, and Shift+Tab the other way', () => {
    openPanel()
    // Close renders first in the panel, so it is the first tabbable; omega,
    // last in children, is the last.
    const close = screen.getByRole('button', { name: 'Close' })
    const omega = screen.getByRole('button', { name: 'omega' })
    omega.focus()
    fireEvent.keyDown(omega, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(omega).toHaveFocus()
  })

  it('a Tab in the interior of the cycle is left to the browser', () => {
    openPanel()
    const alpha = screen.getByRole('button', { name: 'alpha' })
    alpha.focus()
    const event = fireEvent.keyDown(alpha, { key: 'Tab' })
    // fireEvent returns false when preventDefault was called; an interior Tab
    // must pass through untouched.
    expect(event).toBe(true)
    expect(alpha).toHaveFocus()
  })
})
