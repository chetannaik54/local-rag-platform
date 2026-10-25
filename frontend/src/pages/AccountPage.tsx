import { useState } from "react";
import { api } from "../services/api";
import type { Me } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function AccountPage({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const mismatch = again.length > 0 && next !== again;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null); setDone(false);
    try {
      await api.changePassword(current, next);
      setCurrent(""); setNext(""); setAgain(""); setDone(true);
    } catch (err) { setError(errMsg(err)); } finally { setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-lg space-y-6 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Your account</h1>
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-slate-500">Name</dt><dd>{me.name}</dd>
        <dt className="text-slate-500">Email</dt><dd>{me.email ?? "none"}</dd>
        <dt className="text-slate-500">Organization</dt><dd>{me.org_name}</dd>
        <dt className="text-slate-500">Role</dt><dd>{me.role}</dd>
      </dl>
      <details className="text-sm">
        <summary className="cursor-pointer text-slate-500">What can I do? ({me.permissions.length} permissions)</summary>
        <ul className="mt-2 list-disc pl-5 text-xs text-slate-600 dark:text-slate-400">{me.permissions.map((p) => <li key={p}><code>{p}</code></li>)}</ul>
      </details>

      {me.kind === "user" && (
        <form onSubmit={submit} className="space-y-3 text-sm" aria-label="Change password">
          <h2 className="font-semibold">Change password</h2>
          <label className="block"><span>Current password</span>
            <input type="password" autoComplete="current-password" className="input mt-1" value={current} onChange={(e) => setCurrent(e.target.value)} /></label>
          <div>
            <label className="block"><span>New password</span>
              <input type="password" autoComplete="new-password" className="input mt-1" aria-describedby="pw-hint" value={next} onChange={(e) => setNext(e.target.value)} /></label>
            <span id="pw-hint" className="text-xs text-slate-500">At least 10 characters. You will stay signed in here and be signed out on other devices.</span>
          </div>
          <label className="block"><span>Repeat new password</span>
            <input type="password" autoComplete="new-password" className="input mt-1" value={again} onChange={(e) => setAgain(e.target.value)} aria-invalid={mismatch} /></label>
          {mismatch && <p role="alert" className="text-xs text-red-600">The passwords do not match.</p>}
          {error && <p role="alert" className="rounded-lg bg-red-50 p-2 text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>}
          {done && <p role="status" className="text-emerald-600">Password changed.</p>}
          <button className="btn-primary" disabled={busy || !current || !next || next !== again}>{busy ? "Saving…" : "Change password"}</button>
        </form>
      )}

      {me.auth_enabled && <button className="btn-ghost border border-slate-300 dark:border-slate-700" onClick={onSignOut}>Sign out</button>}
    </div>
  );
}
