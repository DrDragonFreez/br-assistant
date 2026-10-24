import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';

import { TooltipProvider } from '@/components/ui/tooltip';

import './globals.css';

/**
 * Корневая разметка.
 *
 * Внешние шрифты намеренно не подключаются: next/font скачивает файлы
 * на этапе сборки, а образ собирается в контуре, где интернета может
 * не быть. Системный стек задан переменной --font-sans в globals.css.
 */

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || 'Помощник по регламентам';

export const metadata: Metadata = {
  title: {
    default: APP_NAME,
    template: `%s · ${APP_NAME}`,
  },
  description:
    'Интеллектуальный помощник по внутренним регламентам: отвечает со ссылками на пункты ' +
    'инструкции, формирует черновики документов по модулям и проверяет готовые проекты.',
  // Внутренний сервис: индексировать нечего и незачем.
  robots: { index: false, follow: false },
  icons: { icon: '/favicon.svg' },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#1b3a7a',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body className="font-sans antialiased">
        <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
      </body>
    </html>
  );
}
