// Dark-mode runtime: init from localStorage, toggle + persist. The `dark`
// class on <html> drives the `.dark { ... }` variable remap in index.css.
//
// The product default is LIGHT, and the OS preference is deliberately not
// consulted. This is a back-of-house tool read next to printed statements and
// shown on shared machines, so it should look the same to everyone until
// somebody chooses otherwise — an operator who has never touched the toggle
// should not see a different portal from the colleague beside them because of
// a system setting they never connected to this app.
//
// A STORED choice still wins in both directions, so anyone who prefers dark
// picks it once and keeps it.

const KEY = 'usali-theme'
const DEFAULT_THEME: Theme = 'light'

export type Theme = 'light' | 'dark'

function apply(theme: Theme): Theme {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  return theme
}

export function currentTheme(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

export function initTheme(): Theme {
  // localStorage can throw when storage is blocked (private mode, iframe
  // policies) — fall back to the default rather than crashing boot.
  let stored: string | null = null
  try {
    stored = localStorage.getItem(KEY)
  } catch {
    stored = null
  }
  if (stored === 'light' || stored === 'dark') return apply(stored)
  return apply(DEFAULT_THEME)
}

export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === 'dark' ? 'light' : 'dark'
  try {
    localStorage.setItem(KEY, next)
  } catch {
    // Blocked storage: the toggle still applies for this session.
  }
  return apply(next)
}
