import { AppHeader } from '@/components/app-header';
import { AuthGate } from '@/components/auth-gate';
import { ChatWorkspace } from '@/components/chat/ChatWorkspace';

export const metadata = { title: 'Диалог' };

interface ChatSessionPageProps {
  params: { id: string };
}

/** Ранее начатый диалог: история подгружается по идентификатору. */
export default function ChatSessionPage({ params }: ChatSessionPageProps) {
  return (
    <AuthGate>
      <div className="flex h-dvh flex-col">
        <AppHeader />
        <ChatWorkspace sessionId={params.id} />
      </div>
    </AuthGate>
  );
}
