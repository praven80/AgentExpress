/** Login, one strategy per provider.
 *
 *  `auth-config.js` is rendered at DEPLOY time by the IaC from workflow.json + the
 *  `idp` variable, and sets window.AUTH_CONFIG. So the same built bundle serves
 *  Cognito, Auth0 and no-login — which is the point: adding a provider is one entry
 *  here plus one branch in terraform/identity.tf, and no rebuild of the UI.
 *
 *  Ported from the pre-Cloudscape page unchanged in behaviour. The one thing worth
 *  keeping in mind: initAuth() REDIRECTS for the code flow, so anything after it in
 *  the boot sequence may not run. The caller must stop when it returns false. */

import { setToken } from "./api";

export interface AuthConfig {
  enabled: boolean;
  provider?: "cognito" | "auth0" | "none";
  /** Cognito */
  domain?: string;
  clientId?: string;
  region?: string;
  /** Auth0 */
  auth0Domain?: string;
  auth0ClientId?: string;
}

declare global {
  interface Window {
    AUTH_CONFIG?: AuthConfig;
    /** Set by the legacy observability island so it can reuse the same token. */
    __idToken?: string | null;
  }
}

export interface Claims {
  email?: string;
  name?: string;
  sub?: string;
  "cognito:username"?: string;
  "cognito:groups"?: string[] | string;
  [k: string]: unknown;
}

export function parseJwt(token: string): Claims {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(escape(atob(part)))) as Claims;
  } catch {
    return {};
  }
}

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}

interface Strategy {
  /** Returns a token, or null when the caller should redirect to login. */
  init(cfg: AuthConfig): Promise<string | null>;
  login(cfg: AuthConfig): void;
  logout(cfg: AuthConfig): void;
}

const NONE: Strategy = {
  // idp = "none": there is no token and nothing to redirect to.
  init: async () => "",
  login: () => {},
  logout: () => {},
};

/** Cognito Hosted UI, authorization-code flow with the token in the URL fragment. */
const COGNITO: Strategy = {
  async init() {
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    const fromHash = hash.get("id_token");
    if (fromHash) {
      sessionStorage.setItem("idToken", fromHash);
      history.replaceState({}, "", location.pathname);
      return fromHash;
    }
    const stored = sessionStorage.getItem("idToken");
    if (stored && !expired(stored)) return stored;
    return null;
  },
  login(cfg) {
    const url = new URL(`https://${cfg.domain}/oauth2/authorize`);
    url.searchParams.set("client_id", cfg.clientId || "");
    url.searchParams.set("response_type", "token");
    url.searchParams.set("scope", "openid email profile");
    url.searchParams.set("redirect_uri", location.origin + "/");
    location.assign(url.toString());
  },
  logout(cfg) {
    sessionStorage.removeItem("idToken");
    const url = new URL(`https://${cfg.domain}/logout`);
    url.searchParams.set("client_id", cfg.clientId || "");
    url.searchParams.set("logout_uri", location.origin + "/");
    location.assign(url.toString());
  },
};

/** Auth0 via auth0-spa-js, loaded on demand so a Cognito deployment never fetches it. */
interface Auth0Client {
  handleRedirectCallback(): Promise<unknown>;
  getIdTokenClaims(): Promise<{ __raw?: string } | undefined>;
  loginWithRedirect(): Promise<void>;
  logout(o: unknown): void;
}
let auth0: Auth0Client | null = null;

const AUTH0: Strategy = {
  async init(cfg) {
    await loadScript("https://cdn.auth0.com/js/auth0-spa-js/2.1/auth0-spa-js.production.js");
    const factory = (window as unknown as {
      auth0: { createAuth0Client(o: unknown): Promise<Auth0Client> };
    }).auth0;
    auth0 = await factory.createAuth0Client({
      domain: cfg.auth0Domain,
      clientId: cfg.auth0ClientId,
      authorizationParams: { redirect_uri: location.origin + "/" },
      cacheLocation: "localstorage",
    });
    if (location.search.includes("code=")) {
      await auth0.handleRedirectCallback();
      history.replaceState({}, "", location.pathname);
    }
    const claims = await auth0.getIdTokenClaims();
    return claims?.__raw ?? null;
  },
  login() {
    void auth0?.loginWithRedirect();
  },
  logout() {
    auth0?.logout({ logoutParams: { returnTo: location.origin + "/" } });
  },
};

const STRATEGIES: Record<string, Strategy> = { none: NONE, cognito: COGNITO, auth0: AUTH0 };

function expired(token: string): boolean {
  const { exp } = parseJwt(token) as { exp?: number };
  return typeof exp === "number" && exp * 1000 < Date.now() + 30_000;
}

export function authConfig(): AuthConfig {
  return window.AUTH_CONFIG ?? { enabled: false, provider: "none" };
}

function strategy(): Strategy {
  const cfg = authConfig();
  if (!cfg.enabled) return NONE;
  return STRATEGIES[cfg.provider ?? "cognito"] ?? COGNITO;
}

export interface AuthState {
  ready: boolean;
  user: string;
}

/** Resolve a token, or redirect to the provider's login page.
 *
 *  Returns false when a redirect is under way, and the caller MUST stop booting.
 *  The pre-Cloudscape page did not, so `loadWorkflow`/`loadMe` fired without a token
 *  and every first visit logged two 401s on the way to the login screen. */
export async function initAuth(): Promise<AuthState | null> {
  const cfg = authConfig();
  if (!cfg.enabled) {
    setToken(null);
    return { ready: true, user: "" };
  }
  const s = strategy();
  let token: string | null = null;
  try {
    token = await s.init(cfg);
  } catch (e) {
    console.error("auth init failed", e);
  }
  if (!token) {
    s.login(cfg);
    return null;
  }
  setToken(token);
  window.__idToken = token;
  const c = parseJwt(token);
  return {
    ready: true,
    user: c.email || c.name || c["cognito:username"] || c.sub || "",
  };
}

export function logout(): void {
  strategy().logout(authConfig());
}

export function authEnabled(): boolean {
  return authConfig().enabled;
}
