import { useQuery } from '@tanstack/react-query';
import { fetchStarfield } from '../../../api/memoryViz';

export function useStarfieldData(scope: string | null) {
  return useQuery({
    queryKey: ['memory-starfield', scope],
    queryFn: () => fetchStarfield(scope as string),
    enabled: !!scope,
    staleTime: 30_000,
    // 202 表示后端正在算坐标：3 秒后重试，最多 20 次
    retry: (count, err) => count < 20 && !!err,
    retryDelay: 3000,
  });
}
