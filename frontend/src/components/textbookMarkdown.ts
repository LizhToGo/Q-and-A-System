/**
 * Conservative presentation pass for OCR/textbook excerpts.
 *
 * It deliberately does not try to infer prose as mathematics: only complete,
 * recognisable tokens and operator expressions are upgraded. Markdown, code
 * and URLs are protected; explicit math keeps its grouping and delimiters.
 */

type Span = { start: number; end: number; value: string }

const TOKEN_START = '\uE000'
const TOKEN_END = '\uE001'

const GREEK: Record<string, string> = {
  'α': '\\alpha', 'β': '\\beta', 'γ': '\\gamma', 'δ': '\\delta',
  'ε': '\\epsilon', 'ϵ': '\\varepsilon', 'ζ': '\\zeta', 'η': '\\eta',
  'θ': '\\theta', 'ϑ': '\\vartheta', 'ι': '\\iota', 'κ': '\\kappa',
  'λ': '\\lambda', 'μ': '\\mu', 'ν': '\\nu', 'ξ': '\\xi',
  'ο': 'o', 'π': '\\pi', 'ρ': '\\rho', 'ς': '\\varsigma', 'σ': '\\sigma',
  'τ': '\\tau', 'υ': '\\upsilon', 'φ': '\\phi', 'ϕ': '\\varphi',
  'χ': '\\chi', 'ψ': '\\psi', 'ω': '\\omega',
  'Γ': '\\Gamma', 'Δ': '\\Delta', 'Θ': '\\Theta', 'Λ': '\\Lambda',
  'Ξ': '\\Xi', 'Π': '\\Pi', 'Σ': '\\Sigma', 'Υ': '\\Upsilon',
  'Φ': '\\Phi', 'Ψ': '\\Psi', 'Ω': '\\Omega',
}

const SUBSCRIPT: Record<string, string> = {
  '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4', '₅': '5',
  '₆': '6', '₇': '7', '₈': '8', '₉': '9', '₊': '+', '₋': '-',
  '₌': '=', '₍': '(', '₎': ')', 'ₐ': 'a', 'ₑ': 'e', 'ₕ': 'h',
  'ᵢ': 'i', 'ⱼ': 'j', 'ₖ': 'k', 'ₗ': 'l', 'ₘ': 'm', 'ₙ': 'n',
  'ₒ': 'o', 'ₚ': 'p', 'ᵣ': 'r', 'ₛ': 's', 'ₜ': 't', 'ᵤ': 'u',
  'ᵥ': 'v', 'ₓ': 'x',
}

const SUPERSCRIPT: Record<string, string> = {
  '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5',
  '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9', '⁺': '+', '⁻': '-',
  '⁼': '=', '⁽': '(', '⁾': ')', 'ⁿ': 'n', 'ⁱ': 'i',
}

const SUB_CHARS = Object.keys(SUBSCRIPT).join('')
const SUPER_CHARS = Object.keys(SUPERSCRIPT).join('')
const MATH_OPERATOR = /[=<>≤≥≠≈±×÷+\-−*/^·∑∏∫∮√∞∈→]/
function escapedAt(source: string, index: number) {
  let slashes = 0
  for (let i = index - 1; i >= 0 && source[i] === '\\'; i -= 1) slashes += 1
  return slashes % 2 === 1
}

function balanced(source: string) {
  const pairs: Record<string, string> = { '(': ')', '[': ']', '{': '}' }
  const stack: string[] = []
  for (const char of source) {
    if (pairs[char]) stack.push(pairs[char])
    else if (char === ')' || char === ']' || char === '}') {
      if (stack.pop() !== char) return false
    }
  }
  return stack.length === 0
}

function nextUnescaped(source: string, needle: string, start: number) {
  let position = source.indexOf(needle, start)
  while (position >= 0 && escapedAt(source, position)) position = source.indexOf(needle, position + needle.length)
  return position
}

/** Single dollars are accepted only in textbook evidence, not general answers. */
function textbookDollarMath(body: string) {
  if (!body.trim() || body !== body.trim() || !balanced(body)) return false
  // This legacy convention is same-line only. Never pair a price with math
  // on a later line or consume inline code/URLs as a formula.
  if (/[\n`\u3400-\u9fff]|(?:https?|ftp):\/\//i.test(body)) return false
  if (/\\[A-Za-z]+/.test(body)) return true
  const words = body.match(/\b[A-Za-z]{3,}\b/g) ?? []
  if (words.some(word => !/^(?:DTFT|IDTFT|DFT|IDFT|FFT|sin|cos|tan|cot|sec|csc|lim|log|arg|min|max)$/.test(word))) return false
  return /^[A-Za-z]$/.test(body) || /[_^=<>≤≥≠≈α-ωΑ-Ωϵϑϕς∑∫]/.test(body)
}

/** Find regions whose contents must not be interpreted by this formatter. */
function protectedSpans(source: string): Span[] {
  const spans: Span[] = []
  let i = 0
  while (i < source.length) {
    const lineStart = i === 0 || source[i - 1] === '\n'
    const fence = lineStart ? source.slice(i).match(/^\s{0,3}(`{3,}|~{3,})/) : null
    if (fence) {
      const marker = fence[1][0]
      const length = fence[1].length
      const close = new RegExp(`(?:^|\\n)\\s{0,3}${marker}{${length},}\\s*(?:\\n|$)`, 'g')
      close.lastIndex = i + fence[0].length
      const match = close.exec(source)
      const end = match ? match.index + match[0].length : source.length
      spans.push({ start: i, end, value: source.slice(i, end) })
      i = end
      continue
    }
    if (source.startsWith('$$', i)) {
      const close = nextUnescaped(source, '$$', i + 2)
      if (close >= 0) {
        const end = close + 2
        spans.push({ start: i, end, value: `$$${normalizeMathSymbols(source.slice(i + 2, close))}$$` })
        i = end
        continue
      }
    }
    if (source[i] === '$' && source[i + 1] !== '$' && !escapedAt(source, i)) {
      const close = nextUnescaped(source, '$', i + 1)
      const body = close >= 0 ? source.slice(i + 1, close) : ''
      if (close >= 0 && source[close + 1] !== '$' && !body.includes('\n')) {
        spans.push({ start: i, end: close + 1, value: textbookDollarMath(body) ? `\\(${normalizeMathSymbols(body)}\\)` : source.slice(i, close + 1) })
        i = close + 1
        continue
      }
    }
    if ((source.startsWith('\\(', i) || source.startsWith('\\[', i)) && !escapedAt(source, i)) {
      const opening = source.slice(i, i + 2)
      const closing = opening === '\\(' ? '\\)' : '\\]'
      const close = nextUnescaped(source, closing, i + 2)
      if (close >= 0) {
        spans.push({ start: i, end: close + 2, value: `${opening}${normalizeMathSymbols(source.slice(i + 2, close))}${closing}` })
        i = close + 2
        continue
      }
    }
    if (source[i] === '`' && !escapedAt(source, i)) {
      let run = 1
      while (source[i + run] === '`') run += 1
      const marker = '`'.repeat(run)
      const close = nextUnescaped(source, marker, i + run)
      if (close >= 0) {
        spans.push({ start: i, end: close + run, value: source.slice(i, close + run) })
        i = close + run
        continue
      }
    }
    const url = source.slice(i).match(/^(?:https?:\/\/|ftp:\/\/)[^\s<>)]+/i)
    if (url) {
      spans.push({ start: i, end: i + url[0].length, value: url[0] })
      i += url[0].length
      continue
    }
    // A backslash separating scripted Karnaugh-map axes is literal, not an
    // escape for the mathematical delimiter introduced around its next atom.
    if (source[i] === '\\' && /^\\[A-Z](?:[₀-₉]|[_^]|[A-Z])/.test(source.slice(i))) {
      spans.push({ start: i, end: i + 1, value: '&#92;' })
      i += 1
      continue
    }
    const emphasis = source.slice(i).match(/^(?:\*\*|__)[^\n]+?(?:\*\*|__)|^\*[^\n*]+\*/)
    const product = source[i] === '*' && source[i + 1] !== '*' && /[A-Za-z0-9)}\]α-ωΑ-Ω]/.test(source[i - 1] ?? '') && /[A-Za-z0-9({α-ωΑ-Ω]/.test(source[i + 1] ?? '')
    if (emphasis && !product && source[i - 1] !== '^' && source[i + emphasis[0].length - 2] !== '^') {
      spans.push({ start: i, end: i + emphasis[0].length, value: emphasis[0] })
      i += emphasis[0].length
      continue
    }
    // Keep Markdown links/images intact, including math-looking link targets.
    const link = source.slice(i).match(/^!?\[[^\]\n]*\]\([^\)\n]*\)/)
    if (link) {
      spans.push({ start: i, end: i + link[0].length, value: link[0] })
      i += link[0].length
      continue
    }
    i += 1
  }
  return spans
}

function putTokens(source: string, spans: Span[]) {
  if (spans.length === 0) return { text: source, values: [] as string[] }
  const values: string[] = []
  let output = ''
  let cursor = 0
  spans.forEach((span, index) => {
    if (span.start < cursor) return
    output += source.slice(cursor, span.start)
    values.push(span.value)
    output += `${TOKEN_START}${index}${TOKEN_END}`
    cursor = span.end
  })
  output += source.slice(cursor)
  return { text: output, values }
}

function restoreTokens(source: string, values: string[]) {
  return source.replace(new RegExp(`${TOKEN_START}(\\d+)${TOKEN_END}`, 'g'), (_, index: string) => values[Number(index)] ?? '')
}

const MATH_SYMBOLS: Record<string, string> = {
  ...GREEK,
  '≤': '\\leq', '≥': '\\geq', '≠': '\\ne', '≈': '\\approx',
  '±': '\\pm', '×': '\\times', '÷': '\\div', '−': '-',
  '∑': '\\sum', '∫': '\\int', '∞': '\\infty', '∈': '\\in',
  '→': '\\to', '∮': '\\oint', '∏': '\\prod',
  '¼': '\\frac{1}{4}', '½': '\\frac{1}{2}', '¾': '\\frac{3}{4}',
  '‖': '\\Vert', '′': "'", '″': "''", '‴': "'''",
  'Ⓛ': '\\mathbin{\\text{\\textcircled{L}}}', '‰': '\\cdot10^{-3}',
  'µ': '\\mu', '℃': '^{\\circ}\\mathrm{C}',
  '①': '\\text{\\textcircled{1}}', '②': '\\text{\\textcircled{2}}', '③': '\\text{\\textcircled{3}}',
  '④': '\\text{\\textcircled{4}}', '⑤': '\\text{\\textcircled{5}}', '⑥': '\\text{\\textcircled{6}}',
  '⑦': '\\text{\\textcircled{7}}', '⑧': '\\text{\\textcircled{8}}', '⑨': '\\text{\\textcircled{9}}', '⑩': '\\text{\\textcircled{10}}',
  '（': '\\text{（}', '）': '\\text{）}', '，': '\\text{，}', '、': '\\text{、}', '–': '\\text{–}',
  '。': '\\text{。}', '；': '\\text{；}', '【': '\\text{【}', '】': '\\text{】}',
}

const MATH_ACCENTS: Record<string, string> = { '\u0302': 'hat', '\u0303': 'tilde', '\u0304': 'bar', '\u0305': 'bar' }

function mathCommand(command: string, following: string) {
  return command + (/^\\[A-Za-z]+$/.test(command) && /^[A-Za-z]/.test(following) ? ' ' : '')
}

/** Linear radical notation consumes only an explicit group or one scripted atom. */
function normalizeRadicals(source: string) {
  if (!source.includes('√')) return source
  const groups: Record<string, string> = { '(': ')', '[': ']', '{': '}' }
  let output = ''
  let cursor = 0
  while (cursor < source.length) {
    if (source.startsWith('\\text{', cursor) || source.startsWith('\\mbox{', cursor)) {
      let depth = 1
      let end = cursor + 6
      for (; end < source.length && depth; end += 1) {
        if (escapedAt(source, end)) continue
        if (source[end] === '{') depth += 1
        if (source[end] === '}') depth -= 1
      }
      if (depth === 0) {
        output += source.slice(cursor, end)
        cursor = end
        continue
      }
    }
    if (source[cursor] !== '√') {
      output += source[cursor++]
      continue
    }
    let start = cursor + 1
    while (source[start] === ' ') start += 1
    const closing = groups[source[start]]
    if (closing) {
      let depth = 1
      let end = start + 1
      for (; end < source.length; end += 1) {
        if (escapedAt(source, end)) continue
        if (source[end] === source[start]) depth += 1
        if (source[end] === closing && --depth === 0) break
      }
      if (depth === 0) {
        output += `\\sqrt{${normalizeRadicals(source.slice(start + 1, end))}}`
        cursor = end + 1
        continue
      }
    }
    const atom = source.slice(start).match(/^(?:\d+(?:\.\d+)?|[A-Za-zα-ωΑ-Ω])(?:[_^](?:\{[^{}]+\}|[A-Za-z0-9]+)|[₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ]+)*/)?.[0]
    if (atom) {
      output += `\\sqrt{${atom}}`
      cursor = start + atom.length
    } else {
      // An unbound root sign has no inferable radicand; preserve its symbol.
      output += '\\surd '
      cursor += 1
    }
  }
  return output
}

/** Replace symbols without changing TeX's explicit script/grouping semantics. */
function normalizeMathSymbols(source: string) {
  // KaTeX does not support partial array rules; retain row separation and values.
  const scientificText = source.replace(/\\text\{([^{}]*)\}/g, (whole, body: string) => {
    // Greek units are math symbols, not text-mode commands; keep their upright
    // Latin unit letters and original spaces without converting ordinary prose.
    if (!/[α-ωΑ-Ωϵϑϕςµ‰℃]/.test(body) && !/\\(?:mu|Omega|alpha|beta|gamma|delta|pi|omega)\b/.test(body)) return whole
    return `\\mathrm{${body.replace(/(^|[^\\]) +/g, '$1\\ ')}}`
  })
  const compatible = normalizeRadicals(scientificText.replace(/(?:\\cline\{\s*\d+\s*-\s*\d+\s*\}\s*)+/g, '\\hline\n'))
  const accented = compatible.replace(/(\\(?:alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi|rho|sigma|tau|phi|psi|omega|Gamma|Delta|Theta|Lambda|Pi|Sigma|Phi|Psi|Omega)|[A-Za-zα-ωΑ-Ωϵϑϕς])([\u0302-\u0305])/g, (_, base: string, accent: string) => `\\${MATH_ACCENTS[accent]}{${base}}`)
  const textRanges: Array<[number, number]> = []
  for (const match of accented.matchAll(/\\(?:text|mbox)\{/g)) {
    if (textRanges.some(([start, end]) => start <= match.index! && end > match.index!)) continue
    let depth = 1
    let end = match.index! + match[0].length
    for (; end < accented.length && depth; end += 1) {
      if (escapedAt(accented, end)) continue
      if (accented[end] === '{') depth += 1
      if (accented[end] === '}') depth -= 1
    }
    if (depth === 0) textRanges.push([match.index!, end])
  }
  return accented.replace(/[α-ωΑ-Ωϵϑϕς≤≥≠≈±×÷−∑∏∫∮∞∈→¼½¾‖′″‴Ⓛ‰µ℃①-⑩（），、–。；【】À-ÿĀ-ž]|[\u3400-\u9fff]+/g, (char, offset: number) => {
    if (textRanges.some(([start, end]) => start <= offset && end > offset)) {
      if (/^[①-⑩Ⓛ]$/.test(char)) return `\\textcircled{${char === 'Ⓛ' ? 'L' : char.charCodeAt(0) - 0x245f}}`
      return char
    }
    if (/^[\u3400-\u9fff]+$/.test(char)) return `\\text{${char}}`
    const decomposed = char.normalize('NFD').match(/^([A-Za-z])([\u0302-\u0305])$/)
    if (decomposed) return `\\${MATH_ACCENTS[decomposed[2]]}{${decomposed[1]}}`
    return mathCommand(MATH_SYMBOLS[char] ?? char, accented.slice(offset + 1, offset + 2))
  })
}

function toLatex(source: string) {
  let output = source
  // A following scripted factor belongs outside this script (a^nz^{-n},
  // f_sT_0). Sigma is a separate symbol, never an inferred summation command.
  output = output.replace(/([A-Za-zα-ωΑ-Ωϵϑϕς]|\\[A-Za-z]+)\^([+-]?(?:[A-Za-z0-9α-ωΑ-ΡΤ-Ωϵϑϕς](?![_^])|\\(?!Sigma\b|sum\b)[A-Za-z]+(?![_^]))+)/g, '$1^{$2}')
  output = output.replace(new RegExp(`([A-Za-zα-ωΑ-Ωϵϑϕς](?:[′″‴\\u0302-\\u0305]+)?|\\\\[A-Za-z]+)([${SUB_CHARS}]+)`, 'g'), (_, base: string, chars: string) => {
    const subscript = [...chars].map((char) => SUBSCRIPT[char] ?? char).join('')
    return `${base}_{${subscript}}`
  })
  output = output.replace(/([A-Za-zα-ωΑ-Ωϵϑϕς]|\\[A-Za-z]+)_([A-Za-z0-9]+)/g, (match, base: string, script: string, offset: number) => {
    const following = output[offset + match.length]
    const factor = script.slice(-1)
    if (script.length > 1 && (following === '_' || (following === '^' && /[A-Za-z]/.test(factor) && /[0-9A-Z]/.test(script)))) {
      return `${base}_{${script.slice(0, -1)}}${factor}`
    }
    return `${base}_{${script}}`
  })
  output = output.replace(/([A-Za-z])̂/g, '\\hat{$1}')
  output = output.replace(/(\})_([A-Za-z0-9]+)/g, '$1_{$2}')
  output = output.replace(/_([\u3400-\u9fff]+)/g, '_{$1}')
  output = normalizeMathSymbols(output)
  output = output.replace(new RegExp(`([A-Za-z0-9)}]|\\[A-Za-z]+)([${SUPER_CHARS}]+)`, 'g'), (_, base: string, chars: string) => {
    const exponent = [...chars].map((char) => SUPERSCRIPT[char] ?? char).join('')
    return `${base}^{${exponent}}`
  })
  output = output.replace(/\b(sin|cos|tan|cot|sec|csc|log|ln|lim)(?=\s*\()/g, '\\$1')
  output = output.replace(/\blim(?=[_^])/g, '\\lim')
  // Braced numerator/denominator is unambiguously a fraction.  Do not touch
  // ordinary braces because they can be explanatory textbook notation.
  output = output.replace(/\{([^{}]+)\}\s*\/\s*\{([^{}]+)\}/g, '\\frac{$1}{$2}')
  return output
}

function trimMathCandidate(candidate: string) {
  let value = candidate.trim()
  while (/^[,.;，。；]$/.test(value.slice(-1))) value = value.slice(0, -1).trim()
  return value
}

function likelyMath(candidate: string) {
  if (!candidate || candidate.includes(TOKEN_START) || !balanced(candidate)) return false
  if (/[_^]\s*$/.test(candidate)) return false
  if (!/[A-Za-zα-ωΑ-Ωϵϑϕς₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ]/.test(candidate)) return false
  if (!MATH_OPERATOR.test(candidate) && !/[α-ωΑ-Ωϵϑϕς₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ_^′″‴\u0302-\u0305]/.test(candidate)) return false
  // Bare words, acronyms, units, and section numbers are prose.
  if (/^[A-Za-z]+$/.test(candidate) || /^(?:kHz|Hz|MHz|GHz|ms|μs|dB|DFT|FFT|FIR|IIR|MATLAB)$/i.test(candidate)) return false
  if (/^section\d(?:\.\d+)+$/i.test(candidate)) return false
  return true
}

function candidateSpans(line: string): Array<[number, number]> {
  const spans: Array<[number, number]> = []
  const incomplete: Array<[number, number]> = []
  const occupied = (start: number, end: number) => spans.some(([a, b]) => a < end && b > start) || incomplete.some(([a, b]) => a < end && b > start)
  // Complete operator expressions (equation chains and explicit fractions).
  const expression = /[A-Za-z0-9_{}()[\]\\|+\-−*/=<>≤≥≠≈±×÷·∑∏∫∮√∞∈→^₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿα-ωΑ-ΩϵϑϕςπΩ′″‴\u0302-\u0305\s.,]+/g
  for (const match of line.matchAll(expression)) {
    const value = trimMathCandidate(match[0])
    const start = match.index! + match[0].indexOf(value)
    const end = start + value.length
    if (!value || occupied(start, end)) continue
    if (!balanced(value)) {
      if (!line.slice(end).trim()) incomplete.push([start, end])
      continue
    }
    const hasChain = /[=<>≤≥≠≈]/.test(value)
    const hasFraction = /\{[^{}]+\}\s*\/\s*\{[^{}]+\}/.test(value)
    const hasMathName = /[_^₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿα-ωΑ-ΩϵϑϕςπΩ′″‴\u0302-\u0305]/.test(value)
    if ((hasChain || hasFraction || (hasMathName && MATH_OPERATOR.test(value))) && likelyMath(value)) spans.push([start, end])
  }
  const limit = /\blim_\{[^{}\n]+\}/g
  for (const match of line.matchAll(limit)) {
    const start = match.index!
    const end = start + match[0].length
    if (!occupied(start, end)) spans.push([start, end])
  }
  // A complete TeX accent and its scripted base are one atom, including when
  // mentioned in prose without an equation operator.
  const accent = /\\(?:bar|hat|vec|tilde|dot|ddot|overline|underline)\s*(?:\{[^{}\n]+\}|[A-Za-zα-ωΑ-Ω])(?:[_^](?:\{[^{}\n]+\}|[A-Za-z0-9]+))*(?:\([^()\n]*\))?/g
  for (const match of line.matchAll(accent)) {
    const start = match.index!
    const end = start + match[0].length
    if (!occupied(start, end)) spans.push([start, end])
  }
  // Keep primes/accents with their base and scripts, including Greek-prefix
  // variables such as Δf′₀; splitting at a modifier leaves a bare Unicode script.
  const variable = /(?<![A-Za-z0-9])(?:(?:[α-ωΑ-Ωϵϑϕς][A-Za-z]?|[A-Za-z])(?:[′″‴\u0302-\u0305]+|[_^](?:\{[^{}]+\}|[A-Za-z0-9]+|[\u3400-\u9fff]+)|[₀-₉]+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ]+)+(?:\([^()\n]*\))?|[α-ωΑ-Ωϵϑϕς](?:\([^()\n]*\))?(?![A-Za-z0-9]))/g
  for (const match of line.matchAll(variable)) {
    const start = match.index!
    const end = start + match[0].length
    if (!occupied(start, end) && likelyMath(match[0])) spans.push([start, end])
  }
  return spans.sort((a, b) => a[0] - b[0])
}

function transformMathLine(line: string, tableCell = false): string {
  if (!line) return line
  const trimmed = line.trim()
  // GFM separators are not absolute-value operators. Explicit math/code has
  // already been tokenized; never let a candidate cross a cell boundary.
  if (!tableCell && trimmed.startsWith('|') && trimmed.endsWith('|') && !escapedAt(trimmed, trimmed.length - 1)) {
    let output = ''
    let start = 0
    for (let index = 0; index < line.length; index += 1) {
      if (line[index] !== '|' || escapedAt(line, index)) continue
      output += transformMathLine(line.slice(start, index), true) + '|'
      start = index + 1
    }
    return output + transformMathLine(line.slice(start), true)
  }
  const prefix = line.slice(0, line.length - line.trimStart().length)
  const listPrefix = trimmed.match(/^((?:[-*+]\s+|\d+[.)]\s+))/)?.[1] ?? ''
  const formula = listPrefix ? trimmed.slice(listPrefix.length).trim() : trimmed
  const displayMatch = formula.match(/^(.*?\S)([，,。；;：:]*)$/)
  const displayCandidate = (displayMatch?.[1] ?? formula).replace(/\s+/g, ' ')
  const displaySuffix = displayMatch?.[2] ?? ''
  const display = !/^#{1,6}\s/.test(trimmed) && likelyMath(displayCandidate) && /(?:=|≤|≥|≠|≈)/.test(displayCandidate) && !/[\u3400-\u9fff]/.test(displayCandidate)
  if (display && balanced(displayCandidate)) return `${prefix}${listPrefix}$$${toLatex(displayCandidate)}$$${displaySuffix}`

  const spans = candidateSpans(line)
  if (spans.length === 0) return line
  let output = ''
  let cursor = 0
  for (const [start, end] of spans) {
    output += line.slice(cursor, start)
    output += `$$${toLatex(line.slice(start, end).trim())}$$`
    cursor = end
  }
  return output + line.slice(cursor)
}

function isHeading(line: string) {
  const value = line.trim().replace(/^#{1,6}\s+/, '')
  if (!value || /^[-*+]\s+/.test(value) || /^\d+[.)]\s+/.test(value)) return false
  return /^(?:学习要点|学习目标|知识要点|小结|习题|练习|思考题|参考答案|解法?(?:一|二|三|[1-3])?|解|第[^\n]{1,30}[章节部分])(?:\s*[:：。.])?$/.test(value)
    || /^(?:奈奎斯特|亚奈奎斯特)[^：:。]{0,20}(?:抽样)?定理$/.test(value)
    || /^[一二三四五六七八九十]+[、.．]\s*\S+/.test(value)
    || /^\d+(?:\.\d+)+\s+\S+/.test(value)
    || /^【(?:例|习题|练习|思考)[^】]*】/.test(value)
    || /^(?:例|习题|练习)\s*\d+(?:\.\d+)*\b/.test(value)
}

function splitTextbookBlocks(source: string) {
  let output = source
  // OCR often puts an entire example/solution on one physical line.  These
  // labels are semantic boundaries, unlike equation references such as
  // (1.4.19), so introducing whitespace here is lossless and idempotent.
  output = output.replace(/([^\n])(?=(?:学习要点|学习目标|知识要点|小结|解法?[一二三123]?|解)\s*[：:])/g, '$1\n\n')
  output = output.replace(/(学习要点|学习目标|知识要点|小结|解法?[一二三123]?|解)\s*([：:])(?=\S)/g, '$1$2\n\n')
  output = output.replace(/(【(?:例|习题|练习|思考)[^】]*】|解法?[一二三123]?\s*[:：]|习题[。.:：]?)(?=\S)/g, '$1\n\n')
  output = output.replace(/([^\n])(?=【(?:例|习题|练习|思考)[^】]*】|解法?[一二三123]?\s*[:：]|习题[。.:：]?)/g, (match, _character: string, offset: number, whole: string) => {
    const lineStart = whole.lastIndexOf('\n', offset) + 1
    return /^\s*#{1,6}\s+$/.test(whole.slice(lineStart, offset + 1)) ? match : `${match}\n\n`
  })
  return output
}

/** Reviewed pipe rows need explicit boundaries; GFM otherwise consumes prose. */
function separateTables(source: string) {
  const lines = source.split('\n')
  const separated: string[] = []
  const separator = /^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$/
  let inTable = false
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const row = line.trimStart().startsWith('|')
    const beginsTable = row && separator.test(lines[index + 1] ?? '')
    if ((beginsTable || (inTable && !row)) && separated.length && separated[separated.length - 1].trim()) separated.push('')
    if (beginsTable) inTable = true
    else if (!row) inTable = false
    separated.push(line)
  }
  return separated.join('\n')
}

function formatStructure(source: string) {
  const lines = source.split('\n')
  const formatted: string[] = []
  for (const original of lines) {
    if (!original.trim()) {
      if (formatted[formatted.length - 1] !== '') formatted.push('')
      continue
    }
    const value = original.trim()
    const alreadyMarkdown = /^(?:#{1,6}\s|[-*+]\s+|>\s|\d+[.)]\s+)/.test(value)
    const numbered = /^(?:[（(]\s*\d+\s*[）)]|[①②③④⑤⑥⑦⑧⑨⑩]|[（(][一二三四五六七八九十]+[）)])\s*/.test(value)
    let line = original
    if (!alreadyMarkdown && numbered && !/^\$\$/.test(value)) line = `${original.slice(0, original.length - original.trimStart().length)}- ${value}`
    if (!alreadyMarkdown && isHeading(line)) line = `${original.slice(0, original.length - original.trimStart().length)}### ${value}`
    const math = transformMathLine(line)
    const heading = /^\s*###\s/.test(math)
    const list = /^\s*[-*+]\s/.test(math)
    const previous = formatted[formatted.length - 1] ?? ''
    const previousList = /^\s*[-*+]\s/.test(previous)
    if ((heading || (list && !previousList)) && formatted.length && previous !== '') formatted.push('')
    formatted.push(math)
    if (heading) formatted.push('')
  }
  while (formatted.length && formatted[formatted.length - 1] === '') formatted.pop()
  return formatted.join('\n')
}

/** Convert a textbook excerpt to conservative, renderable Markdown. */
export function formatTextbookMarkdown(source: string, options: { structure?: boolean } = {}): string {
  if (!source) return source
  const normalized = source.replace(/\r\n?/g, '\n')
  const { text, values } = putTokens(normalized, protectedSpans(normalized))
  const bounded = separateTables(text)
  const formatted = options.structure === false ? bounded.split('\n').map(line => transformMathLine(line)).join('\n') : formatStructure(splitTextbookBlocks(bounded))
  return restoreTokens(formatted, values)
}

export default formatTextbookMarkdown
