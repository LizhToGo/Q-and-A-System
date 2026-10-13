import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import katex from 'katex'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { parseFragment } from 'parse5'

const inputPath = process.argv[2]
const outputPath = process.argv[3]
if (!inputPath || !outputPath) throw new Error('Usage: node frontend/scripts/audit-rag-math.mjs INPUT.json OUTPUT.json')
const snapshot = JSON.parse(await readFile(inputPath, 'utf8'))
// Bundle our presentation components, but use the same installed React and
// Streamdown modules as the app. Absolute external URLs also work from a data URI.
const compiled = await build({
  stdin: {
    contents: 'export { formatTextbookMarkdown } from "./textbookMarkdown.ts"; export { RichText } from "./RichText.tsx";',
    resolveDir: fileURLToPath(new URL('../src/components/', import.meta.url)),
    loader: 'tsx',
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  logLevel: 'silent',
  plugins: [{
    name: 'node-presentation-audit',
    setup(builder) {
      // Styles have no effect on the semantic SSR tree; do not replace plugins.
      builder.onResolve({ filter: /\.css$/ }, args => ({ path: args.path, namespace: 'audit-css' }))
      builder.onLoad({ filter: /.*/, namespace: 'audit-css' }, () => ({ contents: '', loader: 'js' }))
      builder.onResolve({ filter: /^[^./]/ }, args => ({ path: import.meta.resolve(args.path), external: true }))
    },
  }],
})
const formatter = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
const findings = []
const counts = { materials: 0, chunks: 0, formula_records: 0, reviewed_formulas: 0, displayed_formulas: 0, rendered_materials: 0, rendered_math: 0, rendered_tables: 0, renderer_exceptions: 0 }
const materialRendering = []

function mathSpans(text) {
  const spans = []
  const pattern = /(?<literal>```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`)|(?<display>\$\$[\s\S]*?\$\$)|(?<bracket>\\\[[\s\S]*?\\\])|(?<inline>\\\([\s\S]*?\\\))|(?<single>(?<![\\$])\$(?!\$)[^\n$]*?(?<!\\)\$(?!\$))/g
  for (const match of text.matchAll(pattern)) {
    if (match.groups.literal) continue
    const kind = Object.keys(match.groups).find(key => match.groups[key] !== undefined)
    const width = kind === 'single' ? 1 : 2
    spans.push({ start: match.index, end: match.index + match[0].length, kind, body: match[0].slice(width, -width) })
  }
  return spans
}

function escapedAt(source, index) {
  let slashes = 0
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor--) slashes++
  return slashes % 2 === 1
}

/** Literal source regions cannot establish mathematical or table expectations. */
function literalRanges(source) {
  const ranges = []
  let offset = 0
  let fence
  for (const line of source.match(/[^\n]*(?:\n|$)/g) ?? []) {
    const marker = line.match(/^\s{0,3}(`{3,}|~{3,})/)
    if (!fence && marker) fence = { start: offset, marker: marker[1][0], length: marker[1].length }
    else if (fence && marker && marker[1][0] === fence.marker && marker[1].length >= fence.length) {
      ranges.push([fence.start, offset + line.length])
      fence = undefined
    }
    offset += line.length
  }
  if (fence) ranges.push([fence.start, source.length])
  let cursor = 0
  while (cursor < source.length) {
    const protectedRange = ranges.find(([start, end]) => start <= cursor && cursor < end)
    if (protectedRange) {
      cursor = protectedRange[1]
      continue
    }
    const url = source.slice(cursor).match(/^(?:https?|ftp):\/\/[^\s<>]+/i)
    if (url) {
      ranges.push([cursor, cursor + url[0].length])
      cursor += url[0].length
      continue
    }
    if (source[cursor] === '`' && !escapedAt(source, cursor)) {
      let length = 1
      while (source[cursor + length] === '`') length++
      const marker = '`'.repeat(length)
      let close = source.indexOf(marker, cursor + length)
      while (close >= 0 && (source[close - 1] === '`' || source[close + length] === '`')) close = source.indexOf(marker, close + length)
      if (close >= 0 && !ranges.some(([start, end]) => start < close + length && end > cursor)) {
        ranges.push([cursor, close + length])
        cursor = close + length
        continue
      }
    }
    cursor++
  }
  return ranges
}

function presentationMath(source) {
  const literals = literalRanges(source)
  return mathSpans(source).filter(span => span.kind !== 'single'
    && !escapedAt(source, span.start)
    && !literals.some(([start, end]) => start < span.end && end > span.start))
}

function attribute(node, name) {
  return node.attrs?.find(attr => attr.name === name)?.value ?? ''
}

function hasClass(node, name) {
  return attribute(node, 'class').split(/\s+/).includes(name)
}

function walk(node, visit) {
  if (visit(node) === false) return
  for (const child of node.childNodes ?? []) walk(child, visit)
}

function textContent(node) {
  if (node.nodeName === '#text') return node.value
  return (node.childNodes ?? []).map(textContent).join('')
}

/** Compare semantic MathML, not TeX spelling (\vert and | are equivalent). */
function semanticMathKey(node) {
  if (node.nodeName === '#text') return node.value.replace(/\u00a0/g, ' ')
  if (node.tagName === 'annotation' || node.tagName === 'annotation-xml') return ''
  const body = (node.childNodes ?? []).map(semanticMathKey).join('')
  if (['math', 'semantics', 'mrow'].includes(node.tagName)) return body
  const variant = attribute(node, 'mathvariant')
  const tag = { munder: 'msub', mover: 'msup', munderover: 'msubsup' }[node.tagName] ?? node.tagName
  return `${tag}${variant ? `[${variant}]` : ''}(${body})`
}

function renderedFormulas(node) {
  const formulas = []
  walk(node, child => {
    if (child.tagName === 'math') {
      formulas.push(semanticMathKey(child))
      return false
    }
  })
  return formulas
}

const formulaKeys = new Map()
function formulaKey(source) {
  if (formulaKeys.has(source)) return formulaKeys.get(source)
  let key
  try {
    key = renderedFormulas(parseFragment(katex.renderToString(source, { throwOnError: true, strict: 'ignore', displayMode: true })))[0]
  } catch {
    // The isolated syntax audit records invalid TeX; it is not a semantic
    // rendering expectation and must not turn this material into an exception.
    key = undefined
  }
  formulaKeys.set(source, key)
  return key
}

function missingFormulas(expected, actual) {
  const available = new Map()
  for (const key of actual) available.set(key, (available.get(key) ?? 0) + 1)
  return expected.filter(span => {
    const key = formulaKey(span.body)
    if (key === undefined) return false
    const remaining = available.get(key) ?? 0
    if (!remaining) return true
    available.set(key, remaining - 1)
    return false
  }).map(span => span.body)
}

/** Semantic cells: an unescaped bar inside explicit math is not a column. */
function tableCells(line) {
  const protectedRanges = [...literalRanges(line), ...presentationMath(line).map(span => [span.start, span.end])]
  const separators = []
  for (let index = 0; index < line.length; index++) {
    if (line[index] === '|' && !escapedAt(line, index)
      && !protectedRanges.some(([start, end]) => start <= index && index < end)) separators.push(index)
  }
  if (!separators.length) return null
  const cells = []
  let start = 0
  for (const separator of separators) {
    cells.push(line.slice(start, separator).trim())
    start = separator + 1
  }
  cells.push(line.slice(start).trim())
  if (separators[0] === line.search(/\S/)) cells.shift()
  if (!line.slice(separators.at(-1) + 1).trim()) cells.pop()
  return cells
}

function sourceTables(source) {
  const lines = source.split('\n')
  const literals = literalRanges(source)
  const tables = []
  let offset = 0
  for (let index = 0; index < lines.length - 1; index++) {
    const line = lines[index]
    const inLiteral = literals.some(([start, end]) => start <= offset && offset < end)
    offset += line.length + 1
    if (inLiteral) continue
    const header = tableCells(line)
    const separator = tableCells(lines[index + 1])
    if (!header || !separator || header.length !== separator.length
      || !separator.every(cell => /^:?-+:?$/.test(cell))) continue
    const rows = [{ source: line, cells: header }]
    let rowIndex = index + 2
    while (rowIndex < lines.length && lines[rowIndex].trim()) {
      const cells = tableCells(lines[rowIndex])
      if (!cells || /^ {4}|\t|^\s{0,3}(?:```|~~~)/.test(lines[rowIndex])) break
      rows.push({ source: lines[rowIndex], cells })
      rowIndex++
    }
    tables.push({ source: lines.slice(index, rowIndex).join('\n'), columns: header.length, rows })
    // Advance the source offset along with the table, including its separator.
    for (let consumed = index + 1; consumed < rowIndex; consumed++) offset += lines[consumed].length + 1
    index = rowIndex - 1
  }
  return tables
}

function visibleProse(node) {
  if (node.nodeName === '#text') return node.value
  if (hasClass(node, 'katex') || hasClass(node, 'katex-error')
    || ['math', 'code', 'pre', 'kbd', 'samp', 'script', 'style', 'button'].includes(node.tagName)) return ''
  const text = (node.childNodes ?? []).map(visibleProse).join('')
  return /^(?:p|div|tr|td|th|li|h[1-6]|blockquote|br)$/.test(node.tagName ?? '') ? `${text}\n` : text
}

function presentationContent(node) {
  if (node.nodeName === '#text') return node.value
  if (node.tagName === 'button' || node.tagName === 'script' || node.tagName === 'style') return ''
  if (hasClass(node, 'katex')) return `[math:${renderedFormulas(node).join(';')}]`
  if (node.tagName === 'br') return ' '
  return (node.childNodes ?? []).map(presentationContent).join('')
}

function contentKey(node) {
  return presentationContent(node).replace(/\s+/g, ' ').trim()
}

const cellContentKeys = new Map()
function expectedCellContent(formatted) {
  if (!cellContentKeys.has(formatted)) {
    // A prefix establishes inline Markdown context, as in a GFM cell, without
    // exposing expected content to the very pipe-splitting bug under audit.
    const prefix = 'audit-cell-content '
    const html = renderToStaticMarkup(createElement(formatter.RichText, { text: prefix + formatted, normalizeFormulas: false }))
    cellContentKeys.set(formatted, contentKey(parseFragment(html)).slice(prefix.trimEnd().length).trimStart())
  }
  return cellContentKeys.get(formatted)
}

function inspectPresentation(source, formatted, location) {
  const detail = { ...location, rendered_math: 0, rendered_tables: 0 }
  materialRendering.push(detail)
  const firstFinding = findings.length
  try {
    const warnings = []
    const previousWarn = console.warn
    let html
    try {
      console.warn = (...args) => warnings.push(args.map(String).join(' '))
      html = renderToStaticMarkup(createElement(formatter.RichText, {
        text: formatted,
        className: `evidence-textbook${location.content_format === 'text' ? ' evidence-plain-lines' : ''}`,
        normalizeFormulas: false,
      }))
    } finally {
      console.warn = previousWarn
    }
    detail.render_diagnostics = [...new Set(warnings)]
    for (const error of detail.render_diagnostics) {
      if (/Unrecognized Unicode character|No character metrics/.test(error)) findings.push({ ...location, kind: 'rendered_unsupported_glyph', source, error })
    }
    const tree = parseFragment(html)
    const tables = []
    const errors = []
    walk(tree, node => {
      if (hasClass(node, 'katex')) detail.rendered_math++
      if (hasClass(node, 'katex-error')) errors.push({ formula: textContent(node), error: attribute(node, 'title') })
      if (node.tagName === 'table') tables.push(node)
    })
    detail.rendered_tables = tables.length
    counts.rendered_materials++
    counts.rendered_math += detail.rendered_math
    counts.rendered_tables += detail.rendered_tables
    for (const error of errors) findings.push({ ...location, kind: 'rendered_katex_error', source, ...error })
    const expected = presentationMath(formatted)
    const missing = missingFormulas(expected, renderedFormulas(tree))
    if (missing.length) findings.push({ ...location, kind: 'missing_rendered_math', source, expected_math: expected.length, rendered_math: detail.rendered_math, missing_formulas: missing })
    let prose = visibleProse(tree).replace(/(?:https?|ftp):\/\/[^\s<>]+/gi, '')
    // Explicitly escaped source delimiters are literal Markdown, just like code.
    for (const match of source.matchAll(/\\\\([([])[\s\S]*?\\\\([)\]])|(?:\\\$){2,}/g)) {
      const literal = match[0].replace(/\\\\/g, '\\').replace(/\\\$/g, '$')
      prose = prose.replaceAll(literal, '')
    }
    const leftovers = [...prose.matchAll(/\$\$|\\[()[\]]|[A-Za-z][_^]\{[^}\n]+\}|\\(?:[dt]?frac|sqrt|[ct]?frac|int|iint|oint|sum|prod|lim|begin|end|mathrm|mathbf|mathit|mathcal|operatorname|text|left|right|overline|underline|hat|bar|vec|alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega|infty|cdot|times|leq|geq|ne|pm|vert|lvert|rvert|tag|label)\b/g)]
    if (leftovers.length) findings.push({ ...location, kind: 'rendered_math_leftover', source, rendered_excerpt: prose.slice(Math.max(0, leftovers[0].index - 100), leftovers[0].index + 300), tokens: [...new Set(leftovers.map(match => match[0]))] })
    const expectedTables = sourceTables(source)
    if (expectedTables.length !== tables.length) findings.push({ ...location, kind: 'rendered_table_count_mismatch', source, expected_tables: expectedTables.length, rendered_tables: tables.length })
    for (let tableIndex = 0; tableIndex < Math.min(expectedTables.length, tables.length); tableIndex++) {
      const expectedTable = expectedTables[tableIndex]
      const actualRows = []
      walk(tables[tableIndex], node => {
        if (node.tagName === 'tr') actualRows.push((node.childNodes ?? []).filter(child => child.tagName === 'th' || child.tagName === 'td'))
      })
      if (actualRows.length !== expectedTable.rows.length) findings.push({ ...location, kind: 'rendered_table_row_mismatch', source: expectedTable.source, table: tableIndex + 1, expected_rows: expectedTable.rows.length, rendered_rows: actualRows.length })
      for (let rowIndex = 0; rowIndex < Math.min(expectedTable.rows.length, actualRows.length); rowIndex++) {
        const row = expectedTable.rows[rowIndex]
        const cells = actualRows[rowIndex]
        if (cells.length !== expectedTable.columns || row.cells.length > expectedTable.columns) findings.push({ ...location, kind: 'rendered_table_cell_boundary', source: row.source, table: tableIndex + 1, row: rowIndex + 1, expected_columns: expectedTable.columns, source_cells: row.cells.length, rendered_cells: cells.length })
        for (let cellIndex = 0; cellIndex < Math.min(row.cells.length, cells.length); cellIndex++) {
          const cell = row.cells[cellIndex]
          const formattedCell = formatter.formatTextbookMarkdown(cell, { structure: false })
          const expectedMath = presentationMath(formattedCell)
          const missingMath = missingFormulas(expectedMath, renderedFormulas(cells[cellIndex]))
          if (missingMath.length) findings.push({ ...location, kind: 'rendered_table_cell_math_mismatch', source: row.source, cell_source: cell, table: tableIndex + 1, row: rowIndex + 1, column: cellIndex + 1, missing_formulas: missingMath })
          else {
            const expectedContent = expectedCellContent(formattedCell)
            const actualContent = contentKey(cells[cellIndex])
            if (expectedContent !== actualContent) findings.push({ ...location, kind: 'rendered_table_cell_content_mismatch', source: row.source, cell_source: cell, table: tableIndex + 1, row: rowIndex + 1, column: cellIndex + 1, expected_content: expectedContent, rendered_content: actualContent })
          }
        }
      }
    }
  } catch (error) {
    counts.renderer_exceptions++
    detail.error = String(error?.stack ?? error)
    findings.push({ ...location, kind: 'material_renderer_exception', source, error: detail.error })
  }
  detail.findings = findings.length - firstFinding
}

function inspectFormula(source, location, displayMode = /\\tag\b/.test(source)) {
  if (/\*\*\s*[A-Za-z0-9({]/.test(source)) findings.push({ ...location, kind: 'python_exponent_in_math', source })
  const wrapped = formatter.formatTextbookMarkdown(`\\(${source}\\)`, { structure: false })
  const normalized = mathSpans(wrapped)
  const formula = normalized.length === 1 ? normalized[0].body : source
  const warnings = []
  const previousWarn = console.warn
  let error
  try {
    // Keep diagnostics in the report instead of losing them in thousands of stderr lines.
    console.warn = (...args) => warnings.push(args.map(String).join(' '))
    katex.renderToString(formula, { throwOnError: true, strict: 'ignore', displayMode })
  } catch (failure) {
    error = String(failure.message)
  } finally {
    console.warn = previousWarn
  }
  if (error) findings.push({ ...location, kind: 'invalid_latex', source, error })
  for (const warning of new Set(warnings)) findings.push({ ...location, kind: 'unsupported_glyph', source, error: warning })
}

function inspectText(text, location, metadata = {}, display = false) {
  const rendered = display ? formatter.formatTextbookMarkdown(text, { structure: metadata.content_format !== 'markdown' }) : text
  const spans = mathSpans(rendered)
  if (display) counts.displayed_formulas += spans.length
  for (const span of spans) inspectFormula(span.body, location, span.kind === 'display' || span.kind === 'bracket')
  const outside = rendered.split('')
  const literal = /```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`/g
  for (const match of rendered.matchAll(literal)) outside.fill(' ', match.index, match.index + match[0].length)
  for (const span of spans) outside.fill(' ', span.start, span.end)
  const rest = outside.join('')
  if (/\\[\[(]|\\[\])]|\$\$/.test(rest)) findings.push({ ...location, kind: 'unmatched_math_delimiter', source: text.slice(0, 2000), rendered_excerpt: rest.slice(0, 2000) })
  if (display && /(?<![\\$])\$(?!\$)\\(?:frac|int|sum|lim|begin|mathrm|operatorname)\b/.test(rest)) findings.push({ ...location, kind: 'unrendered_dollar_math', source: text.slice(0, 2000), rendered_excerpt: rest.slice(0, 2000) })
  if (/【视觉校订公式】/.test(text)) findings.push({ ...location, kind: 'unreplaced_ocr_with_appended_formulas', source: text.slice(0, 2000) })
  if (display) inspectPresentation(text, rendered, { ...location, content_format: metadata.content_format ?? 'text' })
}

const materialsById = new Map(snapshot.materials.map(row => [row.id, row]))
for (const material of snapshot.materials) {
  counts.materials++
  const metadata = typeof material.metadata_json === 'string' ? JSON.parse(material.metadata_json || '{}') : material.metadata_json ?? {}
  const location = { field: 'material_display', material_id: material.id, document_id: material.source_document_id, page: material.page_number }
  inspectText(material.content, location, metadata, true)
}
for (const chunk of snapshot.chunks) {
  counts.chunks++
  const material = materialsById.get(chunk.material_id)
  inspectText(chunk.text, { field: 'chunk', chunk_id: chunk.id, material_id: chunk.material_id, document_id: material?.source_document_id, page: material?.page_number })
}
for (const formula of snapshot.formulas) {
  counts.formula_records++
  const material = materialsById.get(formula.material_id)
  inspectFormula(formula.raw_formula, { field: 'canonical_formula', formula_id: formula.id, material_id: formula.material_id, document_id: material?.source_document_id, page: material?.page_number })
}
for (const sidecar of snapshot.sidecars) {
  for (const entry of sidecar.review.reviewed_formulas ?? []) {
    const source = typeof entry === 'string' ? entry : entry.source
    if (typeof source !== 'string' || !source.trim()) continue
    counts.reviewed_formulas++
    inspectFormula(source, { field: 'reviewed_formula', document_id: sidecar.document_id ?? 1, page: sidecar.page, path: sidecar.path })
  }
}
const byKind = {}
for (const finding of findings) byKind[finding.kind] = (byKind[finding.kind] ?? 0) + 1
await writeFile(outputPath, JSON.stringify({ counts, by_kind: byKind, findings, material_rendering: materialRendering }, null, 2), 'utf8')
console.log(JSON.stringify({ counts, by_kind: byKind, output: outputPath }))
