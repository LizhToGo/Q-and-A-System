import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RichText } from './RichText'
import { formatTextbookMarkdown } from './textbookMarkdown'
import extractionPage from '../../../data/fullbook-review/page-481.json'
import filterPage from '../../../data/fullbook-review/page-484.json'
import interpolationPage from '../../../data/fullbook-review/page-486.json'

function render(source: string, structure = true) {
  return renderToStaticMarkup(createElement(RichText, { text: formatTextbookMarkdown(source, { structure }), normalizeFormulas: false }))
}

describe('textbook evidence Markdown formatting', () => {
  it('renders arithmetic array rules without changing their rows or results', () => {
    const html = render(String.raw`\[\begin{array}{rr}13&01101\\10&01010\\\cline{1-1}\cline{2-2}23&10111\end{array}\]`)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math.match(/<mtr\b/g)).toHaveLength(3)
    expect(math).toContain('<mn>23</mn>')
    expect(math).toContain('<mn>10111</mn>')
    expect(html).not.toContain('katex-error')
  })

  it('keeps scientific unit values and exponents renderable inside text groups', () => {
    const html = render(String.raw`\[C=0.01\text{ μF},\quad R=10\text{ kΩ},\quad\delta=2\text{‰},\quad T(℃)\]`)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mn>0.01</mn>')
    expect(math).toContain('μ')
    expect(math).toContain('Ω')
    expect(math).toContain('<msup>')
    expect(math).toContain('∘')
    expect(html).not.toContain('katex-error')
  })

  it('keeps scientific units and adjacent numeric ranges in distinct table cells', () => {
    const html = render(String.raw`| 参数 | 单位 | 范围 |
|---|---|---|
| \(SR\) | V/μs | 0.5～0.7 |`, false)
    const cells = html.match(/<td\b[^>]*>[\s\S]*?<\/td>/g) ?? []
    expect(cells).toHaveLength(3)
    expect(cells[0]).toContain('<math')
    expect(cells[1]).toContain('<math')
    expect(cells[1]).toContain('μ')
    expect(cells[2]).toContain('0.5～0.7')
    expect(html).not.toContain('$$')
    expect(html).not.toContain('katex-error')
  })

  it('preserves absolute values and norms without splitting math across table columns', () => {
    const html = render(String.raw`| 表达式 | 条件 |
|---|---|
| \(|z|=e^{\sigma T_s}\) | 1 |
| \(\|x\|\) | 2 |`, false)
    const cells = html.match(/<td\b[^>]*>[\s\S]*?<\/td>/g) ?? []
    expect(cells).toHaveLength(4)
    expect(cells[0]).toContain('<msup>')
    expect(cells[0]).toContain('<math')
    expect(cells[2]).toContain('<math')
    expect(html).not.toContain('$$')
    expect(html).not.toContain('katex-error')
  })

  it('renders both adjacent math operands instead of merging their delimiters', () => {
    const html = render(String.raw`\(\Delta\)\(\omega\)`, false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).map(part => part.replace(/<annotation\b[\s\S]*?<\/annotation>/g, ''))
    expect(math).toHaveLength(2)
    expect(math[0]).toContain('Δ')
    expect(math[1]).toContain('ω')
    expect(html).not.toContain('katex-error')
  })

  it('keeps complete accented transfer functions in math when mentioned in prose', () => {
    const html = render(String.raw`下支路\bar B_M(z)的零点在单位圆外。`, false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mover')
    expect(math).toContain('<msub>')
    expect(math).toContain('<mi>B</mi>')
    expect(html).not.toContain('katex-error')
  })

  it('separates successive tables and keeps following prose outside their rows', () => {
    const html = render(`| 列一 | 列二 |
|---|---|
| 1 | 2 |
表后的说明
| 另一列 | 再一列 |
|---|---|
| 3 | 4 |
最后的说明`, false)
    const tables = html.match(/<table\b[\s\S]*?<\/table>/g) ?? []
    expect(tables).toHaveLength(2)
    for (const table of tables) expect(table.match(/<tr\b/g)).toHaveLength(2)
    expect(html).toMatch(/<\/table>[\s\S]*?<p\b/)
  })

  it('renders complete grouped radicals and scripted atoms without expanding their scope', () => {
    const html = render('增益g=√[(10^{0.1A_s}−1)/(10^{0.1R_p}−1)]，半径r=√a_2，比例q=√W_0/A_0。', false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math.match(/<msqrt>/g)).toHaveLength(3)
    expect(math).toMatch(/<msqrt><msub><mi>a<\/mi><mn>2<\/mn><\/msub><\/msqrt>/)
    expect(math).toMatch(/<msqrt><msub><mi>W<\/mi><mn>0<\/mn><\/msub><\/msqrt>/)
    expect(math).toContain('<mi>A</mi>')
    expect(html).not.toContain('katex-error')
  })

  it('keeps textual punctuation and circled labels stable across repeated formatting', () => {
    const source = String.raw`\[①（a、b）\quad\text{②（说明）}\]`
    const first = formatTextbookMarkdown(source, { structure: false })
    const second = formatTextbookMarkdown(first, { structure: false })
    expect(render(second, false)).toBe(render(first, false))
    expect(render(first, false)).not.toContain('katex-error')
    const math = render(first, false).match(/<math\b[\s\S]*?<\/math>/)?.[0]?.replace(/<annotation\b[\s\S]*?<\/annotation>/g, '') ?? ''
    expect(math).not.toMatch(/[①②]/)
    expect(math).toContain('2')
  })

  it('renders formulas whose braced exponents contain superscripts outside the U+2070 range', () => {
    const html = render('序列g(n)=A^{−n}W^{n²/2}x(n)，输出X(z_k)=W^{k²/2}q(k)。', false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toMatch(/<msup><mi>n<\/mi><mn>2<\/mn><\/msup>/)
    expect(math).toMatch(/<msup><mi>k<\/mi><mn>2<\/mn><\/msup>/)
    expect(html).not.toContain('katex-error')
  })

  it('keeps braced scripts before adjacent factors and renders limits and text subscripts', () => {
    const html = render('极限lim_{z→1}X(z)，分母b_{M−1}z^{M−1}，比较σ²_直接>σ²_级联。', false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mi>b</mi>')
    expect(math).toContain('→')
    expect(math).toContain('直接')
    expect(math).toContain('级联')
    expect(html).not.toContain('katex-error')
  })

  it('does not treat convolution operators as emphasis or suppress neighboring formulas', () => {
    const html = render('卷积y(n)=x(n)*h(n)，性质Z[x*h]=X(z)H(z)，收敛域R_{x−}<|z|<R_{x+}。', false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mi>R</mi>')
    expect(math).toContain('<msub>')
    expect(html).not.toContain('<em>')
    expect(html).not.toContain('katex-error')
  })

  it('renders named Fourier-transform dollar formulas without enabling ordinary price prose', () => {
    const html = render(String.raw`$DTFT[x(n)*h(n)]=X(e^{jω})H(e^{jω})$，价格为 $100 dollars and $5。`, false)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<msup>')
    expect(math).toContain('ω')
    expect(html).toContain('$100')
    expect(html).not.toContain('katex-error')
  })

  it('keeps literal Karnaugh axis separators between adjacent indexed variables', () => {
    const html = render(String.raw`| A₃A₂\A₁A₀ | 值 |
|---|---|
| 00 | 1 |`, false)
    const cells = html.match(/<th\b[^>]*>[\s\S]*?<\/th>/g) ?? []
    expect(cells).toHaveLength(2)
    expect(cells[0]?.match(/<msub>/g)).toHaveLength(4)
    expect(html).not.toContain('katex-error')
  })

  it('renders math and distinct blocks in concatenated textbook prose without losing conditions', () => {
    const source = '1.4.2 时域抽样定理 学习要点：若最高频率为f_h，抽样频率为f_s，必须f_s>2f_h。\n【例3.15】将一数字信号处理器作谱分析之用。解法一：T_0min=1/F_0=1/5s=0.2s。'
    const html = render(source)

    expect(html).toMatch(/<h3\b[^>]*>1\.4\.2 时域抽样定理<\/h3>/)
    expect(html).toMatch(/<h3\b[^>]*>学习要点：<\/h3>/)
    expect(html).toMatch(/<h3\b[^>]*>【例3\.15】<\/h3>/)
    expect(html).toMatch(/<h3\b[^>]*>解法一：<\/h3>/)
    expect(html).toContain('<math')
    expect(html).toContain('<msub><mi>f</mi><mi>h</mi></msub>')
    expect(html).toContain('<mo>&gt;</mo>')
    expect(html).toContain('将一数字信号处理器作谱分析之用。')
    expect(html).not.toContain('katex-error')
  })

  it('preserves existing Markdown, math, code, URLs, prices, and incomplete math', () => {
    const source = [
      '- 已有条目',
      '$$f_s=2f_h$$',
      '`f_s=2f_h` https://example.test/f_s=2',
      '价格为 $100',
      '未完成 x_a(',
      '```text\nf_s=2f_h\n```',
    ].join('\n')
    const html = render(source)

    expect(html).toContain('已有条目')
    expect(html).toContain('<math')
    expect(html).toMatch(/<code[^>]*>f_s=2f_h<\/code>/)
    expect(html).toContain('https://example.test/f_s=2')
    expect(html).toContain('价格为 $100')
    expect(html).toContain('未完成 x_a(')
    expect(html).not.toContain('katex-error')
  })

  it('keeps rendering stable when formatted evidence is processed again', () => {
    const source = '【例3.15】\n\nT_0min=1/F_0=0.2s'
    expect(render(formatTextbookMarkdown(source))).toBe(render(source))
  })

  it('keeps Greek products in the exponent and evaluation condition on PDF page 131', () => {
    const html = render('将s=jΩ及z=e^jΩT代入，得到X(z)|_{z=e^{jΩT}}=X(e^{jΩT})。')
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toMatch(/<msup><mi>e<\/mi><mrow><mi>j<\/mi><mi[^>]*>Ω<\/mi><mi>T<\/mi><\/mrow><\/msup>/)
    expect(math).toContain('<msub>')
    expect(html).not.toContain('katex-error')
    expect(html).not.toContain('代入，得到X(z)|_')
  })

  it('renders legacy single-dollar DTFT integrals and cases on PDF pages 111 and 112', () => {
    const html = render(String.raw`频谱$X(e^{jΩT})$，序列展开为$X(e^{jω})=\sum_{n=-\infty}^{\infty}x(n)e^{-jωn}$。
由于$\frac1{2π}\int_{-π}^{π}e^{jω(m−n)}dω=\begin{cases}1,&n=m\\0,&n\ne m\end{cases}=δ(m−n)$，则有
$x(n)=\frac1{2π}\int_{-π}^{π}X(e^{jω})e^{jωm}dω$。`)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mfrac>')
    expect(math).toMatch(/<mo[^>]*>∫<\/mo>/)
    expect(math).toContain('<mtable')
    expect(math).toMatch(/<mi>j<\/mi><mi>ω<\/mi><mi>m<\/mi>/)
    expect(math).toMatch(/<mi>ω<\/mi><mi>n<\/mi>/)
    expect(html).not.toContain('katex-error')
    expect(html).not.toContain('$\\frac')
  })

  it('does not reinterpret prices, code, or URLs when accepting textbook dollars', () => {
    const html = render(String.raw`价格 $100 和 $200，套餐 $100$。
代码：` + '`$\\frac1{2π}$ \\(x^2\\)`' + String.raw`
https://example.test/$x$?formula=ωn

` + '```text\n$X(e^{jΩT})$ \\(e^jΩT\\)\n```')
    expect(html).toContain('价格 $100 和 $200，套餐 $100$。')
    expect(html).toMatch(/<code[^>]*>\$\\frac1\{2π\}\$ \\\(x\^2\\\)<\/code>/)
    expect(html).toContain('https://example.test/$x$?formula=ωn')
    expect(html).not.toContain('<math')
  })

  it('normalizes legacy math in Markdown evidence without restructuring its headings', () => {
    const html = render(String.raw`## 已校订教材

公式 $X(z)|_{z=e^{jΩT}}=X(e^{jΩT})$。`, false)
    expect(html).toMatch(/<h2\b[^>]*>已校订教材<\/h2>/)
    expect(html).toContain('<msub>')
    expect(html).not.toContain('katex-error')
  })

  it('keeps explicit TeX exponent semantics and does not invent a sum from Sigma', () => {
    const html = render(String.raw`\(x^2n+Σ\)`)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<msup><mi>x</mi><mn>2</mn></msup><mi>n</mi>')
    expect(math).toMatch(/<mi[^>]*>Σ<\/mi>/)
    expect(math).not.toContain('∑')
  })
  it('keeps adjacent scripted factors separate and conjugation stars inside math', () => {
    const html = render('X(z)=a^nz^{-n}，N=f_sT_0，y=a^nΣ_{m=-2}^{n}，x(n)=x^*(-n)，y(n)=y^*(n)。')
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<msup><mi>a</mi><mi>n</mi></msup><msup><mi>z</mi>')
    expect(math).toContain('<msub><mi>f</mi><mi>s</mi></msub><msub><mi>T</mi><mn>0</mn></msub>')
    expect(math).toMatch(/<msup><mi>a<\/mi><mi>n<\/mi><\/msup><msubsup><mi[^>]*>Σ<\/mi>/)
    expect(math).toMatch(/<msup><mi>x<\/mi><mo\b[^>]*>[^<]+<\/mo><\/msup>/)
    expect(formatTextbookMarkdown('序列x(n)=x^*(-n)。')).toContain('$$x(n)=x^*(-n)$$')
    expect(html).not.toContain('katex-error')
  })

  it('does not feed a Markdown heading marker to KaTeX', () => {
    const html = render('### 9.5 h(n)=a^nu(n)')
    expect(html).toMatch(/<h3\b/)
    expect(html).toContain('<math')
    expect(html).not.toContain('katex-error')
  })
  it('renders accented Greek variables, prime frequencies and half factors without missing glyphs', () => {
    const html = render(String.raw`\(H(jΩ̄)=½\cos(ω″)\)`)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toContain('<mover')
    expect(math).toContain('<mfrac><mn>1</mn><mn>2</mn></mfrac>')
    expect(math).toMatch(/<mi>ω<\/mi>/)
    expect(html).not.toContain('katex-error')
  })

  it.each([
    { variable: 'T′₀', latex: "T'_{0}", base: 'T', subscript: '0' },
    { variable: 'Δf′₀', latex: "\\Delta f'_{0}", base: 'f', subscript: '0' },
    { variable: 'X′₁', latex: "X'_{1}", base: 'X', subscript: '1' },
  ])('keeps the complete primed variable $variable inside math', ({ variable, base, subscript }) => {
    const source = `变量${variable}，不改变上下标。`
    const html = render(source)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toMatch(new RegExp(`<msubsup><mi>${base}</mi><mn>${subscript}</mn><(?:mo|mrow)\\b`))
    expect(math).not.toMatch(/[₀₁]/)
    expect(html).not.toContain('katex-error')
  })

  it.each(['X̄₁', 'x̄₁', 'x̄₂'])('keeps the accent and Unicode subscript of %s together', (variable) => {
    const base = variable[0]
    const subscript = variable.endsWith('₁') ? '1' : '2'
    const source = `序列${variable}(n)，用于周期分析。`
    const html = render(source)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toMatch(new RegExp(`<msub><mover\\b[^>]*><mi>${base}</mi>[\\s\\S]*?</mover><mn>${subscript}</mn></msub>`))
    expect(math).not.toMatch(/[₁₂]/)
    expect(html).not.toContain('katex-error')
  })

  it('keeps primed variables whole in reviewed equation chains without changing values', () => {
    const source = '记录T′₀min=2T₀min=0.4s；带宽Δf′₀=f_h/r=243.81Hz；采样f_s=2Δf′₀=488Hz。'
    const html = render(source)
    const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
    expect(math).toMatch(/<msubsup><mi>T<\/mi><mn>0<\/mn>/)
    expect(math.match(/<msubsup><mi>f<\/mi><mn>0<\/mn>/g)).toHaveLength(2)
    expect(math).toContain('<mn>0.4</mn>')
    expect(math).toContain('<mn>243.81</mn>')
    expect(math).toContain('<mn>488</mn>')
    expect(math).not.toContain('₀')
    expect(html).not.toContain('katex-error')
  })

  it.each([
    { page: extractionPage, equation: '8.2.4', firstValue: '<mi>x</mi>', condition: '<mi>n</mi>' },
    { page: filterPage, equation: '8.2.15', firstValue: '<mn>1</mn>', condition: '<mi>ω</mi>' },
    { page: interpolationPage, equation: '8.3.2', firstValue: '<mi>x</mi>', condition: '<mi>n</mi>' },
  ])('renders both reviewed cases rows on PDF page $page.page_number', ({ page, equation, firstValue, condition }) => {
    for (const structure of [true, false]) {
      const html = render(page.normalized_text, structure)
      const math = (html.match(/<math\b[\s\S]*?<\/math>/g) ?? []).join('').replace(/<annotation\b[\s\S]*?<\/annotation>/g, '')
      const tables = math.match(/<mtable\b[\s\S]*?<\/mtable>/g) ?? []
      expect(tables).toHaveLength(1)
      const rows = (tables[0] ?? '').match(/<mtr\b[\s\S]*?<\/mtr>/g) ?? []
      expect(rows).toHaveLength(2)
      expect(rows[0]).toContain(firstValue)
      expect(rows[0]).toContain(condition)
      expect(rows[1]).toContain('<mn>0</mn>')
      expect(rows[1]).toContain('其他')
      expect(rows[1]).toContain(condition)
      expect(html).toContain(equation)
      expect(html).not.toContain('katex-error')
    }
  })
})
