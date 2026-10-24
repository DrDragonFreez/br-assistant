'use client';

/**
 * Состояние диалога с помощником.
 *
 * Поддерживает два режима ответа, и это осознанно:
 *
 * * **Потоковый** (`stream`) — первые слова появляются за секунду, источники
 *   приходят до текста. Так работает повседневный сценарий.
 * * **Полный контур** (`full`) — один запрос, но ответ проходит критика,
 *   возвращает вердикт проверки и подобранные шаблоны. Этот режим
 *   показывают на защите: в нём видно, что ответ проверен.
 *
 * Переключатель не прихоть: проверка ответа требует второго обращения
 * к модели после генерации, и в потоковом режиме она лишена смысла.
 */

import { useCallback, useRef, useState } from 'react';

import { ApiError, chat } from '@/lib/api';
import { streamChat } from '@/lib/sse';
import type { Citation, UiMessage } from '@/lib/types';

export type ChatMode = 'stream' | 'full';

/** Создаёт идентификатор для сообщения в локальном состоянии. */
function localId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Пустое сообщение помощника, которое наполняется по ходу потока. */
function pendingAssistantMessage(): UiMessage {
  return {
    id: localId(),
    role: 'assistant',
    content: '',
    answer_status: '',
    intent: '',
    critic_verdict: '',
    created_at: new Date().toISOString(),
    citations: [],
    isStreaming: true,
  };
}

interface UseChatOptions {
  /** Идентификатор открытого диалога, если история уже загружена. */
  initialSessionId?: string | null;
  initialMessages?: UiMessage[];
  /** Вызывается, когда бэкенд создал новый диалог. */
  onSessionCreated?: (sessionId: string) => void;
}

export function useChat(options: UseChatOptions = {}) {
  const { initialSessionId = null, initialMessages = [], onSessionCreated } = options;

  const [messages, setMessages] = useState<UiMessage[]>(initialMessages);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [mode, setMode] = useState<ChatMode>('stream');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  /** Текущий запрос: нужен, чтобы прервать поток при уходе со страницы. */
  const abortRef = useRef<AbortController | null>(null);
  /** Код шаблона, если идёт пошаговое заполнение документа. */
  const templateCodeRef = useRef('');

  /** Обновляет последнее сообщение помощника. */
  const patchLast = useCallback((patch: Partial<UiMessage>) => {
    setMessages((current) => {
      if (!current.length) return current;
      const next = [...current];
      const last = next[next.length - 1];
      if (last.role !== 'assistant') return current;
      next[next.length - 1] = { ...last, ...patch };
      return next;
    });
  }, []);

  /** Дописывает текст в последнее сообщение помощника. */
  const appendToLast = useCallback((text: string) => {
    setMessages((current) => {
      if (!current.length) return current;
      const next = [...current];
      const last = next[next.length - 1];
      if (last.role !== 'assistant') return current;
      next[next.length - 1] = { ...last, content: last.content + text };
      return next;
    });
  }, []);

  const applySession = useCallback(
    (id: string) => {
      setSessionId((current) => {
        if (current === id) return current;
        onSessionCreated?.(id);
        return id;
      });
    },
    [onSessionCreated],
  );

  /** Отправляет обращение в выбранном режиме. */
  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || busy) return;

      setError('');
      setBusy(true);

      const userMessage: UiMessage = {
        id: localId(),
        role: 'user',
        content: message,
        answer_status: '',
        intent: '',
        critic_verdict: '',
        created_at: new Date().toISOString(),
        citations: [],
      };
      setMessages((current) => [...current, userMessage, pendingAssistantMessage()]);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        if (mode === 'full') {
          const response = await chat.ask({
            message,
            session_id: sessionId,
            template_code: templateCodeRef.current,
          });
          applySession(response.session_id);
          templateCodeRef.current =
            response.answer_status === 'template_filling' ? templateCodeRef.current : '';

          patchLast({
            id: response.message_id,
            content: response.answer,
            answer_status: response.answer_status,
            intent: response.intent,
            critic_verdict: response.critic_verdict,
            citations: response.citations,
            clarifying_questions: response.clarifying_questions,
            template_candidates: response.template_candidates,
            top_relevance_percent: response.top_relevance_percent,
            latency_ms: response.latency_ms,
            model_name: response.model_name,
            from_cache: response.from_cache,
            isStreaming: false,
          });
        } else {
          const started = Date.now();
          let streamCitations: Citation[] = [];

          await streamChat(
            { message, session_id: sessionId, template_code: templateCodeRef.current },
            {
              onMeta: applySession,
              onCitations: (citations) => {
                streamCitations = citations;
                // Источники показываются сразу: сотрудник видит, на чём
                // строится ответ, пока текст ещё печатается.
                patchLast({ citations });
              },
              onToken: appendToLast,
              onReplace: (replacement) => {
                patchLast({ content: replacement, citations: [] });
              },
              onDone: (result) => {
                patchLast({
                  answer_status: (result.status as UiMessage['answer_status']) || 'answered',
                  citations: result.citations?.length ? result.citations : streamCitations,
                  top_relevance_percent: result.relevance_percent,
                  latency_ms: Date.now() - started,
                  isStreaming: false,
                });
              },
              onError: (detail) => {
                patchLast({ answer_status: 'error', content: detail, isStreaming: false });
                setError(detail);
              },
            },
            controller.signal,
          );
        }
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          patchLast({ isStreaming: false });
          return;
        }
        const detail =
          caught instanceof ApiError
            ? caught.message
            : 'Сервис недоступен. Повторите запрос через несколько секунд.';
        setError(detail);
        patchLast({ answer_status: 'error', content: detail, isStreaming: false });
      } finally {
        abortRef.current = null;
        setBusy(false);
      }
    },
    [appendToLast, applySession, busy, mode, patchLast, sessionId],
  );

  /** Прерывает текущий поток. */
  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /** Начинает заполнение документа по выбранному шаблону. */
  const startTemplate = useCallback(
    async (templateCode: string, prompt: string) => {
      templateCodeRef.current = templateCode;
      // Заполнение ведёт граф, а он подключён только в полном контуре.
      setMode('full');
      await send(prompt);
    },
    [send],
  );

  /** Сбрасывает диалог для нового обращения. */
  const reset = useCallback(() => {
    abortRef.current?.abort();
    templateCodeRef.current = '';
    setMessages([]);
    setSessionId(null);
    setError('');
  }, []);

  return {
    messages,
    sessionId,
    mode,
    setMode,
    busy,
    error,
    send,
    stop,
    reset,
    startTemplate,
    setMessages,
  };
}
