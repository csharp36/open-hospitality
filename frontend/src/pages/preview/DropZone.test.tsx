// DropZone unit tests: presentational drag/drop + file-pick control. It
// validates type and size client-side and only calls onFile for a PDF under
// the 10 MB cap; anything else shows a role="alert" message instead.

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import DropZone from './DropZone'

function pickFile(file: File) {
  return screen.findByLabelText('PDF file').then((input) => {
    fireEvent.change(input, { target: { files: [file] } })
  })
}

describe('DropZone', () => {
  it('calls onFile once for a valid PDF picked through the input', async () => {
    const onFile = vi.fn()
    render(<DropZone onFile={onFile} />)

    await pickFile(new File(['x'], 'a.pdf', { type: 'application/pdf' }))

    expect(onFile).toHaveBeenCalledTimes(1)
  })

  it('rejects a non-PDF file and shows an alert instead of calling onFile', async () => {
    const onFile = vi.fn()
    render(<DropZone onFile={onFile} />)

    await pickFile(new File(['x'], 'a.txt', { type: 'text/plain' }))

    expect(onFile).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('rejects a file over 10 MB and shows a "too large" alert instead of calling onFile', async () => {
    const onFile = vi.fn()
    render(<DropZone onFile={onFile} />)

    await pickFile(
      new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'big.pdf', { type: 'application/pdf' }),
    )

    expect(onFile).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toHaveTextContent(/too large|over 10 ?mb/i)
  })
})
