/**
 * Чтение потока ответа помощника.
 *
 * Встроенный EventSource не подходит по двум причинам: он умеет только GET
 * и не позволяет задать заголовок Authorization. Поэтому поток читается
 * обычным fetch с разбором формата text/event-stream вручную.
 *
 * Разбор учитывает особенности формата: событие заканчивается пустой
 * строкой, поле data может повторяться (строки склеиваются через \n),
 * а строки, начинающиеся с двоеточия, — это комментарии. Их присылает
 * sse-starlette каждые 15 секунд, чтобы Nginx не закрыл соединение
 * по таймауту; обрабатывать их не нужно.
 */

import { ApiError, API_BASE, ensureFreshToken } from './api';
import { getAccessToken } from './auth';
import type { Citation } from './types';

/** Событие потока в разобранном виде. */
export interface StreamEvent {
  event: string;
  data: string;
}

/** Данные события `done`: итог обработки обращения. */
export interface StreamDone {
  status: string;
  citations: Citation[];
  cited_clauses: string[];
  relevance_percent: number;
}

/** Обработчики событий потока. */
export interface StreamHandlers {
  /** Идентификатор диалога; приходит первым, до текста. */
  onMeta?: (sessionId: string) => void;
  /** Источники; приходят до первого токена — их показывают сразу. */
  onCitations?: (citations: Citation[]) => void;
  /** Очередной фрагмент текста. */
  onToken?: (text: string) => void;
  /** Полная замена текста: модель отказалась уже в процессе генерации. */
  onReplace?: (text: string) => void;
  /** Поток завершён штатно. */
  onDone?: (result: StreamDone) => void;
  /** Ошибка на стороне сервиса. */
  onError?: (detail: string, code: string) => void;
}

/**
 * Разбирает блок события SSE.
 *
 * @param raw Текст блока без завершающей пустой строки.
 * @returns Событие либо null, если блок состоит из комментариев.
 */
function parseEventBlock(raw: string): StreamEvent | null {
  let event = 'message';
  const dataLines: string[] = [];

  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue;

    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    // По спецификации после двоеточия отбрасывается один пробел.
    let value = separator === -1 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }

  if (!dataLines.length) return null;
  return { event, data: dataLines.join('\n') };
}

/** Безопасный разбор JSON из поля data. */
function parseData<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

/**
 * Отправляет обращение и читает потоковый ответ.
 *
 * @param payload Текст обращения и контекст диалога.
 * @param handlers Обработчики событий.
 * @param signal Позволяет прервать поток при уходе со страницы.
 *
 * @throws ApiError Если сервис отклонил запрос до начала потока.
 */
export async function streamChat(
  payload: { message: string; session_id?: string | null; template_code?: string },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  // Токен в памяти мог истечь: обновляем до начала потока, повторить
  // уже начатую генерацию было бы нельзя.
  await ensureFreshToken();

  const token = getAccessToken();
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      message: payload.message,
      session_id: payload.session_id || null,
      template_code: payload.template_code || '',
    }),
    signal,
  });

  if (!response.ok) {
    let body: Record<string, unknown> = {};
    try {
      body = (await response.json()) as Record<string, unknown>;
    } catch {
      body = { detail: `Ошибка ${response.status}`, code: 'stream_failed' };
    }
    throw new ApiError(response.status, body);
  }

  if (!response.body) {
    throw new ApiError(500, { detail: 'Поток недоступен в этом браузере', code: 'no_stream' });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const handleEvent = (parsed: StreamEvent): void => {
    switch (parsed.event) {
      case 'meta': {
        const meta = parseData<{ session_id: string }>(parsed.data);
        if (meta?.session_id) handlers.onMeta?.(meta.session_id);
        break;
      }
      case 'citations': {
        const citations = parseData<Citation[]>(parsed.data);
        if (citations) handlers.onCitations?.(citations);
        break;
      }
      case 'token': {
        const chunk = parseData<{ text: string }>(parsed.data);
        if (chunk?.text) handlers.onToken?.(chunk.text);
        break;
      }
      case 'replace': {
        const chunk = parseData<{ text: string }>(parsed.data);
        if (chunk?.text) handlers.onReplace?.(chunk.text);
        break;
      }
      case 'done': {
        const done = parseData<StreamDone>(parsed.data);
        handlers.onDone?.(
          done ?? { status: 'answered', citations: [], cited_clauses: [], relevance_percent: 0 },
        );
        break;
      }
      case 'error': {
        const error = parseData<{ detail: string; code: string }>(parsed.data);
        handlers.onError?.(
          error?.detail || 'Не удалось сформировать ответ',
          error?.code || 'stream_error',
        );
        break;
      }
      default:
        // Служебные события (ping) игнорируются.
        break;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // События разделяются пустой строкой; \r\n встречается за прокси.
    let boundary = buffer.search(/\r?\n\r?\n/);
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '');

      const parsed = parseEventBlock(block);
      if (parsed) handleEvent(parsed);

      boundary = buffer.search(/\r?\n\r?\n/);
    }
  }

  // Последнее событие могло прийти без завершающей пустой строки.
  const tail = parseEventBlock(buffer);
  if (tail) handleEvent(tail);
}
