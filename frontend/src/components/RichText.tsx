import { useEffect, useMemo, useState } from 'react'
import { Streamdown } from 'streamdown'
import { code as streamdownCode } from '@streamdown/code'
import { cjk as streamdownCjk } from '@streamdown/cjk'
import { createMathPlugin } from '@streamdown/math'
import 'streamdown/styles.css'
import 'katex/dist/katex.min.css'

const math = createMathPlugin({ singleDollarTextMath: false })

const STANDALONE_FORMULA = /^(\s*(?:[-*+]\s+)?)([A-Za-z][A-Za-z0-9_]*(?:\([^\n)]*\))?(?:\s*[_^][A-Za-z0-9{}]+)?\s*(?:=|>=|<=|≥|≤)\s*[^\n]+?)\s*$/

type SourceRange = [number, number]

function codeFenceRanges(source: string): SourceRange[] {
  const ranges: SourceRange[] = []
  let active: { start: number; marker: string; length: number } | null = null
  let offset = 0
  for (const line of source.match(/[^\n]*(?:\n|$)/g) ?? []) {
    const match = line.replace(/\r?\n$/, '').match(/^\s{0,3}(`{3,}|~{3,})/)
    const lineEnd = offset + line.length
    if (match && !active) {
      active = { start: offset, marker: match[1][0], length: match[1].length }
    } else if (match && active && match[1][0] === active.marker && match[1].length >= active.length) {
      ranges.push([active.start, lineEnd])
      active = null
    }
    offset = lineEnd
  }
  if (active) ranges.push([active.start, source.length])
  return ranges
}
/** Inline code and URL destinations are literal even when they contain TeX. */
function literalRanges(source: string) {
  const fences = codeFenceRanges(source)
  const ranges = [...fences]
  let cursor = 0
  let fenceIndex = 0
  while (cursor < source.length) {
    while (fenceIndex < fences.length && cursor >= fences[fenceIndex][1]) fenceIndex += 1
    const fence = fences[fenceIndex]
    if (fence && cursor >= fence[0]) {
      cursor = fence[1]
      continue
    }
    if (source[cursor] === '`' && !isEscaped(source, cursor)) {
      let length = 1
      while (source[cursor + length] === '`') length += 1
      const marker = '`'.repeat(length)
      let close = source.indexOf(marker, cursor + length)
      while (close >= 0 && (source[close - 1] === '`' || source[close + length] === '`')) {
        close = source.indexOf(marker, close + length)
      }
      if (close >= 0 && !isProtectedRange(cursor, close + length, fences)) {
        ranges.push([cursor, close + length])
        cursor = close + length
        continue
      }
    }
    const url = source.slice(cursor).match(/^(?:https?:\/\/|ftp:\/\/)[^\s<>]+/i)
    if (url) {
      ranges.push([cursor, cursor + url[0].length])
      cursor += url[0].length
      continue
    }
    cursor += 1
  }
  return ranges.sort(([a], [b]) => a - b)
}


function isEscaped(source: string, index: number) {
  let slashes = 0
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) slashes += 1
  return slashes % 2 === 1
}

function isProtectedRange(start: number, end: number, ranges: SourceRange[]) {
  return ranges.some(([rangeStart, rangeEnd]) => rangeStart < end && rangeEnd > start)
}

/** Convert complete slash delimiters, leaving code and URLs untouched. */
export function normalizeLatexDelimiters(source: string) {
  const ranges = literalRanges(source)
  let rangeIndex = 0
  let output = ''
  let cursor = 0
  let previousMathEnd = -1
  while (cursor < source.length) {
    while (rangeIndex < ranges.length && cursor >= ranges[rangeIndex][1]) rangeIndex += 1
    const protectedRange = ranges[rangeIndex]
    if (protectedRange && cursor >= protectedRange[0] && cursor < protectedRange[1]) {
      output += source.slice(cursor, protectedRange[1])
      cursor = protectedRange[1]
      continue
    }
    const opening = source.slice(cursor, cursor + 2)
    const closing = opening === '\\(' ? '\\)' : opening === '\\[' ? '\\]' : opening === '$$' ? '$$' : ''
    if (closing && !isEscaped(source, cursor)) {
      let close = source.indexOf(closing, cursor + 2)
      while (close >= 0 && (isEscaped(source, close) || isProtectedRange(cursor, close + 2, ranges))) {
        close = source.indexOf(closing, close + 2)
      }
      if (close >= 0) {
        let body = source.slice(cursor + 2, close)
        const lineStart = source.lastIndexOf('\n', cursor - 1) + 1
        if (!body.includes('\n') && source.slice(lineStart, cursor).trimStart().startsWith('|')) {
          // GFM parses cell separators before inline math. Equivalent TeX
          // control symbols keep absolute values/norms inside their own cell.
          body = body.replace(/\\\||(?<!\\)\|/g, (bar) => bar === '\\|' ? '\\Vert ' : '\\vert ')
        }
        // Consecutive closing/opening $$ runs are one ambiguous delimiter to
        // Markdown; a boundary keeps both complete operands independently renderable.
        if (cursor === previousMathEnd && output.endsWith('$$')) output += ' '
        output += opening === '\\[' ? `\n\n$$\n${body.trim()}\n$$\n\n` : `$$${body}$$`
        cursor = close + 2
        previousMathEnd = cursor
        continue
      }
    }
    output += source[cursor]
    cursor += 1
  }
  return output
}

/**
 * The LLM contract uses $$...$$ for math. This narrow fallback only upgrades a
 * complete, standalone equation line from the local deterministic answer.
 */
function normalizeStandaloneFormulaLines(source: string) {
  let fenced = false
  let mathBlock = false
  return source.split('\n').map((line) => {
    if (/^\s*(?:```|~~~)/.test(line)) {
      fenced = !fenced
      return line
    }
    if (!fenced && /^\s*\$\$\s*$/.test(line)) {
      mathBlock = !mathBlock
      return line
    }
    if (fenced || mathBlock || /[$`]/.test(line) || line.trim().startsWith('>')) return line
    const match = line.match(STANDALONE_FORMULA)
    if (!match) return line
    const indent = match[1].replace(/^([-*+]\s+)/, '')
    return `${indent}$$${match[2].trim()}$$`
  }).join('\n')
}

/** Keep incomplete streamed fences parseable without touching user content. */
export function stabilizeStreamingMarkdown(source: string) {
  const fences = source.match(/^\s*```/gm)?.length ?? 0
  return fences % 2 === 1 ? `${source}\n\n\u0060\u0060\u0060` : source
}

function useStableSource(text: string, streaming: boolean, normalizeFormulas: boolean) {
  const normalized = useMemo(() => {
    const withLatexDelimiters = normalizeLatexDelimiters(text)
    return normalizeFormulas ? normalizeStandaloneFormulaLines(withLatexDelimiters) : withLatexDelimiters
  }, [normalizeFormulas, text])
  const [buffered, setBuffered] = useState(normalized)

  useEffect(() => {
    if (!streaming) {
      setBuffered(normalized)
      return
    }
    const timer = window.setTimeout(() => setBuffered(normalized), 60)
    return () => window.clearTimeout(timer)
  }, [normalized, streaming])

  return streaming ? stabilizeStreamingMarkdown(buffered) : buffered
}

export function RichText({ text, className, streaming = false, normalizeFormulas = true }: { text: string; className?: string; streaming?: boolean; normalizeFormulas?: boolean }) {
  const source = useStableSource(text, streaming, normalizeFormulas)
  const classes = ['rich-text', className].filter(Boolean).join(' ')

  return (
    <Streamdown
      className={classes}
      mode={streaming ? 'streaming' : 'static'}
      animated={streaming}
      isAnimating={streaming}
      parseIncompleteMarkdown={streaming}
      plugins={{ code: streamdownCode, cjk: streamdownCjk, math }}
      controls={{ code: { copy: true, download: false }, table: false, image: false }}
      lineNumbers={false}
      linkSafety={{ enabled: true }}
    >
      {source}
    </Streamdown>
  )
}
