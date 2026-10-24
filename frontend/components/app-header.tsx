'use client';

/**
 * Заголовок приложения: кто вошёл, с какой ролью и какие разделы доступны.
 *
 * Роль выводится явно: от неё зависит и состав доступных положений
 * инструкции, и набор разделов. Сотрудник должен понимать, почему
 * часть документов он не видит.
 */

import { LogOut, MessagesSquare, Settings2, ShieldCheck } from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useAuth } from '@/hooks/use-auth';
import { ACCESS_LEVEL_LABELS, ROLE_LABELS, cn } from '@/lib/utils';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || 'Помощник по регламентам';

export function AppHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const user = useAuth((state) => state.user);
  const logout = useAuth((state) => state.logout);

  const canSeeAdmin = Boolean(
    user?.permissions.some((item) => ['kb:upload', 'user:read', 'audit:read'].includes(item)),
  );

  const maxLevel = user?.allowed_access_levels?.at(-1) || 'public';

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-card px-4">
      <Link href="/chat" className="flex items-center gap-2 font-medium">
        <ShieldCheck className="h-5 w-5 text-primary" />
        <span className="hidden sm:inline">{APP_NAME}</span>
      </Link>

      <Separator orientation="vertical" className="hidden h-6 sm:block" />

      <nav className="flex items-center gap-1">
        <Button
          asChild
          variant={pathname.startsWith('/chat') ? 'secondary' : 'ghost'}
          size="sm"
          className="gap-1.5"
        >
          <Link href="/chat">
            <MessagesSquare className="h-4 w-4" />
            Диалог
          </Link>
        </Button>

        {canSeeAdmin ? (
          <Button
            asChild
            variant={pathname.startsWith('/admin') ? 'secondary' : 'ghost'}
            size="sm"
            className="gap-1.5"
          >
            <Link href="/admin">
              <Settings2 className="h-4 w-4" />
              База знаний
            </Link>
          </Button>
        ) : null}
      </nav>

      <div className="ml-auto flex items-center gap-2">
        {user ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="hidden text-right sm:block">
                <p className="text-sm leading-tight">{user.full_name || user.email}</p>
                <p className="text-xs leading-tight text-muted-foreground">
                  {ROLE_LABELS[user.role] || user.role}
                </p>
              </div>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              <p>{user.department || 'Подразделение не указано'}</p>
              <p className="mt-1">
                Доступные документы: {ACCESS_LEVEL_LABELS[maxLevel] || maxLevel} и ниже
              </p>
            </TooltipContent>
          </Tooltip>
        ) : null}

        <Badge variant="outline" className={cn('hidden md:inline-flex')}>
          {ACCESS_LEVEL_LABELS[maxLevel] || maxLevel}
        </Badge>

        <Button
          variant="ghost"
          size="icon"
          aria-label="Выйти"
          onClick={async () => {
            await logout();
            router.replace('/login');
          }}
        >
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
    </header>
  );
}
