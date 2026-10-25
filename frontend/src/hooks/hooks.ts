import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, UNAUTHORIZED_EVENT, api, auth, orgContext, uploadFiles } from "../services/api";
import type { Doc, Health, Me, Session, UploadResult } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useTheme() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const toggle = useCallback(() => {
    setDark((d) => {
      const next = !d;
      document.documentElement.classList.toggle("dark", next);
      try { localStorage.setItem("theme", next ? "dark" : "light"); } catch { /* private mode */ }
      return next;
    });
  }, []);
  return { dark, toggle };
}

export function useHealth(intervalMs = 15000) {
  const [health, setHealth] = useState<Health | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      setHealth(await api.health());
      setBackendError(null);
    } catch (e) {
      setHealth(null);
      setBackendError(errMsg(e));
    }
  }, []);
  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);
  return { health, backendError, refresh };
}

export function useDocuments(enabled = true) {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);

  const refresh = useCallback(async () => {
    if (!enabled) { setLoading(false); return; }  // this role cannot list documents; don't ask
    try {
      setDocs(await api.documents());
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => { void refresh(); }, [refresh]);

  const processing = docs.some((d) => d.status === "processing");
  useEffect(() => {
    if (!processing) return;
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [processing, refresh]);

  const upload = useCallback(async (files: File[]) => {
    if (!files.length) return;
    setProgress(0);
    setResults([]);
    try {
      setResults(await uploadFiles(files, setProgress));
    } catch (e) {
      setResults(files.map((f) => ({ filename: f.name, document: null, duplicate: false, error: errMsg(e) })));
    } finally {
      setProgress(null);
      await refresh();
    }
  }, [refresh]);

  const remove = useCallback(async (id: string) => {
    try {
      await api.deleteDocument(id);
      setDocs((d) => d.filter((x) => x.id !== id));
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  return { docs, loading, error, progress, results, upload, remove, refresh };
}

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      setSessions(await api.sessions());
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const remove = useCallback(async (id: string) => {
    await api.deleteSession(id);
    await refresh();
  }, [refresh]);
  return { sessions, error, refresh, remove };
}

export function useAuth() {
  const [me, setMe] = useState<Me | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const check = useCallback(async () => {
    try {
      setMe(await api.me());
      setNeedsLogin(false);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setNeedsLogin(true);
      else setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void check(); }, [check]);

  // Any API call that comes back 401 (token revoked/expired) sends the user back to sign-in.
  useEffect(() => {
    const onUnauthorized = () => { setMe(null); setNeedsLogin(true); setLoading(false); };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  /** Email + password. Throws ApiError (wrong password, locked out, organization disabled...). */
  const login = useCallback(async (email: string, password: string) => {
    const r = await api.login(email.trim(), password);
    auth.set(r.token);
    orgContext.set("");
    setMe(r.me);
    setNeedsLogin(false);
    setError(null);
  }, []);

  /** Sign in with an API key instead of a password. */
  const loginWithKey = useCallback(async (key: string) => {
    auth.set(key.trim());
    try {
      setMe(await api.me());
      setNeedsLogin(false);
    } catch (e) {
      auth.set("");
      throw e;
    }
  }, []);

  const logout = useCallback(async () => {
    try { await api.logout(); } catch { /* the token may already be gone */ }
    auth.set("");
    orgContext.set("");
    setMe(null);
    setNeedsLogin(true);
  }, []);

  /** Platform admins: work inside another organization ("" returns to their own). */
  const switchOrg = useCallback(async (orgId: string) => {
    orgContext.set(orgId);
    await check();
  }, [check]);

  const can = useCallback((perm: string) => !!me?.permissions.includes(perm), [me]);
  return { me, needsLogin, error, loading, login, loginWithKey, logout, switchOrg, can, retry: check };
}

export function useAgents() {
  const [agents, setAgents] = useState<import("../types").Agent[]>([]);
  const [tools, setTools] = useState<import("../types").ToolInfo[]>([]);
  const refresh = useCallback(async () => {
    try {
      const r = await api.agents();
      setAgents(r.items);
      setTools(r.available_tools);
    } catch { /* leave the previous list in place */ }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  return { agents, tools, refresh };
}

/** Speech: recording is sent to the local Whisper endpoint; playback uses the OS voices in the browser. */
export function useVoice() {
  const [sttAvailable, setSttAvailable] = useState(false);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);

  useEffect(() => {
    api.voiceStatus().then((s) => setSttAvailable(s.stt_available)).catch(() => setSttAvailable(false));
  }, []);

  const canRecord = sttAvailable && typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
  const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;

  const startRecording = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks: Blob[] = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => chunks.push(e.data);
      recorder.current = rec;
      rec.start();
      setRecording(true);
      return new Promise<string>((resolve) => {
        rec.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          setRecording(false);
          setTranscribing(true);
          try {
            resolve(await api.transcribe(new Blob(chunks, { type: rec.mimeType || "audio/webm" })));
          } catch (e) {
            setError(errMsg(e));
            resolve("");
          } finally {
            setTranscribing(false);
          }
        };
      });
    } catch {
      setError("Microphone access was denied or is unavailable.");
      return "";
    }
  }, []);

  const stopRecording = useCallback(() => recorder.current?.stop(), []);

  const speak = useCallback((text: string) => {
    if (!canSpeak) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text.replace(/\[\d+(?:\s*,\s*\d+)*\]/g, ""));
    u.onend = () => setSpeaking(false);
    u.onerror = () => setSpeaking(false);
    setSpeaking(true);
    window.speechSynthesis.speak(u);
  }, [canSpeak]);
  const stopSpeaking = useCallback(() => { window.speechSynthesis?.cancel(); setSpeaking(false); }, []);

  return { canRecord, canSpeak, recording, transcribing, speaking, error, startRecording, stopRecording, speak, stopSpeaking };
}

