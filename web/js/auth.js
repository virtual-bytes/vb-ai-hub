// ---------- sign-in
let signingIn = null, me = null;
function signIn() { return signingIn ||= runSignIn().finally(() => { signingIn = null; }); }
async function runSignIn() {
  let st = await fetch("/api/auth/status", { headers: authHeaders() }).then(r => r.status === 401 ? { badToken: true } : r.json()).catch(() => null);
  if (st?.badToken) {  // a wrong saved token is refused outright, so drop it and sign in normally
    store("kb.token"); toast("The saved access token was wrong, so the hub removed it");
    st = await fetch("/api/auth/status").then(r => r.json()).catch(() => null);
  }
  if (!st) return toast("Can't reach the hub");
  if (st.user) { me = st.user; return; }
  let error = "";
  for (;;) {
    const setup = st.setup_needed;
    const fields = setup
      ? [{ name: "username", label: "Admin username", required: true }, { name: "password", label: "Password (10 characters or more)", type: "password", required: true },
         ...(st.setup_token_required ? [{ name: "token", label: "Access token (HUB_TOKEN on the server)", type: "password", required: true }] : [])]
      : [{ name: "username", label: "Username", required: true }, { name: "password", label: "Password", type: "password", required: true }];
    const r = await dialog({ title: setup ? "Create the admin account" : "Sign in to VB AI Hub", fields, ok: setup ? "Create account" : "Sign in", cancel: "Use an access token",
      message: error ? `<span style="color:var(--err)">${esc(error)}</span>` : setup ? "This hub has no accounts yet." : "" });
    if (!r) { openSettings("accounts"); return; }
    const res = await fetch(setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(r) });
    if (res.ok) { await afterSignIn(); return; }
    error = (await res.json().catch(() => ({}))).error || res.statusText;
  }
}
async function afterSignIn() {
  me = (await fetch("/api/auth/status", { headers: authHeaders() }).then(r => r.json())).user;
  connect(); await refresh(); loadUsage();
}
function renderAccount() {
  const via = me?.via, line = $("#accountLine");
  line.textContent = !me ? "Not signed in" : via === "session" ? `Signed in as ${me.name} (${me.role})` : via === "sso" ? `Signed in as ${me.name} through single sign-on` : via === "token" ? "Using the access token" : "Local install, no accounts";
  $("#signOut").hidden = $("#changePw").hidden = via !== "session";
  $("#manageUsers").hidden = me?.role !== "admin";
}
async function manageUsers() {
  for (;;) {
    let users; try { users = (await api("/api/users")).users; } catch (e) { return toast(e.message); }
    const rows = users.map(u => `<tr><td>${esc(u.username)}</td><td>${esc(u.role)}</td><td>${u.last_login ? ago(u.last_login) : "never"}</td><td class="num"><button class="btn small danger" type="button" data-deluser="${esc(u.id)}" data-name="${esc(u.username)}">Remove</button></td></tr>`).join("");
    const pending = dialog({ title: "Users", ok: "Add user", cancel: "Close",
      message: `<div class="t-wrap" style="margin-bottom:6px"><table class="list"><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="4">No accounts yet.</td></tr>`}</tbody></table></div>`,
      fields: [{ name: "username", label: "New username" }, { name: "password", label: "Password (10 characters or more)", type: "password" },
               { name: "role", label: "Role", type: "select", options: [["member", "Member"], ["admin", "Admin"]], value: "member" }] });
    let removed = null;
    $$("[data-deluser]").forEach(b => b.onclick = () => { removed = b.dataset; $("#uiDlg").close("cancel"); });
    const r = await pending;
    if (removed) {
      if (await confirmUI("Remove user?", `Remove <b>${esc(removed.name)}</b>? They're signed out straight away.`, "Remove", true))
        await api(`/api/users/${enc(removed.deluser)}`, { method: "DELETE" }).catch(e => toast(e.message));
      continue;
    }
    if (!r) return;
    if (!r.username || !r.password) { toast("Enter a username and password"); continue; }
    await post("/api/users", r).then(() => toast(`Added ${r.username}`)).catch(e => toast(e.message));
  }
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 86400 * 30) return Math.floor(s / 86400) + "d ago";
  return new Date(ts * 1000).toLocaleDateString();
}
