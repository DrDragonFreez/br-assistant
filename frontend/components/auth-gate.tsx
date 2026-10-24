'use client';

/**
 * Защита страниц от неавторизованного доступа.
 *
 * Настоящая проверка прав выполняется на бэкенде при каждом запросе —
 * здесь только навигация: access-токен живёт в памяти вкладки, после
 * перезагрузки его нужно восстановить по refresh-токену, и до окончания
 * восстановления рисовать интерфейс или уводить на вход нельзя.
 */

import { Loader2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, type ReactNode } from 'react';

import { useAuth } from '@/hooks/use-auth';
import { isAuthenticated } from '@/lib/auth';

interface AuthGateProps {
  children: ReactNode;
  /** Право, без которого страница недоступна (например, kb:upload). */
  permission?: string;
}

export function AuthGate({ children, permission }: AuthGateProps) {
  const router = useRouter();
  const user = useAuth((state) => state.user);
  const loading = useAuth((state) => state.loading);
  const restore = useAuth((state) => state.restore);

  useEffect(() => {
    if (!user) {
      void restore();
    } else {
      useAuth.setState({ loading: false });
    }
  }, [restore, user]);

  useEffect(() => {
    if (!loading && !user && !isAuthenticated()) {
      router.replace('/login');
    }
  }, [loading, router, user]);

  if (loading || (!user && isAuthenticated())) {
    return (
      <div className="flex h-dvh items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!user) return null;

  if (permission && !user.permissions.includes(permission)) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-2 p-6 text-center">
        <p className="text-lg font-medium">Раздел недоступен</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          Для доступа нужно право <span className="font-mono">{permission}</span>. Обратитесь
          к администратору системы.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
