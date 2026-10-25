/**
 * Типы данных API. Повторяют схемы Pydantic на бэкенде:
 * при изменении backend/app/api/v1/schemas.py обновлять и здесь.
 */

export type Role = 'employee' | 'methodologist' | 'auditor' | 'admin';

export type AccessLevel = 'public' | 'internal' | 'confidential';

/** Исход обработки обращения. Определяет, как интерфейс рисует ответ. */
export type AnswerStatus =
  | 'answered'
  | 'no_context'
  | 'clarification'
  | 'template_ready'
  | 'template_filling'
  | 'info'
  | 'error';

export type CriticVerdict = 'approved' | 'needs_revision' | 'rejected' | 'skipped' | '';

export type Intent =
  | 'question'
  | 'template_request'
  | 'document_check'
  | 'approval_route'
  | 'smalltalk'
  | 'out_of_scope';

// ─────────────────────────── Пользователь ───────────────────────────
export interface User {
  id: string;
  email: string;
  full_name: string;
  department: string;
  position: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface CurrentUser extends User {
  permissions: string[];
  allowed_access_levels: string[];
  role_label: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

// ─────────────────────────── Диалог ───────────────────────────
/** Ссылка на положение инструкции — основа доверия к ответу. */
export interface Citation {
  chunk_id: string;
  document_code: string;
  document_title: string;
  version_label: string;
  /** Основной адрес фрагмента. */
  clause_number: string;
  /**
   * Все пункты, текст которых попал во фрагмент: короткие пункты
   * инструкции объединяются при нарезке, и адрес из одного номера
   * описывал бы фрагмент неточно.
   */
  clause_numbers: string[];
  section_title: string;
  breadcrumbs: string;
  page_number: number | null;
  annex_number: string;
  citation_label: string;
  quote: string;
  relevance: number;
  relevance_percent: number;
  rank: number;
}

export interface ClarifyingQuestion {
  question: string;
  field: string;
  options: string[];
}

export interface TemplateCandidate {
  code: string;
  name: string;
  description: string;
  doc_kind: string;
  related_clauses: string[];
  match_percent: number;
  fields: TemplateField[];
  required_approvals: ApprovalStep[];
}

export interface ChatResponse {
  message_id: string;
  session_id: string;
  answer: string;
  answer_status: AnswerStatus;
  citations: Citation[];
  clarifying_questions: ClarifyingQuestion[];
  template_candidates: TemplateCandidate[];
  intent: string;
  top_relevance_percent: number;
  critic_verdict: CriticVerdict;
  model_name: string;
  latency_ms: number;
  /** Ответ отдан из кеша: поиск и модель не вызывались. */
  from_cache: boolean;
  generated_document_id: string | null;
  filled_fields: Record<string, unknown>;
  missing_fields: string[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  answer_status: AnswerStatus | '';
  intent: string;
  critic_verdict: CriticVerdict;
  created_at: string;
  citations: Citation[];
}

/** Сообщение в локальном состоянии чата: добавлены поля для стриминга. */
export interface UiMessage extends Omit<ChatMessage, 'id'> {
  id: string;
  isStreaming?: boolean;
  clarifying_questions?: ClarifyingQuestion[];
  template_candidates?: TemplateCandidate[];
  top_relevance_percent?: number;
  latency_ms?: number;
  model_name?: string;
  /** Ответ отдан из кеша: задержку нельзя сравнивать с полным контуром. */
  from_cache?: boolean;
  /** Черновик, собранный по итогам заполнения модуля в диалоге. */
  generated_document_id?: string | null;
  error?: string;
}

export interface ChatSession {
  id: string;
  title: string;
  messages_count: number;
  is_archived: boolean;
  created_at: string;
  last_message_at: string | null;
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[];
}

// ─────────────────────────── Документы ───────────────────────────
export interface TemplateField {
  name: string;
  label: string;
  type: 'string' | 'text' | 'date' | 'list' | 'number';
  required: boolean;
  question: string;
  options?: string[];
}

export interface ApprovalStep {
  department: string;
  term_days: number;
  order: number;
}

export interface DocumentTemplate {
  code: string;
  name: string;
  description: string;
  doc_kind: string;
  fields_schema: TemplateField[];
  required_approvals: ApprovalStep[];
  related_clauses: string[];
  version: number;
}

export interface GeneratedDocument {
  id: string;
  filename: string;
  file_size: number;
  status: 'draft' | 'pending' | 'ready' | 'failed' | 'expired';
  missing_fields: string[];
  created_at: string;
  expires_at: string | null;
  template_name: string;
  download_url: string;
}

/**
 * Уровень отметки в отчёте проверки.
 *
 * `info` — не несоответствие, а реквизит следующего этапа: дата и
 * номер вносятся при регистрации, подпись появляется после
 * согласования. В оценку такие отметки не входят.
 */
export type Severity = 'critical' | 'major' | 'minor' | 'info';

export interface Violation {
  severity: Severity;
  field: string;
  problem: string;
  requirement: string;
  clause: string;
  recommendation: string;
}

export interface Recommendation {
  priority: number;
  severity: string;
  action: string;
  clause: string;
}

export type ValidationVerdict =
  | 'pending'
  | 'compliant'
  | 'minor_issues'
  | 'major_issues'
  | 'failed';

export interface ValidationReport {
  id: string;
  source_filename: string;
  detected_doc_kind: string;
  verdict: ValidationVerdict;
  score: number;
  violations: Violation[];
  recommendations: Recommendation[];
  checked_rules: number;
  created_at: string;
  summary: string;
  citations: Citation[];
  next_steps?: NextStep[];
}

/** Шаг дальнейшего порядка действий после проверки проекта. */
export interface NextStep {
  step: number;
  action: string;
  clause: string;
}

// ─────────────────────────── База знаний ───────────────────────────
export interface DocumentVersion {
  id: string;
  version_number: number;
  version_label: string;
  source_filename: string;
  status: 'pending' | 'indexing' | 'indexed' | 'failed' | 'archived';
  is_active: boolean;
  chunks_count: number;
  pages_count: number;
  changelog: string;
  created_at: string;
  indexed_at: string | null;
  effective_from: string | null;
  indexing_error: string;
}

export interface KnowledgeDocument {
  id: string;
  code: string;
  title: string;
  doc_type: string;
  description: string;
  access_level: AccessLevel;
  owner_department: string;
  is_active: boolean;
  versions: DocumentVersion[];
}

export interface KnowledgeBaseStats {
  collection: string;
  collection_status: string;
  points_count: number;
  documents_count: number;
  active_versions: number;
  failed_versions: number;
  templates_count: number;
}

export interface TaskAccepted {
  task_id: string;
  status: string;
  message: string;
  resource_id: string | null;
}

// ─────────────────────────── Качество ───────────────────────────
export interface EvalRun {
  id: string;
  name: string;
  model_name: string;
  eval_set: string;
  questions_total: number;
  faithfulness: number | null;
  answer_relevancy: number | null;
  context_precision: number | null;
  context_recall: number | null;
  citation_accuracy: number | null;
  no_answer_rate: number | null;
  duration_seconds: number;
  status: string;
  created_at: string;
}

export interface ComponentHealth {
  name: string;
  healthy: boolean;
  details: Record<string, unknown>;
}

export interface HealthResponse {
  status: 'ok' | 'degraded' | 'unavailable';
  version: string;
  environment: string;
  components: ComponentHealth[];
}

export interface AuditLogEntry {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  outcome: string;
  ip_address: string;
  created_at: string;
  user_email: string;
}

// ─────────────────────────── Ошибки API ───────────────────────────
export interface ApiErrorBody {
  detail: string;
  code: string;
  request_id?: string | null;
  fields?: string[];
}
