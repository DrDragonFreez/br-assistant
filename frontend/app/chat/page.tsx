import { AppHeader } from '@/components/app-header';
import { AuthGate } from '@/components/auth-gate';
import { ChatWorkspace } from '@/components/chat/ChatWorkspace';

export const metadata = { title: 'Диалог' };

/** Новое обращение: диалог создаётся бэкендом при первом сообщении. */
export default function ChatPage() {
  return (
    <AuthGate>
      <div className="flex h-dvh flex-col">
        <AppHeader />
        <ChatWorkspace />
      </div>
    </AuthGate>
  );
}
