import { ThemeToggle } from '@openstack-platform/ui';

export const themeStorageKey = 'owner-portal-theme';

export function ThemeButton() {
  return <ThemeToggle storageKey={themeStorageKey} />;
}
