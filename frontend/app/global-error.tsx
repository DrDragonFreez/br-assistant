'use client';

/**
 * Граница ошибок корневой разметки.
 *
 * Срабатывает, если исключение произошло выше `app/error.tsx` — в самой
 * разметке или провайдерах. Такая граница заменяет документ целиком,
 * поэтому теги `html` и `body` объявляются здесь: корневая разметка в
 * этот момент уже не отрисована. Стили приложения недоступны по той же
 * причине, оформление задано атрибутом style.
 */

import { useEffect } from 'react';

interface GlobalErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function GlobalError({ error, reset }: GlobalErrorProps) {
  useEffect(() => {
    console.error('Сбой разметки приложения:', error);
  }, [error]);

  return (
    <html lang="ru">
      <body
        style={{
          display: 'flex',
          minHeight: '100dvh',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '12px',
          margin: 0,
          padding: '24px',
          textAlign: 'center',
          fontFamily: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif',
          color: '#1f2430',
          background: '#f6f7f9',
        }}
      >
        <p style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>Приложение не отвечает</p>
        <p style={{ fontSize: '14px', color: '#5a6376', margin: 0, maxWidth: '28rem' }}>
          Произошёл сбой при загрузке интерфейса. Повторите попытку — данные на сервере не
          затронуты.
        </p>
        <button
          type="button"
          onClick={reset}
          style={{
            padding: '8px 16px',
            fontSize: '14px',
            cursor: 'pointer',
            borderRadius: '6px',
            border: '1px solid #c8cedb',
            background: '#ffffff',
          }}
        >
          Повторить
        </button>
      </body>
    </html>
  );
}
