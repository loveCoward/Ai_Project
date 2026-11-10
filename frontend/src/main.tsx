import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { ApiError, downloadMaterial, login, readDraft, readIdentity, setAccessToken, setUnauthorizedHandler, type Identity } from "./api";
import "./styles.css";

type AuthContextValue = {
  identity: Identity | null;
  authenticate: (username: string, password: string) => Promise<void>;
  signOut: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("Authentication context is unavailable");
  return value;
}

function AuthProvider({ children }: { children: React.ReactNode }) {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const value = useMemo<AuthContextValue>(
    () => ({
      identity,
      async authenticate(username, password) {
        const result = await login(username, password);
        setAccessToken(result.access_token);
        try {
          const current = await readIdentity();
          setToken(result.access_token);
          setIdentity(current);
        } catch (error) {
          setAccessToken(null);
          throw error;
        }
      },
      signOut() {
        setAccessToken(null);
        setToken(null);
        setIdentity(null);
      },
    }),
    [identity],
  );

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAccessToken(null);
      setToken(null);
      setIdentity(null);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  return <AuthContext.Provider value={value}><div data-authenticated={Boolean(token)}>{children}</div></AuthContext.Provider>;
}

function LoginPage() {
  const { identity, authenticate } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (identity) return <Navigate to="/workspace" replace />;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await authenticate(username.trim(), password);
      navigate("/workspace", { replace: true });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "登录失败，请检查账号信息后重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-layout">
      <section className="login-brand" aria-label="BidDesk 售前工作台">
        <div className="brand-mark">B</div>
        <p className="eyebrow">BIDDESK / PRESALES</p>
        <h1>售前工作台</h1>
        <p className="brand-copy">围绕商机材料与方案证据开展协作。</p>
        <div className="brand-footer"><span className="status-dot" />业务服务认证入口</div>
      </section>
      <section className="login-panel">
        <div className="login-form-wrap">
          <p className="eyebrow">工作空间</p>
          <h2>登录</h2>
          <p className="muted">使用已配置的业务账号继续。</p>
          <form onSubmit={submit}>
            <label htmlFor="username">账号</label>
            <input id="username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required />
            <label htmlFor="password">密码</label>
            <input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary-button" type="submit" disabled={busy}>{busy ? "正在验证…" : "进入工作台"}</button>
          </form>
          <p className="security-note">访问令牌仅保存在当前页面内存中。</p>
        </div>
      </section>
    </main>
  );
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  return useAuth().identity ? <>{children}</> : <Navigate to="/login" replace />;
}

function WorkspacePage() {
  const { identity, signOut } = useAuth();
  const navigate = useNavigate();
  const [draftId, setDraftId] = useState("");
  const [materialId, setMaterialId] = useState("");
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function runRead(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    setDraft("");
    try {
      await operation();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "读取失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  async function openDraft(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await runRead(async () => setDraft(JSON.stringify(await readDraft(draftId.trim()), null, 2)));
  }

  async function openMaterial(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await runRead(async () => {
      const blob = await downloadMaterial(materialId.trim());
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `material-${materialId.trim()}.bin`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice("材料已通过受限接口读取并下载。");
    });
  }

  function logout() {
    signOut();
    navigate("/login", { replace: true });
  }

  return (
    <div className="workspace-shell">
      <header className="topbar">
        <a className="wordmark" href="/workspace" aria-label="BidDesk 工作台首页"><span className="brand-mark small">B</span><span>BidDesk</span></a>
        <div className="topbar-right"><span className="identity"><span className="status-dot" />{identity?.subject}</span><button className="quiet-button" onClick={logout}>退出</button></div>
      </header>
      <main className="workspace-main">
        <div className="page-heading"><div><p className="eyebrow">PRESALES WORKSPACE</p><h1>工作台</h1><p className="muted">当前账号可访问的售前对象</p></div><span className="connection"><span className="status-dot" />已认证</span></div>
        <section className="access-section" aria-labelledby="access-title">
          <div className="section-heading"><div><p className="eyebrow">受限数据</p><h2 id="access-title">对象读取</h2></div><span className="section-count">API / 01</span></div>
          <div className="read-grid">
            <form className="read-form" onSubmit={openDraft}>
              <div className="read-icon draft-icon" aria-hidden="true">D</div>
              <div><h3>方案草稿</h3><p>使用修订 ID 读取 JSON 内容</p></div>
              <label htmlFor="draft-id">修订 ID</label>
              <input id="draft-id" value={draftId} onChange={(event) => setDraftId(event.target.value)} placeholder="UUID" required />
              <button type="submit" disabled={busy}>读取草稿</button>
            </form>
            <form className="read-form" onSubmit={openMaterial}>
              <div className="read-icon material-icon" aria-hidden="true">M</div>
              <div><h3>客户材料</h3><p>使用材料版本 ID 下载原始文件</p></div>
              <label htmlFor="material-id">材料版本 ID</label>
              <input id="material-id" value={materialId} onChange={(event) => setMaterialId(event.target.value)} placeholder="UUID" required />
              <button type="submit" disabled={busy}>读取材料</button>
            </form>
          </div>
          {(error || notice) && <p className={error ? "result-message error-message" : "result-message"} role={error ? "alert" : "status"}>{busy ? "正在读取…" : error || notice}</p>}
          {draft && <pre className="draft-output"><code>{draft}</code></pre>}
        </section>
        <footer className="workspace-footer">BidDesk · 售前业务入口</footer>
      </main>
    </div>
  );
}

function App() {
  return <BrowserRouter><AuthProvider><Routes><Route path="/login" element={<LoginPage />} /><Route path="/workspace" element={<ProtectedRoute><WorkspacePage /></ProtectedRoute>} /><Route path="*" element={<Navigate to="/workspace" replace />} /></Routes></AuthProvider></BrowserRouter>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
