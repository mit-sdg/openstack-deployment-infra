import { useOwner } from './common';

/** An owner's name for a filter: from the listed rows, else one owner read. */
export function useOwnerName(
  id: string | undefined,
  rows: { ownerDisplayName: string }[] | undefined,
) {
  const lookup = useOwner(id, !!rows && !rows.length);
  return rows?.[0]?.ownerDisplayName ?? lookup.data?.displayName;
}
