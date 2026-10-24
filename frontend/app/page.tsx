import { redirect } from 'next/navigation';

/**
 * Корневой адрес.
 *
 * Обычно до него не доходит — перенаправление делает middleware. Страница
 * нужна как запасной вариант: при прямом рендере на сервере (например,
 * после ошибки middleware) пользователь всё равно попадёт в диалог.
 */
export default function HomePage(): never {
  redirect('/chat');
}
