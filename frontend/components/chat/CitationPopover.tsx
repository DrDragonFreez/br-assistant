'use client';

/**
 * Точный текст положения инструкции по клику на источник.
 *
 * Критерий №1 требует, чтобы ответ опирался на конкретные пункты. Номер
 * пункта в тексте ответа этого не доказывает — сотрудник должен увидеть
 * саму формулировку, не выходя из диалога. Поэтому карточка показывает
 * адрес (документ, редакция, глава, пункт, страница), дословную цитату
 * и процент соответствия запросу.
 */

import { FileText, Hash, Layers, Percent } from 'lucide-react';
import type { ReactNode } from 'react';

import { Badge } from '@/components/ui/badge';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Separator } from '@/components/ui/separator';
import type { Citation } from '@/lib/types';
import { cn, relevanceColor } from '@/lib/utils';

interface CitationPopoverProps {
  citation: Citation;
  children: ReactNode;
}

/** Собирает адрес пункта: один номер или перечень объединённых. */
export function clauseAddress(citation: Citation): string {
  const numbers = citation.clause_numbers?.length
    ? citation.clause_numbers
    : citation.clause_number
      ? [citation.clause_number]
      : [];

  if (!numbers.length) {
    return citation.annex_number ? `Приложение ${citation.annex_number}` : '';
  }

  const label = `п. ${numbers.slice(0, 3).join(', ')}${numbers.length > 3 ? ' …' : ''}`;
  return citation.annex_number ? `Приложение ${citation.annex_number}, ${label}` : label;
}

export function CitationPopover({ citation, children }: CitationPopoverProps) {
  const address = clauseAddress(citation);

  return (
    <Popover>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent align="start" className="w-[26rem] p-0" collisionPadding={16}>
        <div className="space-y-1 p-3">
          <div className="flex items-start gap-2">
            <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">
                {citation.document_title || citation.document_code || 'База знаний'}
              </p>
              {citation.version_label ? (
                <p className="text-xs text-muted-foreground">{citation.version_label}</p>
              ) : null}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            {address ? (
              <Badge variant="secondary" className="gap-1 font-mono text-[11px]">
                <Hash className="h-3 w-3" />
                {address}
              </Badge>
            ) : null}
            {citation.page_number ? (
              <Badge variant="outline" className="text-[11px]">
                с. {citation.page_number}
              </Badge>
            ) : null}
            <Badge variant="outline" className={cn('gap-1 text-[11px]', relevanceColor(citation.relevance_percent))}>
              <Percent className="h-3 w-3" />
              {citation.relevance_percent}
            </Badge>
          </div>

          {citation.breadcrumbs ? (
            <p className="flex items-start gap-1.5 pt-1 text-xs text-muted-foreground">
              <Layers className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="min-w-0">{citation.breadcrumbs}</span>
            </p>
          ) : null}
        </div>

        <Separator />

        <blockquote className="prose-clause max-h-64 overflow-y-auto scrollbar-thin p-3 text-muted-foreground">
          {citation.quote || 'Текст положения недоступен.'}
        </blockquote>
      </PopoverContent>
    </Popover>
  );
}
