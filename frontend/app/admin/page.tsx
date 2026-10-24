'use client';

/**
 * Управление базой знаний.
 *
 * Экран отвечает за актуализацию инструкции — требование версионности
 * из условий кейса: редакции загружаются, индексируются фоновой задачей
 * и становятся действующими по команде. Прежние сохраняются, к ним можно
 * вернуться, и в ответах видно, какой редакцией помощник руководствуется.
 */

import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Database,
  FileUp,
  History,
  Loader2,
  RefreshCw,
  ScrollText,
  Sparkles,
  Upload,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { AppHeader } from '@/components/app-header';
import { AuthGate } from '@/components/auth-gate';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/hooks/use-auth';
import { ApiError, admin } from '@/lib/api';
import type {
  AuditLogEntry,
  EvalRun,
  HealthResponse,
  KnowledgeBaseStats,
  KnowledgeDocument,
} from '@/lib/types';
import {
  DOC_TYPE_LABELS,
  VERSION_STATUS_LABELS,
  cn,
  formatDateTime,
  plural,
} from '@/lib/utils';

export default function AdminPage() {
  return (
    <AuthGate permission="kb:read">
      <div className="flex min-h-dvh flex-col">
        <AppHeader />
        <AdminContent />
      </div>
    </AuthGate>
  );
}

function AdminContent() {
  const can = useAuth((state) => state.can);

  const [stats, setStats] = useState<KnowledgeBaseStats | null>(null);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [audit, setAudit] = useState<AuditLogEntry[]>([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const [statsData, docsData] = await Promise.all([
        admin.knowledgeStats(),
        admin.knowledgeDocuments(),
      ]);
      setStats(statsData);
      setDocs(docsData);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Не удалось получить состояние базы');
    }

    // Необязательные разделы: отсутствие права — не ошибка экрана.
    if (can('eval:run') || can('metrics:read')) {
      try {
        setRuns(await admin.evalRuns());
      } catch {
        setRuns([]);
      }
    }
    if (can('metrics:read')) {
      try {
        setHealth(await admin.health());
      } catch {
        setHealth(null);
      }
    }
    if (can('audit:read')) {
      try {
        setAudit(await admin.audit(30));
      } catch {
        setAudit([]);
      }
    }
  }, [can]);

  useEffect(() => {
    void load();
  }, [load]);

  const reindex = async (): Promise<void> => {
    setBusy(true);
    setNotice('');
    try {
      const accepted = await admin.reindex(true);
      setNotice(accepted.message || 'Переиндексация запущена');
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Не удалось запустить переиндексацию');
    } finally {
      setBusy(false);
    }
  };

  const runEval = async (): Promise<void> => {
    setBusy(true);
    setNotice('');
    try {
      const accepted = await admin.runEval({ skip_ragas: true });
      setNotice(accepted.message || 'Оценка качества запущена');
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Не удалось запустить оценку');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 space-y-4 p-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={Database}
          label="Фрагментов в индексе"
          value={stats ? stats.points_count.toLocaleString('ru-RU') : '—'}
          hint={stats?.collection}
        />
        <StatCard
          icon={ScrollText}
          label="Документов"
          value={stats ? String(stats.documents_count) : '—'}
          hint={stats ? `действующих редакций: ${stats.active_versions}` : undefined}
        />
        <StatCard
          icon={FileUp}
          label="Бланков документов"
          value={stats ? String(stats.templates_count) : '—'}
        />
        <StatCard
          icon={CircleAlert}
          label="Ошибок индексации"
          value={stats ? String(stats.failed_versions) : '—'}
          tone={stats && stats.failed_versions > 0 ? 'warning' : 'default'}
        />
      </div>

      {notice ? (
        <p className="rounded-md border border-success/40 bg-success-muted/40 p-2 text-sm">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <Tabs defaultValue="documents">
        <TabsList>
          <TabsTrigger value="documents" className="gap-1.5">
            <ScrollText className="h-3.5 w-3.5" />
            Документы
          </TabsTrigger>
          <TabsTrigger value="quality" className="gap-1.5">
            <Sparkles className="h-3.5 w-3.5" />
            Качество
          </TabsTrigger>
          <TabsTrigger value="state" className="gap-1.5">
            <Activity className="h-3.5 w-3.5" />
            Состояние
          </TabsTrigger>
          {can('audit:read') ? (
            <TabsTrigger value="audit" className="gap-1.5">
              <History className="h-3.5 w-3.5" />
              Журнал
            </TabsTrigger>
          ) : null}
        </TabsList>

        {/* ─── Документы и редакции ─── */}
        <TabsContent value="documents" className="space-y-4">
          {can('kb:upload') ? <UploadForm documents={docs} onUploaded={load} /> : null}

          {can('kb:reindex') ? (
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={reindex}>
                {busy ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                Переиндексировать базу знаний
              </Button>
              <p className="text-xs text-muted-foreground">
                Пересобирает фрагменты и словарь поиска по действующим редакциям.
              </p>
            </div>
          ) : null}

          <div className="space-y-3">
            {docs.map((document) => (
              <Card key={document.id}>
                <CardHeader className="pb-2">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <CardTitle className="text-sm">{document.title}</CardTitle>
                      <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                        {document.code}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      <Badge variant="secondary" className="text-[11px]">
                        {DOC_TYPE_LABELS[document.doc_type] || document.doc_type}
                      </Badge>
                      <Badge variant="outline" className="text-[11px]">
                        {document.access_level}
                      </Badge>
                    </div>
                  </div>
                </CardHeader>

                <CardContent className="space-y-1.5">
                  {document.versions.length ? (
                    document.versions.map((version) => (
                      <div
                        key={version.id}
                        className={cn(
                          'flex flex-wrap items-center gap-2 rounded-md border p-2 text-xs',
                          version.is_active ? 'border-success/40 bg-success-muted/30' : 'border-border',
                        )}
                      >
                        <span className="font-mono">ред. {version.version_number}</span>
                        {version.version_label ? <span>{version.version_label}</span> : null}

                        <Badge
                          variant={version.status === 'indexed' ? 'secondary' : 'outline'}
                          className="text-[10px]"
                        >
                          {VERSION_STATUS_LABELS[version.status] || version.status}
                        </Badge>

                        {version.is_active ? (
                          <Badge className="gap-1 bg-success text-[10px] text-success-foreground">
                            <CheckCircle2 className="h-3 w-3" />
                            действующая
                          </Badge>
                        ) : null}

                        <span className="text-muted-foreground">
                          {version.chunks_count}{' '}
                          {plural(version.chunks_count, ['фрагмент', 'фрагмента', 'фрагментов'])}
                          {version.pages_count ? `, ${version.pages_count} с.` : ''}
                        </span>

                        <span className="ml-auto text-muted-foreground">
                          {formatDateTime(version.indexed_at || version.created_at)}
                        </span>

                        {!version.is_active && can('kb:version:manage') ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-6 text-[11px]"
                            onClick={async () => {
                              try {
                                await admin.activateVersion(document.code, version.version_number);
                                setNotice(`Редакция ${version.version_number} сделана действующей`);
                                await load();
                              } catch (caught) {
                                setError(
                                  caught instanceof ApiError
                                    ? caught.message
                                    : 'Не удалось переключить редакцию',
                                );
                              }
                            }}
                          >
                            Сделать действующей
                          </Button>
                        ) : null}

                        {version.indexing_error ? (
                          <p className="w-full text-destructive">{version.indexing_error}</p>
                        ) : null}
                      </div>
                    ))
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      Редакций нет. Загрузите файл документа.
                    </p>
                  )}
                </CardContent>
              </Card>
            ))}

            {!docs.length ? (
              <p className="text-sm text-muted-foreground">
                Документы не зарегистрированы. Выполните <span className="font-mono">make seed</span>,
                затем загрузите инструкцию.
              </p>
            ) : null}
          </div>
        </TabsContent>

        {/* ─── Качество ответов ─── */}
        <TabsContent value="quality" className="space-y-3">
          {can('eval:run') ? (
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={runEval}>
                {busy ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                Запустить быструю оценку
              </Button>
              <p className="text-xs text-muted-foreground">
                Прогон по эталонному набору без метрик RAGAS — занимает минуты, а не десятки минут.
              </p>
            </div>
          ) : null}

          <div className="space-y-2">
            {runs.map((run) => (
              <Card key={run.id}>
                <CardContent className="space-y-2 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{run.name || 'Прогон оценки'}</p>
                      <p className="font-mono text-xs text-muted-foreground">{run.model_name}</p>
                    </div>
                    <Badge variant={run.status === 'completed' ? 'secondary' : 'outline'}>
                      {run.status}
                    </Badge>
                  </div>

                  <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                    <Metric label="Вопросов" value={run.questions_total} />
                    <Metric label="Точность цитат" value={run.citation_accuracy} percent />
                    <Metric label="Честных отказов" value={run.no_answer_rate} percent />
                    <Metric label="Faithfulness" value={run.faithfulness} percent />
                    <Metric label="Answer relevancy" value={run.answer_relevancy} percent />
                    <Metric label="Context recall" value={run.context_recall} percent />
                  </div>

                  <p className="text-[11px] text-muted-foreground">
                    {formatDateTime(run.created_at)} · {run.duration_seconds} с
                  </p>
                </CardContent>
              </Card>
            ))}

            {!runs.length ? (
              <p className="text-sm text-muted-foreground">Прогонов оценки пока не было.</p>
            ) : null}
          </div>
        </TabsContent>

        {/* ─── Состояние контура ─── */}
        <TabsContent value="state" className="space-y-3">
          {health ? (
            <>
              <p className="text-sm">
                Общее состояние:{' '}
                <span
                  className={cn(
                    'font-medium',
                    health.status === 'ok' && 'text-success',
                    health.status === 'degraded' && 'text-warning',
                    health.status === 'unavailable' && 'text-destructive',
                  )}
                >
                  {health.status}
                </span>{' '}
                <span className="text-muted-foreground">
                  · версия {health.version} · {health.environment}
                </span>
              </p>

              <div className="grid gap-2 sm:grid-cols-2">
                {health.components.map((component) => (
                  <div
                    key={component.name}
                    className="flex items-start gap-2 rounded-md border border-border p-2"
                  >
                    {component.healthy ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                    ) : (
                      <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                    )}
                    <div className="min-w-0">
                      <p className="font-mono text-sm">{component.name}</p>
                      {Object.entries(component.details || {}).length ? (
                        <dl className="mt-0.5 space-y-0.5 text-[11px] text-muted-foreground">
                          {Object.entries(component.details).map(([key, value]) => (
                            <div key={key} className="flex gap-1">
                              <dt className="font-mono">{key}:</dt>
                              <dd className="truncate">{String(value)}</dd>
                            </div>
                          ))}
                        </dl>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Состояние компонентов доступно при наличии права{' '}
              <span className="font-mono">metrics:read</span>.
            </p>
          )}
        </TabsContent>

        {/* ─── Журнал действий ─── */}
        {can('audit:read') ? (
          <TabsContent value="audit">
            <ScrollArea className="h-96 rounded-md border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted">
                  <tr>
                    <th className="p-2 text-left font-medium">Время</th>
                    <th className="p-2 text-left font-medium">Действие</th>
                    <th className="p-2 text-left font-medium">Объект</th>
                    <th className="p-2 text-left font-medium">Пользователь</th>
                    <th className="p-2 text-left font-medium">Исход</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.map((entry) => (
                    <tr key={entry.id} className="border-t">
                      <td className="whitespace-nowrap p-2 text-muted-foreground">
                        {formatDateTime(entry.created_at)}
                      </td>
                      <td className="p-2 font-mono">{entry.action}</td>
                      <td className="p-2 text-muted-foreground">{entry.resource_type}</td>
                      <td className="p-2">{entry.user_email || '—'}</td>
                      <td
                        className={cn(
                          'p-2',
                          entry.outcome === 'success' ? 'text-success' : 'text-destructive',
                        )}
                      >
                        {entry.outcome}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {!audit.length ? (
                <p className="p-3 text-sm text-muted-foreground">Записей нет.</p>
              ) : null}
            </ScrollArea>
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              В журнал попадает факт действия, но не его содержание: текст обращений и реквизиты
              документов могут содержать персональные данные.
            </p>
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}

interface StatCardProps {
  icon: typeof Database;
  label: string;
  value: string;
  hint?: string;
  tone?: 'default' | 'warning';
}

function StatCard({ icon: Icon, label, value, hint, tone = 'default' }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-3">
        <Icon
          className={cn(
            'mt-0.5 h-4 w-4 shrink-0',
            tone === 'warning' ? 'text-warning' : 'text-muted-foreground',
          )}
        />
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">{label}</p>
          <p className="text-lg font-medium tabular-nums">{value}</p>
          {hint ? <p className="truncate text-[11px] text-muted-foreground">{hint}</p> : null}
        </div>
      </CardContent>
    </Card>
  );
}

interface MetricProps {
  label: string;
  value: number | null;
  percent?: boolean;
}

function Metric({ label, value, percent }: MetricProps) {
  const text =
    value === null || value === undefined
      ? '—'
      : percent
        ? `${Math.round(value * 100)}%`
        : String(value);

  return (
    <div className="rounded border border-border p-1.5">
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="font-medium tabular-nums">{text}</p>
    </div>
  );
}

interface UploadFormProps {
  documents: KnowledgeDocument[];
  onUploaded: () => Promise<void> | void;
}

/** Загрузка новой редакции документа базы знаний. */
function UploadForm({ documents, onUploaded }: UploadFormProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [code, setCode] = useState('');
  const [label, setLabel] = useState('');
  const [changelog, setChangelog] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    setCode((current) => current || documents[0]?.code || '');
  }, [documents]);

  const submit = async (): Promise<void> => {
    const file = inputRef.current?.files?.[0];
    if (!file || !code) {
      setMessage('Выберите документ и файл редакции');
      return;
    }

    setBusy(true);
    setMessage('');
    try {
      const accepted = await admin.uploadVersion({
        file,
        documentCode: code,
        versionLabel: label,
        changelog,
        activate: true,
      });
      setMessage(accepted.message || 'Редакция загружена, идёт индексация');
      setLabel('');
      setChangelog('');
      if (inputRef.current) inputRef.current.value = '';
      await onUploaded();
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : 'Не удалось загрузить файл');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Upload className="h-4 w-4 text-primary" />
          Новая редакция документа
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="upload-code">Документ базы знаний</Label>
            <Select value={code} onValueChange={setCode}>
              <SelectTrigger id="upload-code">
                <SelectValue placeholder="Выберите документ" />
              </SelectTrigger>
              <SelectContent>
                {documents.map((document) => (
                  <SelectItem key={document.code} value={document.code}>
                    {document.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="upload-label">Метка редакции</Label>
            <Input
              id="upload-label"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="ред. от 15.10.2026"
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="upload-changelog">Что изменилось</Label>
          <Input
            id="upload-changelog"
            value={changelog}
            onChange={(event) => setChangelog(event.target.value)}
            placeholder="Уточнены сроки согласования в главе 10"
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="upload-file">Файл</Label>
          <Input id="upload-file" ref={inputRef} type="file" accept=".pdf,.docx,.txt,.md,.html" />
        </div>

        <Separator />

        <div className="flex items-center gap-2">
          <Button size="sm" className="gap-1.5" disabled={busy} onClick={submit}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileUp className="h-3.5 w-3.5" />}
            Загрузить и проиндексировать
          </Button>
          {message ? <p className="text-xs text-muted-foreground">{message}</p> : null}
        </div>

        <p className="text-[11px] text-muted-foreground">
          Прежние редакции сохраняются: к ним можно вернуться, а в ответах видно, какой
          редакцией руководствовался помощник.
        </p>
      </CardContent>
    </Card>
  );
}
