'use client';

/**
 * Уточняющий вопрос помощника.
 *
 * Помощник обязан уточнять, а не угадывать: при неполном обращении
 * бэкенд возвращает статус `clarification` или `template_filling`
 * и список вопросов. Варианты ответа выводятся кнопками — сотруднику
 * не нужно печатать, а значит ответ попадёт в ожидаемую формулировку.
 */

import { CircleHelp, ListChecks } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import type { ClarifyingQuestion } from '@/lib/types';
import { plural } from '@/lib/utils';

interface ClarifyCardProps {
  questions: ClarifyingQuestion[];
  /** Заполнение шаблона: сколько полей собрано и сколько осталось. */
  progress?: { filled: number; total: number };
  disabled?: boolean;
  /** Отправляет выбранный вариант как следующую реплику. */
  onAnswer: (answer: string) => void;
}

export function ClarifyCard({ questions, progress, disabled, onAnswer }: ClarifyCardProps) {
  if (!questions.length) return null;

  const percent =
    progress && progress.total > 0 ? Math.round((progress.filled / progress.total) * 100) : 0;

  return (
    <Card className="mt-3 border-primary/30 bg-primary/[0.03]">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          {progress ? (
            <ListChecks className="h-4 w-4 text-primary" />
          ) : (
            <CircleHelp className="h-4 w-4 text-primary" />
          )}
          {progress ? 'Заполнение документа' : 'Нужно уточнить'}
        </CardTitle>

        {progress && progress.total > 0 ? (
          <div className="space-y-1 pt-1">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                Заполнено {progress.filled} из {progress.total}{' '}
                {plural(progress.total, ['поля', 'полей', 'полей'])}
              </span>
              <span className="tabular-nums">{percent}%</span>
            </div>
            <Progress value={percent} className="h-1.5" />
          </div>
        ) : null}
      </CardHeader>

      <CardContent className="space-y-3">
        {questions.map((item, index) => (
          <div key={`${item.field}-${index}`} className="space-y-2">
            <p className="text-sm">{item.question}</p>

            {item.field ? (
              <Badge variant="outline" className="font-mono text-[11px]">
                {item.field}
              </Badge>
            ) : null}

            {item.options?.length ? (
              <div className="flex flex-wrap gap-1.5">
                {item.options.map((option) => (
                  <Button
                    key={option}
                    type="button"
                    size="sm"
                    variant="secondary"
                    disabled={disabled}
                    onClick={() => onAnswer(option)}
                  >
                    {option}
                  </Button>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
