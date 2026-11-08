import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import MemoryVizPage from './features/memory-viz/MemoryVizPage';

const qc = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <MemoryVizPage />
    </QueryClientProvider>
  );
}
