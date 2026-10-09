export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api').replace(/\/$/, '')

export interface Course {
  id: number
  name: string
  description: string
  topic_count: number
  material_count: number
}

export interface Topic {
  id: number
  course_id: number
  name: string
  description: string
}

export interface Material {
  id: number
  course_id: number
  topic_id: number | null
  material_type: string
  title: string
  content: string
  page_number: number | null
  section: string
  source_anchor: string
  tags: unknown[]
  verified: boolean
}

export interface EvidenceItem {
  material_id: number
  chunk_id: number | null
  course: string
  topic: string | null
  title: string
  material_type: string
  page_number: number | null
  section: string
  source_anchor: string
  snippet: string
  score: number
  verified: boolean
}

export interface SearchRequest {
  query: string
  course_id?: number
  topic_id?: number
  material_types?: string[]
  limit?: number
}

export interface SearchResponse {
  query: string
  results: EvidenceItem[]
  has_evidence: boolean
}

export type ValidationStatus = 'pass' | 'fail' | 'unknown' | 'invalid' | 'conflict' | 'not_applicable'

export interface ValidationCheck {
  category: string
  status: ValidationStatus
  message: string
  claim: string
  evidence_ids: number[]
  details: Record<string, unknown>
}

export interface Confidence {
  level: 'high' | 'medium' | 'low'
  score: number
  reasons: string[]
}

export interface ValidateRequest {
  question: string
  answer: string
  course_id?: number
  topic_id?: number
  material_ids?: number[]
}

export interface Traceability {
  [key: string]: unknown
}

export interface ValidateResponse {
  answer_id: number
  question: string
  answer: string
  checks: ValidationCheck[]
  evidence: EvidenceItem[]
  confidence: Confidence
  risk_flags: string[]
  traceability: Traceability
  agent_trace: Array<Record<string, unknown>>
}

export interface CategoryMetric {
  total?: number
  correct?: number
  tp?: number
  fp?: number
  fn?: number
  tn?: number
  [key: string]: unknown
}

export interface Metrics {
  course_count: number
  topic_count: number
  material_count: number
  verified_material_count: number
  evaluation_case_count: number
  evaluation_run: boolean
  traceability_rate: number
  conflict_accuracy: number | null
  false_positive_rate: number | null
  category_stats: Record<string, CategoryMetric>
}

export interface HealthResponse {
  status?: string
  [key: string]: unknown
}

export interface ExtractedItem {
  id: number
  page_number: number
  item_type: string
  raw_text: string
  normalized_text: string
  payload: Record<string, unknown>
  confidence: number
  needs_review: boolean
  review_status: string
  source_anchor: string
}

export interface IngestionJob {
  id: number
  source_document_id: number
  status: string
  processed_pages: number
  total_pages: number
  summary: Record<string, unknown>
  items: ExtractedItem[]
}

export interface PublishResult {
  published: number
  skipped: number
}

export class ApiError extends Error {
  status?: number
  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    const isFormData = typeof FormData !== 'undefined' && init?.body instanceof FormData
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body && !isFormData ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    })
  } catch {
    throw new ApiError('无法连接后端服务，请确认 API 已在 localhost:8000 启动。')
  }

  const text = await response.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : undefined
  } catch {
    body = undefined
  }
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`
    if (typeof body === 'object' && body !== null && 'detail' in body) detail = String(body.detail)
    throw new ApiError(detail, response.status)
  }
  return body as T
}

function queryString(params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  courses: () => request<Course[]>('/courses'),
  topics: (courseId?: number) => request<Topic[]>(`/topics${queryString({ course_id: courseId })}`),
  materials: (courseId?: number, topicId?: number, materialType?: string) => request<Material[]>(`/materials${queryString({ course_id: courseId, topic_id: topicId, material_type: materialType, limit: 200 })}`),
  search: (payload: SearchRequest) => request<SearchResponse>('/search', { method: 'POST', body: JSON.stringify(payload) }),
  validateAnswer: (payload: ValidateRequest) => request<ValidateResponse>('/answers/validate', { method: 'POST', body: JSON.stringify(payload) }),
  answer: (id: number) => request<ValidateResponse>(`/answers/${encodeURIComponent(String(id))}`),
  metrics: () => request<Metrics>('/metrics'),
  ingestion: {
    upload: (file: File, courseId: number) => {
      const body = new FormData()
      body.append('file', file)
      return request<IngestionJob>(`/ingestion/upload${queryString({ course_id: courseId })}`, { method: 'POST', body })
    },
    job: (id: number) => request<IngestionJob>(`/ingestion/jobs/${id}`),
    review: (id: number, payload: { normalized_text: string; review_status: 'approved' | 'rejected' }) => request<ExtractedItem>(`/ingestion/items/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    ocr: (id: number) => request<ExtractedItem>(`/ingestion/items/${id}/ocr`, { method: 'POST' }),
    assist: (id: number, mode: 'auto' | 'text' | 'vision' = 'auto') => request<ExtractedItem>(`/ingestion/items/${id}/assist${mode === 'auto' ? '' : `?mode=${mode}`}`, { method: 'POST' }),
    publish: (id: number, courseId: number) => request<PublishResult>(`/ingestion/jobs/${id}/publish${queryString({ course_id: courseId })}`, { method: 'POST' }),
  },
}
