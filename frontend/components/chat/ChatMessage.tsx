'use client';

/**
 * Реплика диалога.
 *
 * Вид ответа задаёт `answer_status` — это не косметика, а способ показать
 * сотруднику, насколько ответу можно доверять:
 *
 * * `answered` — ответ с источниками и процентом релевантности;
 * * `no_context` — честный отказ, источники не показываются вовсе;
 * * `clarification` / `template_filling` — карточка уточнения;
 * * `template_ready` — готовый черновик и кнопка скачивания;
 * * `info` — справочный ответ без обращения к базе знаний;
 * * `error` — сбой с предложением повторить.
 *
 * Вердикт критика выводится рядом с ответом: на защите он показывает,
 * что ответ прошёл дополнительную проверку на выдумывание.
 */

import {
  AlertTriangle,
  BadgeCheck,
  Bot,
  Download,
  FileCheck2,
  Info,
  Loader2,
  RefreshCw,
  SearchX,
  ShieldAlert,
  ThumbsDown,
  ThumbsUp,
  User as UserIcon,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { ClarifyCard } from '@/components/chat/ClarifyCard';
import { SourceList } from '@/components/chat/SourceBadge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { documents } from '@/lib/api';
import type { GeneratedDocument, TemplateCandidate, UiMessage } from '@/lib/types';
import { cn, formatFileSize, formatLatency, relevanceColor } from '@/lib/utils';

/** Подписи вердикта критика. */
const CRITIC_LABELS: Record<string, string> = {
  approved: 'Ответ проверен критиком',
  needs_revision: 'Ответ переписан после замечаний критика',
  rejected: 'Критик отклонил ответ',
  skipped: 'Проверка критиком не выполнялась',
};

interface ChatMessageProps {
  message: UiMessage;
  disabled?: boolean;
  /** Отправка ответа на уточняющий вопрос. */
  onAnswerClarification?: (answer: string) => void;
  /** Выбор шаблона из подобранных кандидатов. */
  onPickTemplate?: (candidate: TemplateCandidate) => void;
  /** Оценка ответа; доступна только для сохранённых сообщений. */
  onFeedback?: (messageId: string, isHelpful: boolean) => void;
  /** Повторная отправка последнего обращения. */
  onRetry?: () => void;
}

export function ChatMessage({
  message,
  disabled,
  onAnswerClarification,
  onPickTemplate,
  onFeedback,
  onRetry,
}: ChatMessageProps) {
  const [rated, setRated] = useState<boolean | null>(null);

  if (message.role === 'user') {
    return (
      <div className="flex justify-end gap-3 animate-fade-in">
        <div className="max-w-[80%] rounded-lg rounded-br-sm bg-primary px-3 py-2 text-sm text-primary-foreground">
          {message.content}
        </div>
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-secondary">
          <UserIcon className="h-4 w-4 text-secondary-foreground" />
        </div>
      </div>
    );
  }

  const status = message.answer_status;
  const isRefusal = status === 'no_context';
  const isError = status === 'error';
  const showSources = !isRefusal && !isError && status !== 'info' && message.citations.length > 0;

  return (
    <div className="flex gap-3 animate-fade-in">
      <div
        className={cn(
          'mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full',
          isError ? 'bg-destructive/10' : 'bg-primary/10',
        )}
      >
        {isError ? (
          <AlertTriangle className="h-4 w-4 text-destructive" />
        ) : isRefusal ? (
          <SearchX className="h-4 w-4 text-muted-foreground" />
        ) : status === 'info' ? (
          <Info className="h-4 w-4 text-primary" />
        ) : (
          <Bot className="h-4 w-4 text-primary" />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div
          className={cn(
            'rounded-lg rounded-tl-sm border px-3 py-2',
            isRefusal && 'border-dashed bg-muted/40 text-muted-foreground',
            isError && 'border-destructive/40 bg-destructive/5',
            !isRefusal && !isError && 'bg-card',
          )}
        >
          {message.content ? (
            <div className={cn('answer-body', message.isStreaming && 'streaming-caret')}>
              <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown>
            </div>
          ) : (
            <div className="flex items-center gap-1 py-1" aria-label="Помощник печатает">
              <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-primary" />
              <span
                className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-primary"
                style={{ animationDelay: '0.2s' }}
              />
              <span
                className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-primary"
                style={{ animationDelay: '0.4s' }}
              />
            </div>
          )}

          {isError && onRetry ? (
            <Button variant="outline" size="sm" className="mt-2 gap-1.5" onClick={onRetry}>
              <RefreshCw className="h-3.5 w-3.5" />
              Повторить
            </Button>
          ) : null}
        </div>

        {showSources ? <SourceList citations={message.citations} /> : null}

        {message.clarifying_questions?.length && onAnswerClarification ? (
          <ClarifyCard
            questions={message.clarifying_questions}
            disabled={disabled}
            onAnswer={onAnswerClarification}
          />
        ) : null}

        {message.generated_document_id ? (
          <GeneratedDraft documentId={message.generated_document_id} />
        ) : null}

        {message.template_candidates?.length && onPickTemplate ? (
          <TemplateCandidates
            candidates={message.template_candidates}
            disabled={disabled}
            onPick={onPickTemplate}
          />
        ) : null}

        {!message.isStreaming && message.content ? (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {message.critic_verdict && CRITIC_LABELS[message.critic_verdict] ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge
                    variant="outline"
                    className={cn(
                      'gap-1',
                      message.critic_verdict === 'approved' && 'text-success',
                      message.critic_verdict === 'rejected' && 'text-destructive',
                    )}
                  >
                    {message.critic_verdict === 'approved' ? (
                      <BadgeCheck className="h-3 w-3" />
                    ) : (
                      <ShieldAlert className="h-3 w-3" />
                    )}
                    критик
                  </Badge>
                </TooltipTrigger>
                <TooltipContent>{CRITIC_LABELS[message.critic_verdict]}</TooltipContent>
              </Tooltip>
            ) : null}

            {typeof message.top_relevance_percent === 'number' && showSources ? (
              <span className={relevanceColor(message.top_relevance_percent)}>
                релевантность {message.top_relevance_percent}%
              </span>
            ) : null}

            {message.latency_ms ? <span>{formatLatency(message.latency_ms)}</span> : null}
            {message.model_name ? <span className="font-mono">{message.model_name}</span> : null}

            {/* Ответ из кеша отмечается явно: иначе мгновенная выдача
                читается как скорость контура, хотя ни поиск, ни модель
                не вызывались. */}
            {message.from_cache ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="cursor-default underline decoration-dotted">из кеша</span>
                </TooltipTrigger>
                <TooltipContent>
                  Ответ на такой же вопрос уже формировался: поиск и модель не вызывались
                </TooltipContent>
              </Tooltip>
            ) : null}

            {onFeedback && !message.id.startsWith('local-') && !isError ? (
              <span className="ml-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className={cn('h-6 w-6', rated === true && 'text-success')}
                  aria-label="Ответ помог"
                  onClick={() => {
                    setRated(true);
                    onFeedback(message.id, true);
                  }}
                >
                  <ThumbsUp className="h-3.5 w-3.5" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className={cn('h-6 w-6', rated === false && 'text-destructive')}
                  aria-label="Ответ не помог"
                  onClick={() => {
                    setRated(false);
                    onFeedback(message.id, false);
                  }}
                >
                  <ThumbsDown className="h-3.5 w-3.5" />
                </Button>
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Интервал опроса готовности черновика. */
const DRAFT_POLL_INTERVAL_MS = 2000;
/** Предел ожидания: дольше — считаем, что сборка не удалась. */
const DRAFT_POLL_ATTEMPTS = 30;

interface GeneratedDraftProps {
  documentId: string;
}

/**
 * Черновик, собранный по итогам заполнения модуля в диалоге.
 *
 * Сборка идёт фоновой задачей, поэтому состояние опрашивается до
 * готовности файла. Без этой карточки диалог доходил до последнего
 * поля и обрывался: скачать собранное можно было только через форму
 * справа, заполнив всё заново.
 */
function GeneratedDraft({ documentId }: GeneratedDraftProps) {
  const [draft, setDraft] = useState<GeneratedDocument | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const poll = async (): Promise<void> => {
      for (let attempt = 0; attempt < DRAFT_POLL_ATTEMPTS; attempt += 1) {
        if (cancelled) return;
        try {
          const record = await documents.generatedOne(documentId);
          if (cancelled) return;
          setDraft(record);
          if (record.status === 'ready') return;
          if (record.status === 'failed') {
            setFailed(true);
            return;
          }
        } catch {
          // Запись могла ещё не появиться либо запрос упёрся в лимит:
          // следующая попытка через пару секунд.
        }
        await new Promise((resolve) => setTimeout(resolve, DRAFT_POLL_INTERVAL_MS));
      }
      if (!cancelled) setFailed(true);
    };

    void poll();
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  if (failed) {
    return (
      <p className="mt-3 text-xs text-destructive">
        Не удалось собрать файл. Черновик остался в разделе «Черновик» справа — соберите
        его оттуда.
      </p>
    );
  }

  if (!draft || draft.status !== 'ready') {
    return (
      <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Файл собирается…
      </p>
    );
  }

  return (
    <section
      className="mt-3 space-y-2 rounded-md border border-success/40 bg-success-muted/40 p-3"
      aria-label="Готовый черновик"
    >
      <p className="text-sm font-medium">Черновик готов</p>
      <p className="text-xs text-muted-foreground">
        {draft.filename} · {formatFileSize(draft.file_size)}
      </p>
      <Button
        size="sm"
        variant="secondary"
        className="w-full gap-2"
        onClick={() => void documents.download(draft.id, draft.filename)}
      >
        <Download className="h-4 w-4" />
        Скачать DOCX
      </Button>
    </section>
  );
}

interface TemplateCandidatesProps {
  candidates: TemplateCandidate[];
  disabled?: boolean;
  onPick: (candidate: TemplateCandidate) => void;
}

/** Подобранные шаблоны документов с процентом соответствия обращению. */
function TemplateCandidates({ candidates, disabled, onPick }: TemplateCandidatesProps) {
  return (
    <section className="mt-3 space-y-2" aria-label="Подходящие шаблоны">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Подходящие бланки
      </p>
      <div className="space-y-1.5">
        {candidates.map((candidate) => (
          <div
            key={candidate.code}
            className="flex items-start gap-2 rounded-md border border-border bg-card p-2"
          >
            <FileCheck2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium leading-snug">{candidate.name}</p>
              {candidate.description ? (
                <p className="mt-0.5 text-xs text-muted-foreground">{candidate.description}</p>
              ) : null}
              {candidate.related_clauses?.length ? (
                <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                  требования: п. {candidate.related_clauses.slice(0, 4).join(', ')}
                </p>
              ) : null}
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <span className={cn('text-xs tabular-nums', relevanceColor(candidate.match_percent))}>
                {candidate.match_percent}%
              </span>
              <Button size="sm" variant="secondary" disabled={disabled} onClick={() => onPick(candidate)}>
                Заполнить
              </Button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
