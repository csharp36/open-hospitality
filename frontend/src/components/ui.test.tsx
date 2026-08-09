// Shared UI primitives (components/ui.tsx). Badge tone classes must contain
// the legacy color words (green/amber/red/blue) — className-regex assertions
// in the page tests rely on those words for status-color continuity.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import {
  Badge,
  Card,
  PageHeader,
  amountCellClass,
  amountHeadClass,
  cellClass,
  controlClass,
  headCellClass,
  sectionHeadClass,
  tableClass,
} from './ui'

describe('Card', () => {
  it('renders its children', () => {
    render(
      <Card>
        <p>card body</p>
      </Card>,
    )
    expect(screen.getByText('card body')).toBeInTheDocument()
  })

  it('merges an extra className onto the card surface', () => {
    render(<Card className="mt-4">inside</Card>)
    const card = screen.getByText('inside')
    expect(card.className).toMatch(/mt-4/)
    expect(card.className).toMatch(/surface-raised/)
  })

  // Pages locate sections via getByRole('region', { name }) — Card must be
  // able to carry those landmark semantics through rest props.
  it('spreads rest props so consumers can carry region semantics', () => {
    render(
      <Card role="region" aria-label="Sales">
        sales body
      </Card>,
    )
    const region = screen.getByRole('region', { name: 'Sales' })
    expect(region).toHaveTextContent('sales body')
    expect(region.className).toMatch(/surface-raised/)
  })
})

describe('PageHeader', () => {
  it('renders the title as a level-1 heading by default', () => {
    render(<PageHeader title="Coverage" />)
    expect(screen.getByRole('heading', { name: 'Coverage', level: 1 })).toBeInTheDocument()
  })

  // Per-source sections on CoveragePage keep their h2 rank — a page must not
  // grow multiple h1s when sections move onto PageHeader in Task 4.
  it('renders a level-2 heading when level={2}', () => {
    render(<PageHeader title="Bank feed" level={2} />)
    expect(screen.getByRole('heading', { name: 'Bank feed', level: 2 })).toBeInTheDocument()
  })

  it('renders subtitle and actions when provided', () => {
    render(
      <PageHeader
        title="Reports"
        subtitle="Month-end pack"
        actions={<button type="button">Download</button>}
      />,
    )
    expect(screen.getByText('Month-end pack')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
  })

  it('omits the subtitle element when not provided', () => {
    // The subtitle is the only <p> PageHeader can render, so its absence
    // proves the element (not just some text) was omitted.
    const { container } = render(<PageHeader title="Upload" />)
    expect(container.querySelector('p')).toBeNull()
  })
})

describe('Badge', () => {
  it('renders its children', () => {
    render(<Badge tone="ok">pushed</Badge>)
    expect(screen.getByText('pushed')).toBeInTheDocument()
  })

  // The legacy color words keep the className-regex assertions in
  // QboPage.test.tsx / UploadPage.test.tsx meaningful after the restyle.
  it('ok tone classes contain "green"', () => {
    render(<Badge tone="ok">ok</Badge>)
    expect(screen.getByText('ok').className).toMatch(/green/)
  })

  it('warn tone classes contain "amber"', () => {
    render(<Badge tone="warn">warn</Badge>)
    expect(screen.getByText('warn').className).toMatch(/amber/)
  })

  it('danger tone classes contain "red"', () => {
    render(<Badge tone="danger">danger</Badge>)
    expect(screen.getByText('danger').className).toMatch(/red/)
  })

  it('info tone classes contain "blue"', () => {
    render(<Badge tone="info">info</Badge>)
    expect(screen.getByText('info').className).toMatch(/blue/)
  })

  it('neutral tone uses muted surface classes without status color words', () => {
    render(<Badge tone="neutral">not pushed</Badge>)
    const badge = screen.getByText('not pushed')
    expect(badge.className).toMatch(/surface-sunken/)
    expect(badge.className).not.toMatch(/green|amber|red|blue/)
  })

  it('renders as a pill', () => {
    render(<Badge tone="info">pill</Badge>)
    expect(screen.getByText('pill').className).toMatch(/rounded-full/)
  })
})

describe('table style constants', () => {
  it('amount cells are right-aligned with tabular numerals', () => {
    expect(amountCellClass).toMatch(/text-right/)
    expect(amountCellClass).toMatch(/tabular-nums/)
  })

  it('head cells are muted left-aligned label text', () => {
    expect(headCellClass).toMatch(/text-ink-muted/)
    expect(headCellClass).toMatch(/text-xs/)
    expect(headCellClass).toMatch(/text-left/)
  })

  // amountHeadClass exists so amount headers never stack text-right onto
  // headCellClass's text-left (conflicting utilities = stylesheet-order flip).
  it('amount head cells are right-aligned muted labels and never left-aligned', () => {
    expect(amountHeadClass).toMatch(/text-right/)
    expect(amountHeadClass).not.toMatch(/text-left/)
    expect(amountHeadClass).toMatch(/text-ink-muted/)
    expect(amountHeadClass).toMatch(/text-xs/)
    expect(amountHeadClass).toMatch(/whitespace-nowrap/)
  })

  it('tables span their container and body cells top-align', () => {
    expect(tableClass).toMatch(/w-full/)
    expect(cellClass).toMatch(/align-top/)
  })
})

describe('sectionHeadClass', () => {
  it('is the uppercase muted section-label recipe', () => {
    expect(sectionHeadClass).toMatch(/uppercase/)
    expect(sectionHeadClass).toMatch(/text-ink-muted/)
    expect(sectionHeadClass).toMatch(/text-xs/)
  })
})

describe('controlClass', () => {
  it('uses the shared control recipe (rounded-control, line border, accent focus ring)', () => {
    expect(controlClass).toMatch(/rounded-control/)
    expect(controlClass).toMatch(/border-line/)
    expect(controlClass).toMatch(/focus:ring-accent/)
    expect(controlClass).toMatch(/bg-surface-raised/)
  })
})
