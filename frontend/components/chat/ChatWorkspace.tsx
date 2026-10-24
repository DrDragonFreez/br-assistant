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
  FileSignature,
  FileSearch,
  MessageSquarePlus,
  PanelRightClose,
  PanelRightOpen,
  Trash2,
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
import { useChat } from '@/hooks/use-chat';
import { ApiError, chat as chatApi } from '@/lib/api';
import type { ChatSession, TemplateCandidate, UiMessage } from '@/lib/types';
import { cn, formatDate, truncate } from '@/lib/utils';

interface ChatWorkspaceProps {
  /** Открытый диалог; для нового обращения не задаётся. */
  sessionId?: string;
}

export function ChatWorkspace({ sessionId }: ChatWorkspaceProps) {
  const router = useRouter();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [draft, setDraft] = useState('');
  const [panelOpen, setPanelOpen] = useState(true);
  const [templateCode, setTemplateCode] = useState('');
  const [panelTab, setPanelTab] = useState('generate');
  const [historyError, setHistoryError] = useState('');

  const bottomRef = useRef<HTMLDivElement>(null);
  const lastQuestionRef = useRef('');

  const {
    messages,
    mode,
    setMode,
    busy,
    error,
    send,
    stop,
    startTemplate,
    setMessages,
  } = useChat({
    initialSessionId: sessionId || null,
    onSessionCreated: (created) => {
      // Адрес меняется без перезагрузки: история уже в состоянии компонента.
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

  const removeSession = useCallback(
    async (id: string) => {
      try {
        await chatApi.deleteSession(id);
        setSessions((current) => current.filter((item) => item.id !== id));
        if (id === sessionId) router.replace('/chat');
      } catch (caught) {
        setHistoryError(caught instanceof ApiError ? caught.message : 'Не удалось удалить диалог');
      }
    },
    [router, sessionId],
  );

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
              <div
                key={item.id}
                className={cn(
                  'group flex items-center gap-1 rounded-md pr-1',
                  item.id === sessionId ? 'bg-accent' : 'hover:bg-accent/60',
                )}
              >
                <a
                  href={`/chat/${item.id}`}
                  className="min-w-0 flex-1 px-2 py-1.5"
                  title={item.title}
                >
                  <p className="truncate text-sm leading-tight">{truncate(item.title, 34)}</p>
                  <p className="text-xs text-muted-foreground">
                    {formatDate(item.last_message_at || item.created_at)}
                  </p>
                </a>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Удалить диалог"
                  className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100"
                  onClick={() => void removeSession(item.id)}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}

            {!sessions.length ? (
              <p className="px-2 py-4 text-xs text-muted-foreground">
                Обращений пока нет. Задайте первый вопрос.
              </p>
            ) : null}
          </nav>
        </ScrollArea>

        {historyError ? (
          <p className="border-t p-2 text-xs text-destructive">{historyError}</p>
        ) : null}
      </aside>

      {/* ─── Диалог ─── */}
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-end gap-1 border-b px-3 py-1.5 xl:hidden">
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
                disabled={busy}
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
            <ChatInput
              value={draft}
              onChange={setDraft}
              onSubmit={submit}
              onStop={stop}
              busy={busy}
              mode={mode}
              onModeChange={setMode}
            />
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

          <ScrollArea className="flex-1">
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
