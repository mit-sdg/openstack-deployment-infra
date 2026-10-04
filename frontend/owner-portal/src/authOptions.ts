// Anonymous sign-in options. Both the sign-in page and the shell read this one
// query so the browser never requests two binder cookies at once.
export type AuthOptions = { csrfToken: string; providerLabel: string; platformName?: string };

export const authOptionsQuery = {
  queryKey: ['auth-options'],
  queryFn: async (): Promise<AuthOptions> => {
    const response = await fetch('/auth/options', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('Sign-in is unavailable right now. Try again in a minute.');
    const value = await response.json();
    if (typeof value.data?.csrfToken !== 'string' || typeof value.data?.providerLabel !== 'string')
      throw new Error('Sign-in is unavailable right now. Try again in a minute.');
    return {
      csrfToken: value.data.csrfToken,
      providerLabel: value.data.providerLabel,
      platformName:
        typeof value.data.platformName === 'string' && value.data.platformName
          ? value.data.platformName
          : undefined,
    };
  },
} as const;
