import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ReleaseNotes from './ReleaseNotes'
import type { Release } from '../generated/releaseNotes'

const FIXTURES: Release[] = [
  {
    sha: 'aaa111',
    date: '2026-08-13',
    pr: 49,
    changes: [
      { type: 'feat', label: 'Features', scope: 'ui', breaking: false, subject: 'stamp the build sha', pr: null },
      { type: 'fix', label: 'Fixes', scope: 'demo', breaking: false, subject: 'seed config every run', pr: null },
    ],
  },
  {
    sha: 'bbb222',
    date: '2026-08-10',
    pr: 45,
    changes: [
      { type: 'fix', label: 'Fixes', scope: null, breaking: false, subject: 'founding org rls', pr: null },
    ],
  },
]

describe('ReleaseNotes', () => {
  it('lists each release with its sha, date, and a PR link to GitHub', () => {
    render(<ReleaseNotes buildSha="aaa111" onClose={vi.fn()} releases={FIXTURES} />)
    expect(screen.getByText('aaa111')).toBeInTheDocument()
    expect(screen.getByText('bbb222')).toBeInTheDocument()
    expect(screen.getByText('2026-08-13')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '#49' })).toHaveAttribute(
      'href',
      'https://github.com/csharp36/open-hospitality/pull/49',
    )
  })

  it('groups a release\'s changes under type headings', () => {
    render(<ReleaseNotes buildSha="aaa111" onClose={vi.fn()} releases={FIXTURES} />)
    expect(screen.getAllByText('Features').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Fixes').length).toBeGreaterThan(0)
    expect(screen.getByText(/stamp the build sha/)).toBeInTheDocument()
  })

  it('marks the release matching the running build as "This build"', () => {
    const { rerender } = render(
      <ReleaseNotes buildSha="aaa111" onClose={vi.fn()} releases={FIXTURES} />,
    )
    const stamped = screen.getByText('This build').closest('li')
    expect(within(stamped as HTMLElement).getByText('aaa111')).toBeInTheDocument()
    expect(screen.getAllByText('This build')).toHaveLength(1)

    // The marker follows the build, not a fixed position.
    rerender(<ReleaseNotes buildSha="bbb222" onClose={vi.fn()} releases={FIXTURES} />)
    const moved = screen.getByText('This build').closest('li')
    expect(within(moved as HTMLElement).getByText('bbb222')).toBeInTheDocument()
  })

  it('does not mark any release when the build is not in history (e.g. local dev)', () => {
    render(<ReleaseNotes buildSha="dev" onClose={vi.fn()} releases={FIXTURES} />)
    expect(screen.queryByText('This build')).not.toBeInTheDocument()
  })

  it('shows an empty state when there are no notes', () => {
    render(<ReleaseNotes buildSha="dev" onClose={vi.fn()} releases={[]} />)
    expect(screen.getByText(/no release notes available/i)).toBeInTheDocument()
  })
})
