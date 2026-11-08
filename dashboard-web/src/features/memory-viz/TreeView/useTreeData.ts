import { useQuery } from '@tanstack/react-query';
import { fetchTree } from '../../../api/memoryViz';

export function useTreeData(scope: string | null) {
  return useQuery({
    queryKey: ['memory-tree', scope],
    queryFn: () => fetchTree(scope as string),
    enabled: !!scope,
    staleTime: 15_000,
  });
}
