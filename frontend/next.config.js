/** @type {import('next').NextConfig} */
const nextConfig = {
  // Обязательно для multi-stage Dockerfile: собирает минимальный server.js
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,

  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || '/api',
    NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_APP_NAME || 'Помощник по регламентам',
  },

  // В dev-режиме фронт работает на 3000, бэк на 8000 — проксируем запросы,
  // чтобы не настраивать CORS локально. В Docker проксированием занимается Nginx.
  async rewrites() {
    if (process.env.NODE_ENV === 'production') return [];
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:8000/api/:path*',
      },
    ];
  },

  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
    ];
  },

  eslint: {
    // Падать на линте во время хакатона — непозволительная роскошь
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: false,
  },
};

module.exports = nextConfig;
