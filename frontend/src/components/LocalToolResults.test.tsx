import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { API_BASE_URL, type LocalToolResult } from '../api'
import { LocalToolResults } from './LocalToolResults'

const completed: LocalToolResult = {
  id: 'tool-fft', tool: 'fourier', status: 'completed', title: '高斯函数频谱', summary: '本地计算得到幅度谱，采用角频率 ω。',
  parameters: { expression: 'exp(-t**2)', start: -8, end: 8 },
  artifacts: [{ id: 'aabbcc', url: '/api/tool-artifacts/aabbcc', caption: '有限窗口数值频谱' }],
  data: { method: 'fft', samples: 1024 }, notes: ['有限窗口 FFT 是数值近似，不是无限区间理想变换。'],
}

describe('actual local computation cards', () => {
  it('renders generated PNG links, real summaries and conditions separately from evidence', () => {
    const html = renderToStaticMarkup(<LocalToolResults results={[completed]} />)
    const artifactUrl = new URL('/api/tool-artifacts/aabbcc', API_BASE_URL).href
    expect(html).toContain(`src="${artifactUrl}"`)
    expect(html).toContain(`href="${artifactUrl}"`)
  })

  it('shows tool failure explicitly rather than fabricating an image or successful result', () => {
    const error: LocalToolResult = { ...completed, id: 'failed-tool', status: 'error', summary: '函数在采样区间出现非有限值，计算已停止。', artifacts: [], data: {}, notes: ['请检查定义域。'] }
    const html = renderToStaticMarkup(<LocalToolResults results={[error]} />)
    expect(html).toContain('role="alert"')
    expect(html).not.toContain('<img')
  })

  it('renders nothing for historical replies without local tools', () => {
    expect(renderToStaticMarkup(<LocalToolResults results={[]} />)).toBe('')
  })
})
