'use client';

/**
 * Вход в систему.
 *
 * Логин — корпоративный адрес, пароль проверяет бэкенд. Подсказка
 * с демонстрационными учётными записями показывается только в режиме
 * разработки: в рабочем контуре выводить логины на экран входа нельзя.
 */

import { Loader2, LogIn, ShieldCheck, TriangleAlert } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/hooks/use-auth';
import { isAuthenticated } from '@/lib/auth';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || 'Помощник по регламентам';

export default function LoginPage() {
  const router = useRouter();
  const login = useAuth((state) => state.login);
  const user = useAuth((state) => state.user);
  const restore = useAuth((state) => state.restore);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  // Если refresh-токен ещё жив, вход не нужен — сразу в диалог.
  useEffect(() => {
    if (user) {
      router.replace('/chat');
      return;
    }
    if (isAuthenticated()) {
      void restore();
    } else {
      useAuth.setState({ loading: false });
    }
  }, [restore, router, user]);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await login(email.trim(), password);
      router.replace('/chat');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Не удалось войти');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="space-y-2 text-center">
          <ShieldCheck className="mx-auto h-8 w-8 text-primary" />
          <CardTitle>{APP_NAME}</CardTitle>
          <CardDescription>
            Вход по корпоративной учётной записи. Состав доступных документов
            определяется вашей ролью.
          </CardDescription>
        </CardHeader>

        <CardContent>
          <form onSubmit={submit} className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="email">Электронная почта</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="ivanov@cbr.local"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password">Пароль</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>

            {error ? (
              <p className="flex items-start gap-1.5 text-xs text-destructive">
                <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {error}
              </p>
            ) : null}

            <Button type="submit" className="w-full gap-2" disabled={busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              Войти
            </Button>
          </form>

          {process.env.NODE_ENV !== 'production' ? (
            <p className="mt-4 text-center text-[11px] leading-relaxed text-muted-foreground">
              Демонстрационные учётные записи создаёт команда <span className="font-mono">make seed</span>.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </main>
  );
}
