export const AUTH_TOKEN_KEY = "surogates_auth_token";
export const AUTH_REFRESH_TOKEN_KEY = "surogates_auth_refresh_token";

const POST_AUTH_REDIRECT_KEY = "surogates_post_auth_redirect";

function canUseStorage(): boolean {
  return typeof window !== "undefined";
}

export function hasAuthToken(): boolean {
  if (!canUseStorage()) return false;
  return Boolean(localStorage.getItem(AUTH_TOKEN_KEY));
}

export function hasRefreshToken(): boolean {
  if (!canUseStorage()) return false;
  return Boolean(localStorage.getItem(AUTH_REFRESH_TOKEN_KEY));
}

export function getAuthToken(): string | null {
  if (!canUseStorage()) return null;
  return localStorage.getItem(AUTH_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (!canUseStorage()) return null;
  return localStorage.getItem(AUTH_REFRESH_TOKEN_KEY);
}

export function storeAuthTokens(
  accessToken: string,
  refreshToken: string,
): void {
  if (!canUseStorage()) return;
  localStorage.setItem(AUTH_TOKEN_KEY, accessToken);
  localStorage.setItem(AUTH_REFRESH_TOKEN_KEY, refreshToken);
}

export function clearAuthTokens(): void {
  if (!canUseStorage()) return;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_REFRESH_TOKEN_KEY);
}

export function setPostAuthRedirect(url: string): void {
  if (!canUseStorage()) return;
  sessionStorage.setItem(POST_AUTH_REDIRECT_KEY, url);
}

export function getPostAuthRoute(): string {
  if (!canUseStorage()) return "/login";
  if (!hasAuthToken()) return "/login";
  const saved = sessionStorage.getItem(POST_AUTH_REDIRECT_KEY);
  if (saved) {
    sessionStorage.removeItem(POST_AUTH_REDIRECT_KEY);
    return saved;
  }
  return "/chat";
}
