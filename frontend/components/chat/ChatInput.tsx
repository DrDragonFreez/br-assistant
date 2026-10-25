'use client';

/**
 * Поле ввода обращения.
 *
 * Переключатель режима стоит рядом с полем не ради настроек: в потоковом
 * режиме ответ начинает печататься за секунду, а полный контур добавляет
 * проверку критиком. Сотрудник (и жюри на защите) должен видеть, какой
 * режим включён, потому что от этого зависит состав ответа.
 */

import { Send, Square, Zap } from 'lucide-react';
import { useEffect, useRef, type KeyboardEvent } from 'react';

import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { ChatMode } from '@/hooks/use-chat';
import { cn } from '@/lib/utils';

const MAX_LENGTH = 4000;

/** Подсказки для пустого диалога: сразу показывают, о чём можно спросить. */
export const SUGGESTIONS: string[] = [
  'Какой срок согласования служебной записки?',
  'Кто визирует приказ о приёме и передаче дел?',
  'Подготовь проект распоряжения о создании рабочей группы',
  'Что сказано в п. 3.1 инструкции?',
];

interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  busy: boolean;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  disabled?: boolean;
}

export function ChatInput({
  value,
  onChange,
  onSubmit,
  onStop,
  busy,
  mode,
  onModeChange,
  disabled,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Поле растёт под текст: длинные формулировки вопросов — норма.
  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }, [value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    // Enter отправляет, Shift+Enter переносит строку.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (!busy && value.trim()) onSubmit();
    }
  };

  return (
    <div className="space-y-2">
      <div
        className={cn(
          'flex items-end gap-2 rounded-lg border bg-card p-2',
          'focus-within:border-primary/40 focus-within:ring-1 focus-within:ring-ring',
        )}
      >
        <Textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => onChange(event.target.value.slice(0, MAX_LENGTH))}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder="Спросите о требованиях инструкции или попросите подготовить документ…"
          className="min-h-9 resize-none border-0 bg-transparent p-1 shadow-none focus-visible:ring-0"
        />

        {busy ? (
          <Button type="button" size="icon" variant="outline" onClick={onStop} aria-label="Остановить">
            <Square className="h-4 w-4" />
          </Button>
        ) : (
          <Button
            type="button"
            size="icon"
            onClick={onSubmit}
            disabled={disabled || !value.trim()}
            aria-label="Отправить"
          >
            <Send className="h-4 w-4" />
          </Button>
        )}
      </div>

      <div className="flex items-center justify-between gap-2 px-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={() => onModeChange(mode === 'stream' ? 'full' : 'stream')}
              disabled={busy}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors',
                'hover:bg-accent disabled:opacity-50',
                mode === 'full' ? 'text-primary' : 'text-muted-foreground',
              )}
            >
              <Zap className={cn('h-3.5 w-3.5', mode === 'stream' && 'text-warning')} />
              {mode === 'stream' ? 'Потоковый ответ' : 'Полный контур с критиком'}
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            {mode === 'stream'
              ? 'Текст печатается по мере генерации, источники приходят первыми. В этом режиме не выполняются проверка критиком, подбор модуля документа и уточняющие вопросы — нажмите, чтобы вернуться к полному контуру.'
              : 'Полный разбор обращения: проверка критиком на выдуманные ссылки, подбор подходящего модуля, перечень визирующих подразделений, уточняющие вопросы. Ответ приходит целиком, около 6 секунд.'}
          </TooltipContent>
        </Tooltip>

        <span className="text-xs text-muted-foreground">
          Enter — отправить, Shift+Enter — перенос строки
        </span>
      </div>
    </div>
  );
}
