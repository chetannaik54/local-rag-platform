import { useState } from "react";
import { ApiError } from "../services/api";

interface Props {
  onLogin: (email: string, password: string) => Promise<void>;
  onLoginWithKey?: (key: string) => Promise<void>;
}

const EMAIL_KEY = "rag-last-email";

function friendly(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "rate_limited") return "Too many attempts. Please wait a few minutes and try again.";
    if (e.code === "invalid_credentials" || e.code === "unauthorized") return "Incorrect email or password.";
    if (e.code === "org_disabled") return "Your organization has been disabled. Contact your administrator.";
    if (e.code === "backend_unreachable") return e.message;
    return e.message;
  }
  return "Something went wrong. Please try again.";
}

export function LoginPage({ onLogin, onLoginWithKey }: Props) {
  const [email, setEmail] = useState(() => {
    try { return localStorage.getItem(EMAIL_KEY) ?? ""; } catch { return ""; }
  });
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useKey, setUseKey] = useState(false);
  const [key, setKey] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (useKey) {
        await onLoginWithKey?.(key);
      } else {
        await onLogin(email, password);
        try { localStorage.setItem(EMAIL_KEY, email.trim()); } catch { /* private mode */ }
      }
    } catch (err) {
      setError(friendly(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  const ready = useKey ? key.trim().length > 0 : email.trim().length > 0 && password.length > 0;

  return (
    <div className="flex h-full items-center justify-center bg-slate-50 p-4 dark:bg-slate-950">
      <form
        onSubmit={submit} aria-labelledby="login-title" noValidate
        className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
      >
        <div>
          <h1 id="login-title" className="text-xl font-semibold">Sign in</h1>
          <p className="mt-1 text-sm text-slate-500">Use the account your administrator created for you.</p>
        </div>

        {useKey ? (
          <label className="block text-sm">
            <span className="font-medium">API key</span>
            <input
              type="password" autoComplete="off" className="input mt-1" value={key} autoFocus
              onChange={(e) => setKey(e.target.value)} placeholder="rag_…"
            />
          </label>
        ) : (
          <>
            <label className="block text-sm">
              <span className="font-medium">Email</span>
              <input
                type="email" autoComplete="username" className="input mt-1" value={email}
                autoFocus={!email} onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="block text-sm">
              <span className="font-medium">Password</span>
              <span className="relative mt-1 block">
                <input
                  type={show ? "text" : "password"} autoComplete="current-password" className="input pr-16"
                  value={password} autoFocus={!!email} onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button" onClick={() => setShow(!show)} aria-pressed={show}
                  className="absolute inset-y-0 right-2 text-xs text-slate-500 hover:underline"
                >{show ? "Hide" : "Show"}<span className="sr-only"> password</span></button>
              </span>
            </label>
          </>
        )}

        {error && <p role="alert" className="rounded-lg bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>}

        <button className="btn-primary w-full" disabled={!ready || busy}>{busy ? "Signing in…" : "Sign in"}</button>

        {onLoginWithKey && (
          <button
            type="button" className="w-full text-center text-xs text-slate-500 hover:underline"
            onClick={() => { setUseKey(!useKey); setError(null); }}
          >{useKey ? "Use email and password instead" : "Sign in with an API key instead"}</button>
        )}
      </form>
    </div>
  );
}
