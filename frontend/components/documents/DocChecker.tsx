'use client';

/**
 * Отчёт проверки проекта документа.
 *
 * Показывает не оценку «хорошо/плохо», а конкретный перечень
 * несоответствий: что именно не так, какое требование нарушено, какой
 * пункт инструкции это требует и что сделать. Без ссылки на пункт
 * замечание бесполезно — его нельзя проверить.
 */

import { CheckCircle2, CircleAlert, Clock, Info, OctagonAlert } from 'lucide-react';

import { CitationPopover, clauseAddress } from '@/components/chat/CitationPopover';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Separator } from '@/components/ui/separator';
import type { Severity, ValidationReport } from '@/lib/types';
import { SEVERITY_LABELS, VERDICT_LABELS, cn, plural } from '@/lib/utils';

/** Цвет и значок по уровню критичности замечания. */
const SEVERITY_STYLE: Record<Severity, { className: string; icon: typeof CircleAlert }> = {
  critical: { className: 'text-destructive', icon: OctagonAlert },
  major: { className: 'text-warning', icon: CircleAlert },
  minor: { className: 'text-muted-foreground', icon: Info },
};

interface DocCheckerProps {
  report: ValidationReport;
}

export function DocChecker({ report }: DocCheckerProps) {
  const percent = Math.round(report.score * 100);
  const isPending = report.verdict === 'pending';
  const isCompliant = report.verdict === 'compliant';

  return (
    <div className="space-y-3">
      <div className="space-y-2 rounded-md border border-border bg-card p-3">
        <div className="flex items-start gap-2">
          {isPending ? (
            <Clock className="mt-0.5 h-4 w-4 shrink-0 animate-pulse text-muted-foreground" />
          ) : isCompliant ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
          ) : (
            <CircleAlert
              className={cn(
                'mt-0.5 h-4 w-4 shrink-0',
                report.verdict === 'major_issues' ? 'text-destructive' : 'text-warning',
              )}
            />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">{VERDICT_LABELS[report.verdict] || report.verdict}</p>
            <p className="truncate text-xs text-muted-foreground">{report.source_filename}</p>
          </div>
          {!isPending ? (
            <span className="shrink-0 text-sm font-medium tabular-nums">{percent}%</span>
          ) : null}
        </div>

        {!isPending ? <Progress value={percent} className="h-1.5" /> : null}

        {report.summary ? <p className="text-xs text-muted-foreground">{report.summary}</p> : null}

        {report.checked_rules ? (
          <p className="text-[11px] text-muted-foreground">
            Проверено {report.checked_rules}{' '}
            {plural(report.checked_rules, ['требование', 'требования', 'требований'])}
          </p>
        ) : null}
      </div>

      {report.violations.length ? (
        <section className="space-y-2" aria-label="Несоответствия">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {report.violations.length}{' '}
            {plural(report.violations.length, ['замечание', 'замечания', 'замечаний'])}
          </p>

          {report.violations.map((violation, index) => {
            const style = SEVERITY_STYLE[violation.severity] || SEVERITY_STYLE.minor;
            const Icon = style.icon;
            return (
              <div key={index} className="space-y-1 rounded-md border border-border bg-card p-2">
                <div className="flex items-center gap-1.5">
                  <Icon className={cn('h-3.5 w-3.5 shrink-0', style.className)} />
                  <span className={cn('text-xs font-medium', style.className)}>
                    {SEVERITY_LABELS[violation.severity] || violation.severity}
                  </span>
                  {violation.field ? (
                    <Badge variant="outline" className="text-[10px]">
                      {violation.field}
                    </Badge>
                  ) : null}
                  {violation.clause ? (
                    <Badge variant="secondary" className="ml-auto font-mono text-[10px]">
                      п. {violation.clause}
                    </Badge>
                  ) : null}
                </div>

                <p className="text-sm leading-snug">{violation.problem}</p>

                {violation.requirement ? (
                  <p className="text-xs text-muted-foreground">
                    Требование: {violation.requirement}
                  </p>
                ) : null}

                {violation.recommendation ? (
                  <>
                    <Separator className="my-1" />
                    <p className="text-xs">
                      <span className="font-medium">Как исправить: </span>
                      {violation.recommendation}
                    </p>
                  </>
                ) : null}
              </div>
            );
          })}
        </section>
      ) : !isPending ? (
        <p className="rounded-md border border-success/40 bg-success-muted/40 p-2 text-xs">
          Несоответствий требованиям инструкции не выявлено.
        </p>
      ) : null}

      {report.recommendations.length ? (
        <section className="space-y-1" aria-label="Рекомендации">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Порядок доработки
          </p>
          <ol className="space-y-1 text-xs text-muted-foreground">
            {report.recommendations.map((item, index) => (
              <li key={index} className="flex gap-1.5">
                <span className="font-medium text-foreground">{index + 1}.</span>
                <span>
                  {item.action}
                  {item.clause ? <span className="font-mono"> [п. {item.clause}]</span> : null}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {report.citations.length ? (
        <section className="space-y-1" aria-label="Положения инструкции">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Положения, по которым шла проверка
          </p>
          <div className="flex flex-wrap gap-1.5">
            {report.citations.map((citation, index) => (
              <CitationPopover key={`${citation.chunk_id}-${index}`} citation={citation}>
                <button
                  type="button"
                  className="rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[11px] hover:border-primary/40 hover:bg-accent"
                >
                  {clauseAddress(citation) || citation.document_code}
                </button>
              </CitationPopover>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
