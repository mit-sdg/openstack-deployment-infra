import { api } from '../api';
import { AppFrame } from '../components/AppFrame';
import { LogViewer } from '../components/LogViewer';

export function LogsPage({ id }: { id: string }) {
  return (
    <AppFrame id={id} active="Logs">
      <LogViewer
        queryKey={['logs', id]}
        read={(stream) => api.logs(id, stream)}
        idle="Logs appear while your app is running. Deploy it, or start it if it’s stopped."
      />
    </AppFrame>
  );
}
