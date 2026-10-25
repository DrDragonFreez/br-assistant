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
  // По умолчанию полный контур, а не поток. Потоковая ветка собрана
  // отдельно от графа: в ней нет ни критика, ни подбора модуля, ни
  // перечня визирующих подразделений — то есть ровно того, ради чего
  // сотрудник и обращается к помощнику при подготовке документа.
  // Поток остаётся переключателем для вопросов, где важна скорость
  // первого слова.
  const [mode, setMode] = useState<ChatMode>('full');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  /** Текущий запрос: нужен, чтобы прервать поток при уходе со страницы. */
  const abortRef = useRef<AbortController | null>(null);
  /** Текущий диалог без задержки на перерисовку. */
  const sessionRef = useRef<string | null>(initialSessionId);
  /**
   * Диалог, о создании которого ещё не сообщено наружу.
   *
   * Сообщать сразу нельзя: получатель пишет идентификатор в адрес
   * страницы, Next выполняет это как переход, экран монтируется заново
   * и незавершённый поток обрывается.
   */
  const pendingSessionRef = useRef('');
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

  /**
   * Запоминает диалог, в котором идёт обращение.
   *
   * Наружу о новом диалоге сообщается не здесь, а по завершении
   * обращения: получатель меняет адрес страницы, а переход в середине
   * потока обрывает его.
   */
  const applySession = useCallback((id: string) => {
    if (sessionRef.current === id) return;
    sessionRef.current = id;
    pendingSessionRef.current = id;
    setSessionId(id);
  }, []);

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
            generated_document_id: response.generated_document_id,
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
        // Обращение удалено, пока оно было открыто: продолжать слать в
        // него сообщения бессмысленно, каждый повтор давал бы тот же
        // ответ. Состояние диалога очищается, и повтор уходит в новое
        // обращение.
        if (caught instanceof ApiError && caught.status === 404) {
          sessionRef.current = null;
          pendingSessionRef.current = '';
          setSessionId(null);
          const gone = 'Обращение было удалено. Нажмите «Повторить» — вопрос уйдёт в новое.';
          setError(gone);
          patchLast({ answer_status: 'error', content: gone, isStreaming: false });
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

        // Адрес страницы меняется только теперь. Раньше он менялся по
        // первому событию потока, и вызванный этим переход обрывал сам
        // поток, не дав прийти ни одному токену.
        const created = pendingSessionRef.current;
        if (created) {
          pendingSessionRef.current = '';
          onSessionCreated?.(created);
        }
      }
    },
    [appendToLast, applySession, busy, mode, onSessionCreated, patchLast, sessionId],
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
    // Ссылки сбрасываются вместе с состоянием: иначе следующий диалог
    // сравнивался бы с идентификатором предыдущего.
    sessionRef.current = null;
    pendingSessionRef.current = '';
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
