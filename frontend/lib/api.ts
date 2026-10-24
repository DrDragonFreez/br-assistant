/**
 * Клиент API.
 *
 * Единственное место, где фронтенд обращается к бэкенду: компоненты
 * вызывают функции отсюда, прямых fetch в них нет. Так проверка токена,
 * обработка ошибок и формат адресов описаны один раз.
 *
 * Access-токен живёт в памяти и истекает через час. Клиент обновляет его
 * сам: при истечении — до запроса, при ответе 401 — один раз и с повтором
 * исходного запроса. Параллельные запросы при этом ждут одного обновления
 * (см. refreshPromise), иначе первый же экран с тремя запросами потратил бы
 * три refresh-токена и разлогинил пользователя.
 */

import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  isAccessTokenExpired,
  setTokens,
} from './auth';
import type {
  ApiErrorBody,
  AuditLogEntry,
  ChatResponse,
  ChatSession,
  ChatSessionDetail,
  CurrentUser,
  DocumentTemplate,
  EvalRun,
  GeneratedDocument,
  HealthResponse,
  KnowledgeBaseStats,
  KnowledgeDocument,
  Role,
  TaskAccepted,
  TokenResponse,
  User,
  ValidationReport,
} from './types';

/** Базовый адрес API вместе с префиксом версии. */
export const API_BASE = `${process.env.NEXT_PUBLIC_API_URL || '/api'}/v1`;

/** Ошибка API с кодом и человекочитаемым описанием. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly fields: string[];

  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.detail || 'Сервис недоступен. Повторите попытку.');
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code || 'unknown_error';
    this.requestId = body.request_id || '';
    this.fields = body.fields || [];
  }

  /** True, если проблема в авторизации, а не в данных запроса. */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  /** Запрос без токена: вход и обновление токенов. */
  anonymous?: boolean;
  signal?: AbortSignal;
  /** Не повторять запрос после обновления токена (защита от цикла). */
  noRetry?: boolean;
}

/** Текущее обновление токенов; остальные запросы ждут его, а не плодят новые. */
let refreshPromise: Promise<boolean> | null = null;

/**
 * Обновляет пару токенов по refresh-токену.
 * @returns true, если токены обновлены.
 */
async function refreshTokens(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${API_BASE}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) {
          clearTokens();
          return false;
        }
        setTokens((await response.json()) as TokenResponse);
        return true;
      } catch {
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }

  return refreshPromise;
}

/**
 * Обновляет access-токен, если он истёк.
 *
 * Нужна потоковому запросу: начатую генерацию повторить с новым токеном
 * уже нельзя, поэтому срок проверяется до открытия потока.
 *
 * @returns true, если действующий токен есть.
 */
export async function ensureFreshToken(): Promise<boolean> {
  if (!isAccessTokenExpired()) return true;
  if (!getRefreshToken()) return false;
  return refreshTokens();
}

/** Разбирает тело ошибки, не падая на пустом или не-JSON ответе. */
async function parseErrorBody(response: Response): Promise<Partial<ApiErrorBody>> {
  try {
    return (await response.json()) as Partial<ApiErrorBody>;
  } catch {
    return { detail: `Ошибка ${response.status}`, code: 'bad_response' };
  }
}

/**
 * Выполняет запрос к API.
 *
 * @param path Путь после префикса версии, например `/chat/sessions`.
 * @param options Параметры запроса.
 * @returns Разобранный ответ; для 204 — undefined.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, anonymous = false, signal, noRetry = false } = options;

  if (!anonymous && isAccessTokenExpired() && getRefreshToken()) {
    await refreshTokens();
  }

  const headers: Record<string, string> = {};
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;
  if (body !== undefined && !isFormData) {
    headers['Content-Type'] = 'application/json';
  }
  if (!anonymous) {
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: isFormData ? (body as FormData) : body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });

  // Токен мог быть отозван или истечь раньше срока — пробуем один раз.
  if (response.status === 401 && !anonymous && !noRetry) {
    if (await refreshTokens()) {
      return request<T>(path, { ...options, noRetry: true });
    }
    clearTokens();
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response));
  }

  if (response.status === 204 || response.headers.get('content-length') === '0') {
    return undefined as T;
  }

  return (await response.json()) as T;
}

// ─────────────────────────── Аутентификация ───────────────────────────
export const auth = {
  /** Вход по корпоративному адресу и паролю. */
  async login(email: string, password: string): Promise<TokenResponse> {
    const tokens = await request<TokenResponse>('/auth/login', {
      method: 'POST',
      body: { email, password },
      anonymous: true,
    });
    setTokens(tokens);
    return tokens;
  },

  /** Сведения о текущем пользователе вместе с его правами. */
  me(): Promise<CurrentUser> {
    return request<CurrentUser>('/auth/me');
  },

  /** Выход: токен отзывается на сервере, локальные следы стираются. */
  async logout(): Promise<void> {
    try {
      await request<void>('/auth/logout', { method: 'POST' });
    } finally {
      clearTokens();
    }
  },

  /** Смена собственного пароля. */
  changePassword(oldPassword: string, newPassword: string): Promise<void> {
    return request<void>('/auth/password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
    });
  },
};

// ─────────────────────────── Диалог ───────────────────────────
export const chat = {
  /**
   * Обращение с полным контуром обработки: маршрутизация, поиск,
   * реранжирование, генерация и проверка критиком. Дольше потокового
   * режима, зато в ответе есть вердикт критика и подобранные шаблоны.
   */
  ask(payload: {
    message: string;
    session_id?: string | null;
    template_code?: string;
  }): Promise<ChatResponse> {
    return request<ChatResponse>('/chat', {
      method: 'POST',
      body: {
        message: payload.message,
        session_id: payload.session_id || null,
        template_code: payload.template_code || '',
      },
    });
  },

  sessions(includeArchived = false): Promise<ChatSession[]> {
    return request<ChatSession[]>(
      `/chat/sessions?include_archived=${includeArchived ? 'true' : 'false'}`,
    );
  },

  session(sessionId: string): Promise<ChatSessionDetail> {
    return request<ChatSessionDetail>(`/chat/sessions/${sessionId}`);
  },

  createSession(title = 'Новый диалог'): Promise<ChatSession> {
    return request<ChatSession>('/chat/sessions', { method: 'POST', body: { title } });
  },

  deleteSession(sessionId: string): Promise<void> {
    return request<void>(`/chat/sessions/${sessionId}`, { method: 'DELETE' });
  },

  /** Оценка ответа: по ней считается доля полезных ответов. */
  feedback(
    messageId: string,
    payload: { is_helpful: boolean; reason?: string; comment?: string },
  ): Promise<void> {
    return request<void>(`/chat/messages/${messageId}/feedback`, {
      method: 'POST',
      body: {
        is_helpful: payload.is_helpful,
        reason: payload.reason || '',
        comment: payload.comment || '',
      },
    });
  },
};

// ─────────────────────────── Документы ───────────────────────────
export const documents = {
  templates(): Promise<DocumentTemplate[]> {
    return request<DocumentTemplate[]>('/documents/templates');
  },

  template(code: string): Promise<DocumentTemplate> {
    return request<DocumentTemplate>(`/documents/templates/${encodeURIComponent(code)}`);
  },

  /** Ставит в очередь сборку DOCX; файл появляется через несколько секунд. */
  generate(payload: {
    template_code: string;
    values: Record<string, unknown>;
    session_id?: string | null;
  }): Promise<TaskAccepted> {
    return request<TaskAccepted>('/documents/generate', {
      method: 'POST',
      body: {
        template_code: payload.template_code,
        values: payload.values,
        session_id: payload.session_id || null,
      },
    });
  },

  generated(): Promise<GeneratedDocument[]> {
    return request<GeneratedDocument[]>('/documents/generated');
  },

  generatedOne(documentId: string): Promise<GeneratedDocument> {
    return request<GeneratedDocument>(`/documents/generated/${documentId}`);
  },

  /** Адрес для скачивания; браузер скачивает файл сам, минуя клиент. */
  downloadUrl(documentId: string): string {
    return `${API_BASE}/documents/generated/${documentId}/download`;
  },

  /**
   * Скачивает готовый черновик.
   *
   * Простая ссылка не подошла бы: токен хранится в памяти и в адрес его
   * помещать нельзя, поэтому файл забирается запросом с заголовком
   * авторизации и отдаётся браузеру как blob.
   */
  async download(documentId: string, filename: string): Promise<void> {
    const token = getAccessToken();
    const response = await fetch(documents.downloadUrl(documentId), {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      throw new ApiError(response.status, await parseErrorBody(response));
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const link = window.document.createElement('a');
    link.href = url;
    link.download = filename || 'документ.docx';
    window.document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  },

  /** Отправляет проект документа на проверку соответствия инструкции. */
  validate(file: File, docKind = ''): Promise<TaskAccepted> {
    const form = new FormData();
    form.append('file', file);
    form.append('doc_kind', docKind);
    return request<TaskAccepted>('/documents/validate', { method: 'POST', body: form });
  },

  validationReport(reportId: string): Promise<ValidationReport> {
    return request<ValidationReport>(`/documents/validate/${reportId}`);
  },

  validationReports(): Promise<ValidationReport[]> {
    return request<ValidationReport[]>('/documents/validate');
  },
};

// ─────────────────────────── Администрирование ───────────────────────────
export const admin = {
  knowledgeStats(): Promise<KnowledgeBaseStats> {
    return request<KnowledgeBaseStats>('/admin/knowledge/stats');
  },

  knowledgeDocuments(): Promise<KnowledgeDocument[]> {
    return request<KnowledgeDocument[]>('/admin/knowledge/documents');
  },

  /** Загружает новую редакцию документа базы знаний. */
  uploadVersion(payload: {
    file: File;
    documentCode: string;
    versionLabel?: string;
    changelog?: string;
    activate?: boolean;
  }): Promise<TaskAccepted> {
    const form = new FormData();
    form.append('file', payload.file);
    form.append('document_code', payload.documentCode);
    form.append('version_label', payload.versionLabel || '');
    form.append('changelog', payload.changelog || '');
    form.append('activate', String(payload.activate ?? true));
    return request<TaskAccepted>('/admin/knowledge/upload', { method: 'POST', body: form });
  },

  reindex(rebuildVocabulary = true): Promise<TaskAccepted> {
    return request<TaskAccepted>(
      `/admin/knowledge/reindex?rebuild_vocabulary=${rebuildVocabulary ? 'true' : 'false'}`,
      { method: 'POST' },
    );
  },

  activateVersion(documentCode: string, versionNumber: number): Promise<void> {
    return request<void>(
      `/admin/knowledge/documents/${encodeURIComponent(documentCode)}/versions/${versionNumber}/activate`,
      { method: 'POST' },
    );
  },

  evalRuns(): Promise<EvalRun[]> {
    return request<EvalRun[]>('/admin/eval/runs');
  },

  runEval(payload: { name?: string; limit?: number | null; skip_ragas?: boolean }): Promise<TaskAccepted> {
    return request<TaskAccepted>('/admin/eval/run', {
      method: 'POST',
      body: {
        name: payload.name || '',
        limit: payload.limit ?? null,
        skip_ragas: payload.skip_ragas ?? false,
      },
    });
  },

  users(): Promise<User[]> {
    return request<User[]>('/admin/users');
  },

  createUser(payload: {
    email: string;
    password: string;
    full_name: string;
    department?: string;
    position?: string;
    role?: Role;
  }): Promise<User> {
    return request<User>('/admin/users', { method: 'POST', body: payload });
  },

  updateUser(userId: string, payload: Partial<Pick<User, 'full_name' | 'department' | 'position' | 'role' | 'is_active'>>): Promise<User> {
    return request<User>(`/admin/users/${userId}`, { method: 'PATCH', body: payload });
  },

  audit(limit = 50): Promise<AuditLogEntry[]> {
    return request<AuditLogEntry[]>(`/admin/audit?limit=${limit}`);
  },

  /**
   * Состояние компонентов контура для экрана администратора.
   *
   * Используется авторизованный эндпоинт, а не корневой `/health/ready`:
   * тот подключён вне префикса версии (его опрашивают docker healthcheck
   * и мониторинг), наружу Nginx отдаёт только `/api/`, и состав моделей
   * с показателями базы знаний незачем раскрывать без токена.
   */
  health(): Promise<HealthResponse> {
    return request<HealthResponse>('/admin/system/health');
  },
};

