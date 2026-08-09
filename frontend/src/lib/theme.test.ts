// theme.ts owns the dark-mode runtime: init resolves a stored choice, else
// the product default; toggle flips the root class and persists.
// jsdom has no matchMedia, so it is stubbed per-test and restored after —
// the point of those stubs is now to prove init IGNORES the OS.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { currentTheme, initTheme, toggleTheme } from './theme'

function stubMatchMedia(prefersDark: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: prefersDark && query === '(prefers-color-scheme: dark)',
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })),
  )
}

describe('theme runtime', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })

  it('applies a stored dark preference', () => {
    localStorage.setItem('usali-theme', 'dark')
    expect(initTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('applies a stored light preference even when the OS prefers dark', () => {
    stubMatchMedia(true)
    localStorage.setItem('usali-theme', 'light')
    expect(initTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  // The product default is light and the OS is not consulted: two operators
  // on machines with different system settings must see the same portal until
  // one of them chooses otherwise.
  it('defaults to light when nothing is stored, even when the OS prefers dark', () => {
    stubMatchMedia(true)
    expect(initTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('defaults to light when nothing is stored and the OS prefers light', () => {
    stubMatchMedia(false)
    expect(initTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  // Blocked storage (private mode, locked-down iframe) must boot, not throw.
  it('defaults to light when localStorage is unreadable', () => {
    const getItem = Storage.prototype.getItem
    Storage.prototype.getItem = () => { throw new Error('blocked') }
    try {
      expect(initTheme()).toBe('light')
    } finally {
      Storage.prototype.getItem = getItem
    }
  })

  it('toggle flips the class and persists the choice', () => {
    stubMatchMedia(false)
    initTheme()
    expect(currentTheme()).toBe('light')

    expect(toggleTheme()).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(localStorage.getItem('usali-theme')).toBe('dark')

    expect(toggleTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(localStorage.getItem('usali-theme')).toBe('light')
  })
})
