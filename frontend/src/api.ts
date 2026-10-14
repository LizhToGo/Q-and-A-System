export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api').replace(/\/$/, '')

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

export interface EvidenceMedia {
  id: string
  kind: 'figure' | 'table' | 'page'
  label: string
  page_number: number
  url: string
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
  source_document_id?: number | null
  content_format?: 'markdown' | 'text'
  full_content?: string | null
  page_image_url?: string | null
  media?: EvidenceMedia[]
}

export interface SourceDocument {
  id: number
  course_id: number | null
  filename: string
  parser: string
  status: string
  page_count: number
  job_id: number | null
  rag_indexed_pages: number
  rag_verified_pages: number
  rag_candidate_pages: number
  rag_coverage: number
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

export interface AskRequest {
  conversation_id?: number
  question: string
  course_id?: number | null
  topic_id?: number | null
  source_document_id?: number | null
  use_web?: boolean
  provider_id?: string
  model?: string
}
export interface Traceability {
  [key: string]: unknown
}

export interface LocalToolResult {
  id: string
  tool: string
  status: 'completed' | 'error'
  title: string
  summary: string
  parameters: Record<string, unknown>
  artifacts: Array<{ id: string; url: string; caption: string }>
  data: Record<string, unknown>
  notes: string[]
}

export interface StudySuggestionsRequest extends Partial<ConversationScope> {
  provider_id?: string
  model?: string
  exclude?: string[]
}

export interface StudySuggestionsResponse {
  suggestions: string[]
  provider: string
  model: string
  evidence_ids: number[]
}

export interface ValidateResponse {
  answer_id: number
  conversation_id: number | null
  conversation_title: string | null
  conversation_title_source: ConversationTitleSource | null
  question: string
  answer: string
  checks: ValidationCheck[]
  evidence: EvidenceItem[]
  confidence: Confidence
  risk_flags: string[]
  traceability: Traceability
  agent_trace: Array<Record<string, unknown>>
  tool_results?: LocalToolResult[]
}

export interface ConversationScope {
  course_id: number | null
  topic_id: number | null
  source_document_id: number | null
}

export type ConversationTitleSource = 'pending' | 'model' | 'user'
export interface ConversationTitlesRequest {
  conversation_ids: number[]
  provider_id?: string
  model?: string
}

export interface Conversation extends ConversationScope {
  id: number
  title: string
  title_source: ConversationTitleSource
  created_at: string
  updated_at: string
}

export interface ConversationDetail extends Conversation {
  turns: ValidateResponse[]
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

export interface ProviderConfig {
  id: string
  enabled: boolean
  provider: string
  base_url: string
  model: string
  models: string[]
  protocol: 'responses'
  gateway_messages: boolean
  api_key_configured: boolean
  config_source: 'environment' | 'saved'
}

export interface ProviderAdminResponse {
  providers: ProviderConfig[]
  default_provider_id: string
  env_path?: string
}

export interface PublicProvider {
  id: string
  provider: string
  model: string
  models: string[]
  enabled: boolean
  api_key_configured: boolean
}

export interface ProviderDraft {
  enabled: boolean
  provider: string
  base_url: string
  model: string
  models: string[]
  gateway_messages: boolean
  api_key?: string
}
export interface ProviderModelsDraft extends ProviderDraft {
  provider_id?: string
}

export interface ProviderModelResponse {
  models: Array<{ id: string }>
  current_model: string
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
  updated?: number
  skipped: number
  revoked?: number
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

export interface RagIndexStatus {
  job_id: number
  source_document_id: number
  filename: string
  status: string
  page_count: number
  indexed_pages: number
  verified_pages: number
  candidate_pages: number
  missing_pages: number
  material_count: number
  chunk_count: number
  coverage: number
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  courses: () => request<Course[]>('/courses'),
  conversations: () => request<Conversation[]>('/conversations'),
  conversation: (id: number) => request<ConversationDetail>(`/conversations/${encodeURIComponent(String(id))}`),
  deleteConversation: (id: number) => request<void>(`/conversations/${encodeURIComponent(String(id))}`, { method: 'DELETE' }),
  summarizeConversationTitles: (payload: ConversationTitlesRequest) => request<Conversation[]>('/conversations/titles', { method: 'POST', body: JSON.stringify(payload) }),
  sourceDocuments: (courseId?: number) => request<SourceDocument[]>(`/source-documents${queryString({ course_id: courseId })}`),
  topics: (courseId?: number) => request<Topic[]>(`/topics${queryString({ course_id: courseId })}`),
  materialEvidence: (id: number) => request<EvidenceItem>(`/materials/${encodeURIComponent(String(id))}/evidence`),
  ask: (payload: AskRequest) => request<ValidateResponse>('/answers/ask', { method: 'POST', body: JSON.stringify(payload) }),
  studySuggestions: (payload: StudySuggestionsRequest) => request<StudySuggestionsResponse>('/study-suggestions', { method: 'POST', body: JSON.stringify(payload) }),
  metrics: () => request<Metrics>('/metrics'),
  ingestion: {
    upload: (file: File, courseId: number) => {
      const body = new FormData()
      body.append('file', file)
      return request<IngestionJob>(`/ingestion/upload${queryString({ course_id: courseId })}`, { method: 'POST', body })
    },
    job: (id: number) => request<IngestionJob>(`/ingestion/jobs/${id}`),
    rag: (id: number) => request<RagIndexStatus>(`/ingestion/jobs/${id}/rag`),
    buildRag: (id: number, courseId?: number) => request<Record<string, number | string>>(`/ingestion/jobs/${id}/build-rag${queryString({ course_id: courseId })}`, { method: 'POST' }),
    qualityAudit: (id: number) => request<{ audited_pages: number; flagged: number; revoked: number; status: string }>(`/ingestion/jobs/${id}/quality-audit`, { method: 'POST' }),
    review: (id: number, payload: { normalized_text: string; review_status: 'approved' | 'rejected'; quality_review_cleared?: boolean }) => request<ExtractedItem>(`/ingestion/items/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    ocr: (id: number) => request<ExtractedItem>(`/ingestion/items/${id}/ocr`, { method: 'POST' }),
    assist: (id: number, mode: 'auto' | 'text' | 'vision' = 'auto') => request<ExtractedItem>(`/ingestion/items/${id}/assist${mode === 'auto' ? '' : `?mode=${mode}`}`, { method: 'POST' }),
    publish: (id: number, courseId: number) => request<PublishResult>(`/ingestion/jobs/${id}/publish${queryString({ course_id: courseId })}`, { method: 'POST' }),
  },
  providers: {
    get: (adminToken: string) => request<ProviderAdminResponse>('/admin/providers', { headers: { 'X-Admin-Token': adminToken } }),
    create: (adminToken: string, payload: ProviderDraft & { copy_from?: string }) => request<ProviderConfig>('/admin/providers', {
      method: 'POST',
      headers: { 'X-Admin-Token': adminToken },
      body: JSON.stringify(payload),
    }),
    update: (adminToken: string, id: string, payload: ProviderDraft) => request<ProviderConfig>(`/admin/providers/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'X-Admin-Token': adminToken },
      body: JSON.stringify(payload),
    }),
    setDefault: (adminToken: string, providerId: string) => request<{ default_provider_id: string }>('/admin/providers/default', {
      method: 'PUT',
      headers: { 'X-Admin-Token': adminToken },
      body: JSON.stringify({ provider_id: providerId }),
    }),
    models: (adminToken: string, payload: ProviderModelsDraft) => request<ProviderModelResponse>('/admin/providers/models', {
      method: 'POST',
      headers: { 'X-Admin-Token': adminToken },
      body: JSON.stringify(payload),
    }),
  },
  publicProviders: () => request<{ providers: PublicProvider[]; default_provider_id: string }>('/providers'),
  providerModels: (providerId: string) => request<ProviderModelResponse>(`/providers/${encodeURIComponent(providerId)}/models`),
}
