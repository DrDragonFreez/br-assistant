'use client';

/**
 * Карточка источника под ответом помощника.
 *
 * Показывает адрес положения и процент соответствия запросу; по клику
 * раскрывается дословная цитата (см. CitationPopover). Порядок карточек
 * задаёт бэкенд: первыми идут пункты, на которые модель действительно
 * сослалась в тексте ответа.
 */

import { Quote } from 'lucide-react';

import { CitationPopover, clauseAddress } from '@/components/chat/CitationPopover';
import { Progress } from '@/components/ui/progress';
import type { Citation } from '@/lib/types';
import { cn, plural, relevanceColor, truncate } from '@/lib/utils';

interface SourceBadgeProps {
  citation: Citation;
}

export function SourceBadge({ citation }: SourceBadgeProps) {
  const address = clauseAddress(citation);

  return (
    <CitationPopover citation={citation}>
      <button
        type="button"
        className={cn(
          'group flex w-full items-start gap-2 rounded-md border border-border bg-card p-2 text-left',
          'transition-colors hover:border-primary/40 hover:bg-accent',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
      >
        <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground group-hover:text-primary" />
        <span className="min-w-0 flex-1 space-y-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate font-mono text-xs font-medium">
              {address || citation.document_code || 'Источник'}
            </span>
            <span className={cn('shrink-0 text-xs tabular-nums', relevanceColor(citation.relevance_percent))}>
              {citation.relevance_percent}%
            </span>
          </span>
          <span className="block text-xs leading-snug text-muted-foreground">
            {truncate(citation.quote || citation.section_title || '', 90)}
          </span>
          <Progress value={citation.relevance_percent} className="h-0.5" />
        </span>
      </button>
    </CitationPopover>
  );
}

interface SourceListProps {
  citations: Citation[];
}

/** Блок источников под ответом. */
export function SourceList({ citations }: SourceListProps) {
  if (!citations.length) return null;

  return (
    <section className="mt-3 space-y-2" aria-label="Источники ответа">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {citations.length} {plural(citations.length, ['источник', 'источника', 'источников'])}{' '}
        из инструкции
      </p>
      <div className="grid gap-1.5 sm:grid-cols-2">
        {citations.map((citation, index) => (
          <SourceBadge key={`${citation.chunk_id}-${index}`} citation={citation} />
        ))}
      </div>
    </section>
  );
}
