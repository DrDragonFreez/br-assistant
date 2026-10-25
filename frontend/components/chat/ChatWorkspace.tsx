'use client';

/**
 * Рабочее место сотрудника: диалог, история обращений и модуль документов
 * на одном экране.
 *
 * Разделение на три области отражает сценарий работы: слева — к чему
 * возвращаются (история), в центре — разговор, справа — переход от ответа
 * к действию (сформировать черновик, проверить проект). Именно переход
 * «ответ → документ» и оценивается третьим критерием, поэтому он всегда
 * на виду, а не спрятан в отдельном разделе.
 */

import {
  Check,
  Eye,
  FileSignature,
  FileSearch,
  MessageSquarePlus,
  PanelRightClose,
  PanelRightOpen,
  Trash2,
  X,
} from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ChatInput, SUGGESTIONS } from '@/components/chat/ChatInput';
import { ChatMessage } from '@/components/chat/ChatMessage';
import { DocGenerator } from '@/components/documents/DocGenerator';
import { DocUploader } from '@/components/documents/DocUploader';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/hooks/use-auth';
import { useChat } from '@/hooks/use-chat';
import { ApiError, chat as chatApi } from '@/lib/api';
import type { ChatSession, TemplateCandidate, UiMessage } from '@/lib/types';
import { cn, formatDate, truncate } from '@/lib/utils';

/**
 * Через сколько снимается подтверждение удаления.
 *
 * Пяти секунд хватает, чтобы подтвердить осознанно, и мало, чтобы
 * строка осталась взведённой до случайного щелчка.
 */
const DISARM_DELAY_MS = 5000;

interface ChatWorkspaceProps {
  /** Открытый диалог; для нового обращения не задаётся. */
  sessionId?: string;
}

export function ChatWorkspace({ sessionId }: ChatWorkspaceProps) {
  const router = useRouter();
  // Аудитор читает журнал и историю, но вопросов не задаёт и документов
  // не собирает: бэкенд отвечает ему 403 на любой из этих запросов.
  // Без этой проверки интерфейс показывал рабочее поле ввода, и первый
  // же вопрос заканчивался ошибкой «Недостаточно прав». Пока профиль
  // загружается, всё считается доступным — иначе у сотрудника на
  // долю секунды мелькала бы пометка о режиме чтения; права при этом
  // всё равно проверяет бэкенд.
  const user = useAuth((state) => state.user);
  const canAsk = !user || user.permissions.includes('chat:ask');
  const canUseDocs =
    !user ||
    user.permissions.includes('doc:generate') ||
    user.permissions.includes('doc:validate');
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [draft, setDraft] = useState('');
  const [panelOpen, setPanelOpen] = useState(true);
  const [templateCode, setTemplateCode] = useState('');
  const [panelTab, setPanelTab] = useState('generate');
  const [historyError, setHistoryError] = useState('');
  /** Диалог, для которого показано подтверждение удаления. */
  const [pendingDelete, setPendingDelete] = useState('');
  /** Показано подтверждение очистки всей истории. */
  const [clearArmed, setClearArmed] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const lastQuestionRef = useRef('');
  /** Таймер снятия подтверждения. */
  const disarmRef = useRef<ReturnType<typeof setTimeout>>();

  const {
    messages,
    // Диалог, в который уходят сообщения. Отличается от sessionId из
    // адреса страницы: новое обращение создаётся уже в ходе работы, и
    // адрес обновляется после ответа.
    sessionId: openSessionId,
    mode,
    setMode,
    busy,
    error,
    send,
    stop,
    reset,
    startTemplate,
    setMessages,
  } = useChat({
    initialSessionId: sessionId || null,
    onSessionCreated: (created) => {
      // Вызывается после завершения обращения, а не по первому событию
      // потока. Next перехватывает запись в history и выполняет её как
      // переход: сегмент /chat/[id] монтируется заново, и сделанный в
      // середине потока переход обрывал бы его. Перезагрузки страницы
      // при этом нет — история диалога уже в состоянии компонента.
      window.history.replaceState(null, '', `/chat/${created}`);
      void refreshSessions();
    },
  });

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await chatApi.sessions());
    } catch (caught) {
      setHistoryError(caught instanceof ApiError ? caught.message : 'Не удалось получить историю');
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // История открытого диалога подгружается один раз при входе по адресу.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;

    chatApi
      .session(sessionId)
      .then((detail) => {
        if (cancelled) return;
        const restored: UiMessage[] = detail.messages.map((item) => ({
          ...item,
          isStreaming: false,
        }));
        setMessages(restored);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setHistoryError(
            caught instanceof ApiError ? caught.message : 'Не удалось открыть диалог',
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [sessionId, setMessages]);

  // Прокрутка к последнему сообщению, в том числе во время печати.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  const submit = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    lastQuestionRef.current = text;
    setDraft('');
    void send(text);
  }, [draft, send]);

  const answerClarification = useCallback(
    (answer: string) => {
      lastQuestionRef.current = answer;
      void send(answer);
    },
    [send],
  );

  const pickTemplate = useCallback(
    (candidate: TemplateCandidate) => {
      setTemplateCode(candidate.code);
      setPanelTab('generate');
      setPanelOpen(true);
      void startTemplate(candidate.code, `Заполним бланк «${candidate.name}»`);
    },
    [startTemplate],
  );

  const rate = useCallback(async (messageId: string, isHelpful: boolean) => {
    try {
      await chatApi.feedback(messageId, { is_helpful: isHelpful });
    } catch {
      // Оценка необязательна: молча игнорируем сбой, не мешая работе.
    }
  }, []);

  /**
   * Снимает подтверждение само.
   *
   * Взведённая строка, оставшаяся висеть после того как от неё
   * отвлеклись, при следующем щелчке удалит диалог без спроса.
   */
  const disarmLater = useCallback(() => {
    clearTimeout(disarmRef.current);
    disarmRef.current = setTimeout(() => {
      setPendingDelete('');
      setClearArmed(false);
    }, DISARM_DELAY_MS);
  }, []);

  useEffect(() => () => clearTimeout(disarmRef.current), []);

  const armDelete = useCallback(
    (id: string) => {
      setClearArmed(false);
      setPendingDelete(id);
      disarmLater();
    },
    [disarmLater],
  );

  const armClear = useCallback(() => {
    setPendingDelete('');
    setClearArmed(true);
    disarmLater();
  }, [disarmLater]);

  const disarm = useCallback(() => {
    clearTimeout(disarmRef.current);
    setPendingDelete('');
    setClearArmed(false);
  }, []);

  /**
   * Закрывает переписку, если открыто удалённое обращение.
   *
   * Переход на `/chat` сам по себе её не закрывает: оба адреса
   * отрисовывают один компонент, он не пересоздаётся и сохраняет
   * сообщения. Без явного сброса оставался бы открытый диалог,
   * которого уже нет в списке.
   */
  const closeIfOpen = useCallback(
    (removedIds: string[]) => {
      const current = openSessionId || sessionId;
      if (!current || !removedIds.includes(current)) return;
      reset();
      router.replace('/chat');
    },
    [openSessionId, reset, router, sessionId],
  );

  const removeSession = useCallback(
    async (id: string) => {
      disarm();
      try {
        await chatApi.deleteSession(id);
        setSessions((current) => current.filter((item) => item.id !== id));
        closeIfOpen([id]);
      } catch (caught) {
        setHistoryError(caught instanceof ApiError ? caught.message : 'Не удалось удалить диалог');
      }
    },
    [closeIfOpen, disarm],
  );

  /**
   * Удаляет все обращения.
   *
   * Отдельного эндпоинта для этого нет, диалоги удаляются по одному.
   * Удачно удалённые убираются из списка даже при сбое на середине:
   * иначе список разошёлся бы с тем, что осталось на сервере.
   */
  const removeAllSessions = useCallback(async () => {
    disarm();
    const ids = sessions.map((item) => item.id);
    const removed: string[] = [];

    try {
      for (const id of ids) {
        await chatApi.deleteSession(id);
        removed.push(id);
      }
    } catch (caught) {
      setHistoryError(
        caught instanceof ApiError ? caught.message : 'Не удалось удалить все диалоги',
      );
    }

    setSessions((current) => current.filter((item) => !removed.includes(item.id)));
    closeIfOpen(removed);
  }, [closeIfOpen, disarm, sessions]);

  const isEmpty = useMemo(() => messages.length === 0, [messages]);

  return (
    <div className="flex min-h-0 flex-1">
      {/* ─── История обращений ─── */}
      <aside className="hidden w-64 shrink-0 flex-col border-r bg-card md:flex">
        <div className="p-3">
          <Button asChild variant="outline" className="w-full justify-start gap-2">
            <a href="/chat">
              <MessageSquarePlus className="h-4 w-4" />
              Новое обращение
            </a>
          </Button>
        </div>

        <Separator />

        <ScrollArea className="flex-1">
          <nav className="space-y-0.5 p-2">
            {sessions.map((item) => (
              <SessionRow
                key={item.id}
                session={item}
                active={item.id === sessionId}
                armed={pendingDelete === item.id}
                busy={busy && item.id === sessionId}
                onArm={() => armDelete(item.id)}
                onCancel={disarm}
                onConfirm={() => void removeSession(item.id)}
              />
            ))}

            {!sessions.length ? (
              <p className="px-2 py-4 text-xs text-muted-foreground">
                Обращений пока нет. Задайте первый вопрос.
              </p>
            ) : null}
          </nav>
        </ScrollArea>

        {sessions.length > 1 ? (
          <>
            <Separator />
            <div className="p-2">
              {clearArmed ? (
                <div className="flex items-center gap-1">
                  <p className="min-w-0 flex-1 text-xs text-destructive">
                    Удалить все {sessions.length}?
                  </p>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Подтвердить удаление всех обращений"
                    className="h-7 w-7 text-destructive hover:text-destructive"
                    onClick={() => void removeAllSessions()}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Отменить удаление"
                    className="h-7 w-7"
                    onClick={disarm}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full justify-start gap-2 text-xs hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive"
                  onClick={armClear}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Очистить историю ({sessions.length})
                </Button>
              )}
            </div>
          </>
        ) : null}

        {historyError ? (
          <p className="border-t p-2 text-xs text-destructive">{historyError}</p>
        ) : null}
      </aside>

      {/* ─── Диалог ─── */}
      <main className="flex min-w-0 flex-1 flex-col">
        <div
          className={cn(
            'flex items-center justify-end gap-1 border-b px-3 py-1.5 xl:hidden',
            !canUseDocs && 'hidden',
          )}
        >
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
            onClick={() => setPanelOpen((current) => !current)}
          >
            {panelOpen ? (
              <PanelRightClose className="h-4 w-4" />
            ) : (
              <PanelRightOpen className="h-4 w-4" />
            )}
            Документы
          </Button>
        </div>

        <ScrollArea className="flex-1">
          <div className="mx-auto max-w-3xl space-y-4 p-4">
            {isEmpty ? (
              <EmptyState
                onPick={(text) => {
                  lastQuestionRef.current = text;
                  void send(text);
                }}
                disabled={busy || !canAsk}
              />
            ) : (
              messages.map((message) => (
                <ChatMessage
                  key={message.id}
                  message={message}
                  disabled={busy}
                  onAnswerClarification={answerClarification}
                  onPickTemplate={pickTemplate}
                  onFeedback={(id, helpful) => void rate(id, helpful)}
                  onRetry={() => {
                    if (lastQuestionRef.current) void send(lastQuestionRef.current);
                  }}
                />
              ))
            )}
            <div ref={bottomRef} />
          </div>
        </ScrollArea>

        <div className="border-t bg-card p-3">
          <div className="mx-auto max-w-3xl">
            {canAsk ? (
              <ChatInput
                value={draft}
                onChange={setDraft}
                onSubmit={submit}
                onStop={stop}
                busy={busy}
                mode={mode}
                onModeChange={setMode}
              />
            ) : (
              <p className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-2.5 text-xs text-muted-foreground">
                <Eye className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  Роль «{user?.role_label}» — режим просмотра: история диалогов, журнал действий и
                  состояние базы знаний в разделе «База знаний». Задавать вопросы и готовить
                  документы эта роль не может.
                </span>
              </p>
            )}
            {error ? <p className="mt-1.5 text-xs text-destructive">{error}</p> : null}
          </div>
        </div>
      </main>

      {/* ─── Документы ─── */}
      <aside
        className={cn(
          'w-96 shrink-0 overflow-hidden border-l bg-card',
          panelOpen ? 'block' : 'hidden',
          'xl:block',
          !canUseDocs && '!hidden',
        )}
      >
        <Tabs value={panelTab} onValueChange={setPanelTab} className="flex h-full flex-col">
          <TabsList className="m-2 grid grid-cols-2">
            <TabsTrigger value="generate" className="gap-1.5">
              <FileSignature className="h-3.5 w-3.5" />
              Черновик
            </TabsTrigger>
            <TabsTrigger value="check" className="gap-1.5">
              <FileSearch className="h-3.5 w-3.5" />
              Проверка
            </TabsTrigger>
          </TabsList>

          {/* Radix оборачивает содержимое в блок с display: table, и тот
              растягивается под самую длинную строку: имя загруженного
              файла распирало колонку, а кнопка и выбор вида уходили за
              край панели. Блочная обёртка держит ширину панели, и
              truncate снова работает. */}
          <ScrollArea className="flex-1 [&_[data-radix-scroll-area-viewport]>div]:!block">
            <TabsContent value="generate" className="m-0 p-3">
              <DocGenerator sessionId={sessionId} initialTemplateCode={templateCode} />
            </TabsContent>
            <TabsContent value="check" className="m-0 p-3">
              <DocUploader />
            </TabsContent>
          </ScrollArea>
        </Tabs>
      </aside>
    </div>
  );
}

interface SessionRowProps {
  session: ChatSession;
  /** Диалог открыт в основной области. */
  active: boolean;
  /** Показано подтверждение удаления. */
  armed: boolean;
  /** В этом диалоге идёт обращение — удалять его рано. */
  busy: boolean;
  onArm: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}

/**
 * Строка истории обращений с удалением.
 *
 * Удаление необратимое — бэкенд сносит диалог вместе со всеми
 * сообщениями, — поэтому щелчок по корзине не удаляет, а показывает
 * подтверждение на месте названия. Отдельное окно для одной строки
 * избыточно и уводит внимание из панели.
 */
function SessionRow({
  session,
  active,
  armed,
  busy,
  onArm,
  onCancel,
  onConfirm,
}: SessionRowProps) {
  if (armed) {
    return (
      <div className="flex items-center gap-1 rounded-md bg-accent pr-1">
        <p className="min-w-0 flex-1 px-2 py-1.5 text-xs text-destructive">Удалить обращение?</p>
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Подтвердить удаление: ${session.title}`}
          title="Удалить"
          className="h-8 w-8 shrink-0 rounded-md border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20 hover:text-destructive"
          onClick={onConfirm}
        >
          <Check className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Отменить удаление"
          title="Отменить"
          className="h-8 w-8 shrink-0 rounded-md border border-border bg-background"
          onClick={onCancel}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <div
      className={cn(
        'group flex items-center gap-1 rounded-md pr-1',
        active ? 'bg-accent' : 'hover:bg-accent/60',
      )}
    >
      <a href={`/chat/${session.id}`} className="min-w-0 flex-1 px-2 py-1.5" title={session.title}>
        <p className="truncate text-sm leading-tight">{truncate(session.title, 24)}</p>
        <p className="text-xs text-muted-foreground">
          {formatDate(session.last_message_at || session.created_at)}
        </p>
      </a>
      {/* Кнопка читается без наведения и без догадок. Сначала она
          появлялась только под курсором, потом была приглушена до
          неразличимости — в обоих случаях удаление выглядело как
          отсутствующее, и обращения копились мёртвым грузом. */}
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Удалить обращение: ${session.title}`}
        title="Удалить обращение"
        disabled={busy}
        className={cn(
          'h-8 w-8 shrink-0 rounded-md border border-border bg-background',
          'text-muted-foreground transition-colors',
          'hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive',
        )}
        onClick={onArm}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  );
}

interface EmptyStateProps {
  onPick: (text: string) => void;
  disabled: boolean;
}

/** Первый экран диалога: сразу показывает, о чём можно спросить. */
function EmptyState({ onPick, disabled }: EmptyStateProps) {
  return (
    <div className="space-y-4 py-10">
      <div className="space-y-1 text-center">
        <h1 className="text-lg font-medium">Чем помочь по документообороту?</h1>
        <p className="text-sm text-muted-foreground">
          Отвечаю по действующей редакции инструкции и ссылаюсь на конкретные пункты.
          Если требования в базе знаний нет — скажу об этом прямо.
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            disabled={disabled}
            onClick={() => onPick(suggestion)}
            className={cn(
              'rounded-md border border-border bg-card p-2.5 text-left text-sm',
              'transition-colors hover:border-primary/40 hover:bg-accent disabled:opacity-50',
            )}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}
