import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RichText, stabilizeStreamingMarkdown } from './RichText'

describe('RichText Markdown and math rendering', () => {
  it('renders GFM blocks and KaTeX math without exposing executable HTML', () => {
    const source = [
      '# 标题',
      '',
      '**粗体** *斜体* ~~删除线~~ [链接](https://example.com)',
      '',
      '- 列表',
      '  - 子项',
      '',
      '> 引用',
      '',
      '| 列 | 值 |',
      '| --- | --- |',
      '| A | 1 |',
      '',
      '$$f_{max}=f_s/2$$',
      '',
      '<script>alert(1)</script>',
    ].join('\n')

    const html = renderToStaticMarkup(<RichText text={source} />)
    expect(html).toMatch(/<h1\b/)
    expect(html).toContain('data-streamdown="strong"')
    expect(html).toContain('<del>删除线</del>')
    expect(html).toContain('<table')
    expect(html).toContain('katex')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('alert(1)')
  })

  it('upgrades only complete standalone equation lines for legacy local answers', () => {
    const html = renderToStaticMarkup(<RichText text={'结论：\n\nfmax = fs / 2\n\n价格为 $100'} />)
    expect(html).toContain('katex')
    expect(html).toContain('价格为 $100')
  })

  it('renders standalone equations inside existing model math blocks without wrapping them again', () => {
    const source = '教材给出：\n\n$$\ny(1)=3x(2)\n$$\n\n所以系统是非因果的。'
    const html = renderToStaticMarkup(<RichText text={source} />)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mi>y</mi>')
    expect(math).toContain('<mn>3</mn>')
    expect(html).not.toContain('$$')
    expect(html).not.toContain('katex-error')
  })

  it('renders complete inline and block slash-delimited LaTeX', () => {
    const source = String.raw`行内 \(x^2\)

\[
f_s = 2 f_{max}
\]`
    const html = renderToStaticMarkup(<RichText text={source} />)
    expect((html.match(/katex/g) ?? []).length).toBeGreaterThanOrEqual(2)
  })

  it('preserves fenced, escaped, and incomplete slash delimiters', () => {
    const source = [
      '```markdown',
      String.raw`\(literal\)`,
      '```',
      String.raw`\\(escaped\\)`,
      String.raw`未完成 \(x^2`,
    ].join('\n')
    const html = renderToStaticMarkup(<RichText text={source} normalizeFormulas={false} />)
    expect(html).toContain(String.raw`\(literal\)`)
    expect(html).toContain('未完成 (x^2')
    expect(html).not.toContain('<math')
  })

  it('does not enable single dollars for ordinary answers or alter inline code and URLs', () => {
    const source = String.raw`价格 $100 和 $200；原样 $x^2$。
` + '`\\(x^2\\) $x$`' + String.raw`
https://example.test/math/\(x^2\)`
    const html = renderToStaticMarkup(<RichText text={source} />)
    expect(html).toContain('价格 $100 和 $200；原样 $x^2$。')
    expect(html).toMatch(/<code[^>]*>\\\(x\^2\\\) \$x\$<\/code>/)
    expect(html).toContain(String.raw`https://example.test/math/\(x^2\)`)
    expect(html).not.toContain('<math')
  })

  it('keeps incomplete streaming code fences parseable', () => {
    expect(stabilizeStreamingMarkdown('```python\nprint(1)')).toContain('```python\nprint(1)\n\n```')
    expect(stabilizeStreamingMarkdown('```python\nprint(1)\n```')).toBe('```python\nprint(1)\n```')
  })
})
