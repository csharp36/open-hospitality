import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import BuildStamp from './BuildStamp'

afterEach(() => vi.unstubAllEnvs())

describe('BuildStamp', () => {
  it("shows 'dev' when no build sha is baked in (local dev)", () => {
    render(<BuildStamp />)
    expect(screen.getByText('build dev')).toBeInTheDocument()
  })

  it('shows the baked-in short commit sha so a screenshot names its build', () => {
    vi.stubEnv('VITE_BUILD_SHA', 'a1b2c3d')
    render(<BuildStamp />)
    expect(screen.getByText('build a1b2c3d')).toBeInTheDocument()
    expect(screen.getByText('build a1b2c3d')).toHaveAttribute('title', 'Build a1b2c3d')
  })

  it('stays screen-reader accessible when the sidebar is collapsed', () => {
    vi.stubEnv('VITE_BUILD_SHA', 'a1b2c3d')
    render(<BuildStamp collapsed />)
    expect(screen.getByText('build a1b2c3d')).toHaveClass('sr-only')
  })
})
