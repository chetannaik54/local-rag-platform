import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { AccountPage } from "../pages/AccountPage";
import { AdminPage } from "../pages/AdminPage";
import { LoginPage } from "../pages/LoginPage";
import { PlatformPage } from "../pages/PlatformPage";
import { SettingsPage } from "../pages/SettingsPage";
import { ApiError } from "../services/api";
import { json, meOf, mockApi, shellRoutes } from "./util";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

// ----------------------------------------------------------------------------------- login
describe("LoginPage", () => {
  const setup = (onLogin = vi.fn().mockResolvedValue(undefined), onKey = vi.fn().mockResolvedValue(undefined)) => {
    render(<LoginPage onLogin={onLogin} onLoginWithKey={onKey} />);
    return { onLogin, onKey };
  };
  const fill = async (email = "ada@acme.test", pw = "correct-horse-1") => {
    if (email) await userEvent.type(screen.getByLabelText("Email"), email);
    if (pw) await userEvent.type(screen.getByLabelText("Password"), pw);
  };

  it("keeps Sign in disabled until both fields are filled, then submits them", async () => {
    const { onLogin } = setup();
    const btn = screen.getByRole("button", { name: "Sign in" });
    expect(btn).toBeDisabled();
    await fill("ada@acme.test", "");
    expect(btn).toBeDisabled();
    await fill("", "correct-horse-1");
    expect(btn).toBeEnabled();
    await userEvent.click(btn);
    expect(onLogin).toHaveBeenCalledWith("ada@acme.test", "correct-horse-1");
  });

  it("submits with Enter and remembers the email (never the password)", async () => {
    const { onLogin } = setup();
    await fill();
    await userEvent.type(screen.getByLabelText("Password"), "{Enter}");
    await waitFor(() => expect(onLogin).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem("rag-last-email")).toBe("ada@acme.test");
    expect(JSON.stringify({ ...localStorage })).not.toContain("correct-horse-1");
  });

  it.each([
    ["invalid_credentials", 401, "Incorrect email or password."],
    ["rate_limited", 429, "Too many attempts"],
    ["org_disabled", 403, "organization has been disabled"],
    ["backend_unreachable", 0, "Cannot reach the backend"],
  ])("explains a '%s' failure in plain language and clears the password", async (code, status, text) => {
    const onLogin = vi.fn().mockRejectedValue(new ApiError(code, code === "backend_unreachable" ? "Cannot reach the backend." : "x", status));
    setup(onLogin);
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Email")).toHaveValue("ada@acme.test");  // do not make people retype it
  });

  it("can show and hide the password", async () => {
    setup();
    await fill();
    const pw = screen.getByLabelText("Password");
    expect(pw).toHaveAttribute("type", "password");
    await userEvent.click(screen.getByRole("button", { name: /Show/ }));
    expect(pw).toHaveAttribute("type", "text");
  });

  it("offers API-key sign-in as an alternative", async () => {
    const { onKey } = setup();
    await userEvent.click(screen.getByRole("button", { name: /API key instead/ }));
    await userEvent.type(screen.getByLabelText("API key"), "rag_secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onKey).toHaveBeenCalledWith("rag_secret");
  });

  it("disables the button while signing in so it cannot be double-submitted", async () => {
    let finish!: () => void;
    const onLogin = vi.fn(() => new Promise<void>((r) => { finish = r; }));
    setup(onLogin);
    await fill();
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
    finish();
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
  });
});

// ----------------------------------------------------------------------------------- navigation by permission
describe("App navigation follows permissions", () => {
  const visibleSections = async (role: string, extra = {}) => {
    mockApi(shellRoutes(meOf(role), extra));
    localStorage.setItem("rag-token", "ses_x");
    render(<App />);
    await screen.findByText("Local RAG");
    const nav = screen.getByRole("navigation", { name: "Sections" });
    return within(nav).getAllByRole("button").map((b) => b.textContent);
  };

  it("member: chat only", async () => { expect(await visibleSections("member")).toEqual(["Chat"]); });
  it("support agent: chat, console, settings", async () => {
    expect(await visibleSections("agent", { "GET /api/handoffs": () => json({ items: [] }), "GET /api/canned": () => json({ items: [] }) }))
      .toEqual(["Chat", "Console", "Settings"]);
  });
  it("admin: everything except the platform", async () => {
    expect(await visibleSections("admin")).toEqual(["Chat", "Console", "Admin", "Settings"]);
  });
  it("super admin: also the platform, and the organization switcher", async () => {
    const sections = await visibleSections("super_admin", {
      "GET /api/orgs": () => json({ items: [{ id: "org-acme", name: "Acme", slug: "acme", active: true, created_at: "", user_count: 1, document_count: 0 }] }),
    });
    expect(sections).toEqual(["Chat", "Console", "Admin", "Settings", "Platform"]);
    expect(await screen.findByLabelText("Organization")).toBeInTheDocument();
  });

  it("a role with only analytics.view gets an Admin area with just that tab", async () => {
    const me = meOf("custom", { permissions: ["chat.use", "analytics.view"] });
    mockApi(shellRoutes(me, {
      "GET /api/analytics": () => json({ days: 30, total_questions: 0, answered: 0, answer_rate: null, avg_latency_ms: null, handoffs: 0,
        thumbs_up: 0, thumbs_down: 0, per_agent: {}, tool_use: {}, per_day: [], unanswered: [], top_documents: [] }),
    }));
    localStorage.setItem("rag-token", "ses_x");
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Sections" });
    await userEvent.click(within(nav).getByRole("button", { name: "Admin" }));
    const tabs = within(await within(screen.getByRole("main")).findByRole("tablist")).getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(["Analytics"]);
  });

  it("does not ask for documents a role may not list", async () => {
    const { calls } = mockApi(shellRoutes(meOf("custom", { permissions: ["chat.use"] })));
    localStorage.setItem("rag-token", "ses_x");
    render(<App />);
    await screen.findByText("Local RAG");
    await userEvent.click(screen.getByRole("tab", { name: "Documents" }));
    expect(await screen.findByText(/cannot see the document list/)).toBeInTheDocument();
    expect(calls.some((c) => c.key === "GET /api/documents")).toBe(false);
  });

  it("shows the organization and signs out through the API", async () => {
    const { calls } = mockApi(shellRoutes(meOf("admin"), { "POST /api/auth/logout": () => new Response(null, { status: 204 }) }));
    localStorage.setItem("rag-token", "ses_x");
    render(<App />);
    expect(await screen.findByTitle("Your organization")).toHaveTextContent("Acme");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(calls.some((c) => c.key === "POST /api/auth/logout")).toBe(true);
    expect(localStorage.getItem("rag-token")).toBeNull();
  });

  it("platform admins switch organization: every later request carries X-Org-Id", async () => {
    let org = "org-default";
    const orgs = [
      { id: "org-default", name: "Default", slug: "default", active: true, created_at: "", user_count: 1, document_count: 0 },
      { id: "org-beta", name: "Beta", slug: "beta", active: true, created_at: "", user_count: 2, document_count: 3 },
    ];
    const { calls } = mockApi(shellRoutes(meOf("super_admin"), {
      "GET /api/auth/me": () => json(meOf("super_admin", { org_id: org, org_name: org === "org-beta" ? "Beta" : "Default" })),
      "GET /api/orgs": () => json({ items: orgs }),
    }));
    localStorage.setItem("rag-token", "ses_x");
    render(<App />);
    const select = await screen.findByLabelText("Organization");
    org = "org-beta";
    await userEvent.selectOptions(select, "org-beta");
    await waitFor(() => expect(localStorage.getItem("rag-org")).toBe("org-beta"));
    await waitFor(() => expect(calls.some((c) => c.key === "GET /api/documents" && c.headers["X-Org-Id"] === "org-beta")).toBe(true));
  });
});

// ----------------------------------------------------------------------------------- admin: users, roles, embed
const ROLES = [
  { id: "admin", name: "admin", builtin: true, permissions: ["chat.use", "documents.read", "documents.write", "users.manage", "roles.manage", "embed.manage"], user_count: 1 },
  { id: "member", name: "member", builtin: true, permissions: ["chat.use", "documents.read"], user_count: 2 },
  { id: "r-power", name: "Power", builtin: false, permissions: ["chat.use", "analytics.view"], user_count: 0 },
];
const USERS = [
  { id: "id-admin", name: "Ada Admin", email: "admin@acme.test", role: "admin", role_id: "admin", active: true, has_password: true, has_api_key: false, created_at: "" },
  { id: "u-bob", name: "bob", email: "bob@acme.test", role: "member", role_id: "member", active: true, has_password: true, has_api_key: true, created_at: "" },
];

describe("Admin > Users", () => {
  const open = async (extra = {}) => {
    const me = meOf("custom", { id: "id-admin", permissions: ["chat.use", "documents.read", "users.manage", "roles.manage", "embed.manage"] });
    const m = mockApi({ "GET /api/users": () => json({ items: USERS }), "GET /api/roles": () => json(ROLES), ...extra });
    render(<AdminPage docs={[]} me={me} />);
    await userEvent.click(await screen.findByRole("tab", { name: "Users" }));
    await screen.findByText("bob");
    return m;
  };

  it("creates a user with a password and a role", async () => {
    const { calls } = await open({ "POST /api/users": () => json({ ...USERS[1], id: "u-new", name: "eve", token: null }, 201) });
    await userEvent.type(screen.getByLabelText("Name"), "eve");
    await userEvent.type(screen.getByLabelText("Email"), "eve@acme.test");
    await userEvent.type(screen.getByLabelText("Initial password"), "a-long-password-1");
    await userEvent.click(screen.getByRole("button", { name: "Create user" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/users")?.body).toEqual({
      name: "eve", role: "member", email: "eve@acme.test", password: "a-long-password-1", api_key: false }));
  });

  it("shows a new API key exactly once, with a warning", async () => {
    await open({ "POST /api/users/u-bob/api-key": () => json({ ...USERS[1], token: "rag_brand_new_key" }) });
    const row = screen.getByText("bob").closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "New API key" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/will not be shown again.*rag_brand_new_key/);
  });

  it("protects your own account: no role change, disable or delete on yourself", async () => {
    await open();
    const self = screen.getByText("Ada Admin").closest("li")!;
    expect(within(self).getByLabelText("Role of Ada Admin")).toBeDisabled();
    expect(within(self).getByRole("button", { name: "Disable" })).toBeDisabled();
    expect(within(self).getByRole("button", { name: "Delete" })).toBeDisabled();
    const other = screen.getByText("bob").closest("li")!;
    expect(within(other).getByRole("button", { name: "Disable" })).toBeEnabled();
  });

  it("does not let you hand out roles that contain permissions you do not hold", async () => {
    await open();
    const role = screen.getByLabelText("Role") as HTMLSelectElement;
    const power = within(role).getByRole("option", { name: /Power/ }) as HTMLOptionElement;
    expect(power.disabled).toBe(true);          // needs analytics.view, which this admin lacks
    expect(power.textContent).toMatch(/cannot grant/);
    expect((within(role).getByRole("option", { name: /member/ }) as HTMLOptionElement).disabled).toBe(false);
  });

  it("disables and resets a password for someone else; shows server errors", async () => {
    const { calls } = await open({ "PUT /api/users/u-bob": () => json({ error: { code: "conflict", message: "That would leave the organization without anyone who can manage users." } }, 409) });
    const row = screen.getByText("bob").closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "Disable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/without anyone who can manage users/);
    expect(calls.find((c) => c.key === "PUT /api/users/u-bob")?.body).toEqual({ active: false });
    await userEvent.click(within(row).getByRole("button", { name: "Password…" }));
    await userEvent.type(screen.getByLabelText("New password for bob"), "another-pass-77");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    await waitFor(() => expect(calls.filter((c) => c.key === "PUT /api/users/u-bob").at(-1)?.body).toEqual({ password: "another-pass-77" }));
  });
});

describe("Admin > Roles", () => {
  const CATALOG = { permissions: { "chat.use": "Chat", "documents.write": "Upload documents", "analytics.view": "Analytics", "users.manage": "Users", "platform.manage": "Platform" }, assistant_choices: [] };

  it("builds a role from the permissions you hold; the rest are visibly unavailable", async () => {
    const me = meOf("custom", { permissions: ["chat.use", "documents.write", "roles.manage"] });
    const { calls } = mockApi({
      "GET /api/roles": () => json(ROLES), "GET /api/permissions": () => json(CATALOG),
      "POST /api/roles": () => json({ id: "r-new", name: "Uploader", builtin: false, permissions: ["chat.use", "documents.write"], user_count: 0 }, 201),
    });
    render(<AdminPage docs={[]} me={me} />);
    await screen.findByText("Power");
    await userEvent.click(screen.getByRole("button", { name: /New role/ }));
    expect(screen.queryByLabelText("platform.manage")).not.toBeInTheDocument();     // never offered
    expect(screen.getByLabelText("analytics.view")).toBeDisabled();                  // you do not hold it
    expect(screen.getByLabelText("users.manage")).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Role name"), "Uploader");
    await userEvent.click(screen.getByLabelText("documents.write"));
    await userEvent.click(screen.getByRole("button", { name: "Save role" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/roles")?.body).toEqual({ name: "Uploader", permissions: ["chat.use", "documents.write"] }));
  });

  it("built-in roles are read-only; custom ones can be edited or deleted", async () => {
    const me = meOf("custom", { permissions: ["chat.use", "roles.manage"] });
    mockApi({ "GET /api/roles": () => json(ROLES), "GET /api/permissions": () => json(CATALOG) });
    render(<AdminPage docs={[]} me={me} />);
    const builtin = (await screen.findByText("member", { selector: "b" })).closest("li")!;
    expect(within(builtin).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(within(builtin).getByText("built-in")).toBeInTheDocument();
    const custom = screen.getByText("Power").closest("li")!;
    expect(within(custom).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(custom).getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });
});

describe("Admin > Website embed", () => {
  const KEY = {
    id: "k1", name: "Company site", token: "pk_abc123", token_prefix: "pk_abc123", allowed_origins: ["https://www.acme.test"],
    agent_id: null, title: "Ask Acme", welcome: "Hi", color: "#4f46e5", active: true, created_at: "", last_used: null, wildcard: false,
    snippet: '<script src="https://api.acme.test/widget.js" data-key="pk_abc123" async></script>',
  };
  const open = async (keys = [KEY], extra = {}) => {
    const me = meOf("custom", { permissions: ["chat.use", "embed.manage"] });
    const m = mockApi({
      "GET /api/embed-keys": () => json({ items: keys }),
      "GET /api/agents": () => json({ items: [{ id: "general", name: "General assistant", required_permission: "chat.use" }, { id: "hr", name: "HR assistant", required_permission: "chat.use" }, { id: "management", name: "Mgmt", required_permission: "analytics.view" }], available_tools: [] }),
      ...extra,
    });
    render(<AdminPage docs={[]} me={me} />);
    return m;
  };

  it("shows the copy-paste snippet for existing keys", async () => {
    await open();
    const box = await screen.findByLabelText("Embed code for Company site");
    expect(box).toHaveValue(KEY.snippet);
    expect(box).toHaveAttribute("readonly");
    expect(screen.getByText(/Last used: never/)).toBeInTheDocument();
  });

  it("creates a key from a list of websites and only offers chat-safe assistants", async () => {
    const { calls } = await open([], { "POST /api/embed-keys": () => json(KEY, 201) });
    const assistant = await screen.findByLabelText("Assistant");
    expect(within(assistant).queryByRole("option", { name: "Mgmt" })).not.toBeInTheDocument();  // needs extra permissions
    await userEvent.type(screen.getByLabelText("Key name"), "Company site");
    const create = screen.getByRole("button", { name: "Create key" });
    expect(create).toBeDisabled();                                           // no websites yet
    await userEvent.type(screen.getByLabelText("Allowed websites"), "https://www.acme.test{Enter}https://*.acme.test, https://shop.acme.test");
    await userEvent.selectOptions(assistant, "hr");
    await userEvent.click(create);
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/embed-keys")?.body).toMatchObject({
      name: "Company site", agent_id: "hr", allowed_origins: ["https://www.acme.test", "https://*.acme.test", "https://shop.acme.test"] }));
  });

  it("warns loudly about a wildcard key and lets you disable or delete keys", async () => {
    const { calls } = await open([{ ...KEY, allowed_origins: ["*"], wildcard: true }], {
      "PUT /api/embed-keys/k1": () => json({ ...KEY, active: false }), "DELETE /api/embed-keys/k1": () => new Response(null, { status: 204 }) });
    expect(await screen.findByRole("alert")).toHaveTextContent(/accepts any website/);
    await userEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/embed-keys/k1")?.body).toEqual({ active: false }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(calls.some((c) => c.key === "DELETE /api/embed-keys/k1")).toBe(true));
  });

  it("shows the server's validation message for a bad origin", async () => {
    await open([], { "POST /api/embed-keys": () => json({ error: { code: "validation_error", message: "'example.com' is not a valid origin." } }, 422) });
    await userEvent.type(await screen.findByLabelText("Key name"), "x");
    await userEvent.type(screen.getByLabelText("Allowed websites"), "example.com");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a valid origin/);
  });
});

// ----------------------------------------------------------------------------------- platform / account / settings
describe("PlatformPage", () => {
  const ORGS = [{ id: "org-default", name: "Default", slug: "default", active: true, created_at: "", user_count: 1, document_count: 2 }];
  const SETTINGS = { ollama_url: "http://localhost:11434", chat_model: "llama3.1:8b", embedding_model: "nomic-embed-text" };

  it("creates an organization with its first admin, suggesting the slug from the name", async () => {
    const { calls } = mockApi({
      "GET /api/orgs": () => json({ items: ORGS }), "GET /api/platform/settings": () => json(SETTINGS),
      "POST /api/orgs": () => json({ ...ORGS[0], id: "o2", name: "Acme Corp", slug: "acme-corp" }, 201),
    });
    render(<PlatformPage currentOrgId="org-default" onSwitchOrg={vi.fn()} onOrgsChanged={vi.fn()} />);
    await screen.findByText("Default");
    await userEvent.type(screen.getByLabelText("Organization name"), "Acme Corp");
    expect(screen.getByLabelText("Slug")).toHaveValue("acme-corp");
    await userEvent.type(screen.getByLabelText("Admin email"), "boss@acme.test");
    await userEvent.type(screen.getByLabelText("Admin password"), "a-long-password-1");
    await userEvent.click(screen.getByRole("button", { name: "Create organization" }));
    await waitFor(() => expect(calls.find((c) => c.key === "POST /api/orgs")?.body).toEqual({
      name: "Acme Corp", slug: "acme-corp", admin_name: "admin", admin_email: "boss@acme.test", admin_password: "a-long-password-1" }));
  });

  it("asks before disabling an organization and can open one for support", async () => {
    const onSwitch = vi.fn();
    const { calls } = mockApi({
      "GET /api/orgs": () => json({ items: [...ORGS, { ...ORGS[0], id: "o2", name: "Beta", slug: "beta" }] }),
      "GET /api/platform/settings": () => json(SETTINGS), "PUT /api/orgs/o2": () => json({ ...ORGS[0], id: "o2", active: false }),
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<PlatformPage currentOrgId="org-default" onSwitchOrg={onSwitch} onOrgsChanged={vi.fn()} />);
    const beta = (await screen.findByText("Beta")).closest("li")!;
    await userEvent.click(within(beta).getByRole("button", { name: "Disable" }));
    expect(calls.some((c) => c.key === "PUT /api/orgs/o2")).toBe(false);          // declined the confirmation
    await userEvent.click(within(beta).getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/orgs/o2")?.body).toEqual({ active: false }));
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    await userEvent.click(within(beta).getByRole("button", { name: "Open" }));
    expect(onSwitch).toHaveBeenCalledWith("o2");
    expect(within(screen.getByText("Default").closest("li")!).getByRole("button", { name: "Open" })).toBeDisabled();  // already there
  });

  it("saves the platform-wide Ollama settings", async () => {
    const { calls } = mockApi({ "GET /api/orgs": () => json({ items: ORGS }), "GET /api/platform/settings": () => json(SETTINGS),
      "PUT /api/platform/settings": () => json({ ...SETTINGS, ollama_url: "http://10.0.0.5:11434" }) });
    render(<PlatformPage currentOrgId="org-default" onSwitchOrg={vi.fn()} onOrgsChanged={vi.fn()} />);
    const url = await screen.findByLabelText("Ollama server URL");
    await userEvent.clear(url);
    await userEvent.type(url, "http://10.0.0.5:11434");
    await userEvent.click(screen.getByRole("button", { name: "Save platform settings" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.key === "PUT /api/platform/settings")?.body.ollama_url).toBe("http://10.0.0.5:11434");
  });
});

describe("AccountPage", () => {
  it("validates the repeat password and changes it", async () => {
    const { calls } = mockApi({ "POST /api/auth/password": () => new Response(null, { status: 204 }) });
    render(<AccountPage me={meOf("member")} onSignOut={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Current password"), "old-password-1");
    await userEvent.type(screen.getByLabelText("New password"), "brand-new-pass-9");
    await userEvent.type(screen.getByLabelText("Repeat new password"), "brand-new-pass-");
    expect(screen.getByRole("alert")).toHaveTextContent(/do not match/);
    expect(screen.getByRole("button", { name: "Change password" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Repeat new password"), "9");
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Password changed.")).toBeInTheDocument();
    expect(calls.find((c) => c.key === "POST /api/auth/password")?.body).toEqual({ current_password: "old-password-1", new_password: "brand-new-pass-9" });
    expect(screen.getByLabelText("Current password")).toHaveValue("");
  });

  it("shows the server's reason when the change is refused", async () => {
    mockApi({ "POST /api/auth/password": () => json({ error: { code: "weak_password", message: "Password must be at least 10 characters." } }, 422) });
    render(<AccountPage me={meOf("member")} onSignOut={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Current password"), "old-password-1");
    await userEvent.type(screen.getByLabelText("New password"), "short");
    await userEvent.type(screen.getByLabelText("Repeat new password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/at least 10 characters/);
  });

  it("lists what the role can do and hides password change for API-key and widget identities", () => {
    const { rerender } = render(<AccountPage me={meOf("member")} onSignOut={vi.fn()} />);
    expect(screen.getByText(/What can I do/)).toBeInTheDocument();
    rerender(<AccountPage me={meOf("member", { kind: "local", auth_enabled: false })} onSignOut={vi.fn()} />);
    expect(screen.queryByRole("form", { name: "Change password" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });
});

describe("SettingsPage for tenants", () => {
  const SETTINGS = { settings: { ollama_url: "http://managed-by-platform.invalid", chat_model: "m", embedding_model: "e", chunk_size: 1000, chunk_overlap: 150, top_k: 5,
    similarity_threshold: 0.35, temperature: 0.2, num_ctx: 8192, max_tokens: 1024, response_language: "auto" },
    validation: { ollama_reachable: true, chat_model_available: true, embedding_model_available: true, messages: [] }, reindex_required: false, stale_documents: 0 };

  it("an organization admin cannot edit the shared Ollama server", async () => {
    mockApi({ "GET /api/settings": () => json(SETTINGS), "GET /api/models": () => json({ models: [] }) });
    render(<SettingsPage onSaved={vi.fn()} canEdit canEditServer={false} />);
    const url = await screen.findByLabelText(/Ollama server URL/);
    expect(url).toBeDisabled();
    expect(url).toHaveValue("Managed by the platform administrator");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
  });

  it("read-only roles can look but not save", async () => {
    mockApi({ "GET /api/settings": () => json(SETTINGS), "GET /api/models": () => json({ models: [] }) });
    render(<SettingsPage onSaved={vi.fn()} canEdit={false} canEditServer={false} />);
    expect(await screen.findByRole("button", { name: "Save settings" })).toBeDisabled();
    expect(screen.getByLabelText(/Top-k retrieved chunks/)).toBeDisabled();
  });
});
