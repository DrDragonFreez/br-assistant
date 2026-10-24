import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

/**
 * Middleware маршрутизации.
 *
 * Настоящая проверка прав выполняется на бэкенде при каждом запросе —
 * здесь только навигационные удобства: редирект с корня и проставление
 * заголовка X-Request-ID, если его не выставил Nginx.
 *
 * Токен в middleware не проверяется: access-токен живёт в памяти вкладки
 * и серверному коду недоступен. Защита страниц реализована на клиенте,
 * а данные без токена всё равно не отдаются.
 */
export function middleware(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;

  if (pathname === '/') {
    return NextResponse.redirect(new URL('/chat', request.url));
  }

  const response = NextResponse.next();

  if (!request.headers.get('x-request-id')) {
    response.headers.set('x-request-id', crypto.randomUUID());
  }

  return response;
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico|.*\\.svg).*)'],
};
