'use client';

/**
 * Граница ошибок приложения.
 *
 * Без неё любое исключение в рендере уводит пользователя на страницу
 * «Application error: a client-side exception has occurred» — без
 * объяснения и без возможности вернуться. На защите прототипа это
 * выглядит как полный отказ, хотя отказал один компонент.
 *
 * Здесь ошибка остаётся локальной: `reset()` перерисовывает ветку
 * заново, а состояние приложения — профиль, access-токен в памяти,
 * открытый диалог — сохраняется, потому что перезагрузки страницы не
 * происходит. Повторный вход не нужен.
 */

import { RotateCcw, TriangleAlert } from 'lucide-react';
import { useEffect } from 'react';

import { Button } from '@/components/ui/button';

interface ErrorBoundaryProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ErrorBoundary({ error, reset }: ErrorBoundaryProps) {
  useEffect(() => {
    // Текст ошибки нужен для разбора; в консоль, а не на экран —
    // пользователю он ничего не говорит.
    console.error('Сбой интерфейса:', error);
  }, [error]);

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-3 p-6 text-center">
      <TriangleAlert className="h-6 w-6 text-destructive" />
      <div className="space-y-1">
        <p className="text-lg font-medium">Не удалось показать раздел</p>
        <p className="max-w-md text-sm text-muted-foreground">
          Сбой затронул только эту часть интерфейса. Сессия и история обращений сохранены —
          достаточно повторить попытку.
        </p>
      </div>
      <Button onClick={reset} variant="outline" size="sm" className="gap-1.5">
        <RotateCcw className="h-3.5 w-3.5" />
        Повторить
      </Button>
      {error.digest ? (
        <p className="font-mono text-[11px] text-muted-foreground">код: {error.digest}</p>
      ) : null}
    </div>
  );
}
