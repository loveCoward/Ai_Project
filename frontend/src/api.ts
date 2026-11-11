export type Identity = { subject: string };
export type LoginResult = { access_token: string; token_type: string; expires_in: number };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let accessToken: string | null = null;
let sessionRevision = 0;
let onUnauthorized: (() => void) | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
  sessionRevision += 1;
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

async function request(path: string, init: RequestInit = {}) {
  const revision = sessionRevision;
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  headers.set("Cache-Control", "no-store");

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers, cache: "no-store" });
  } catch {
    throw new ApiError(0, "无法连接售前服务，请检查服务状态后重试。");
  }

  if (revision !== sessionRevision) throw new ApiError(401, "登录状态已改变，请重新读取。");

  if (response.status === 401 && path !== "/auth/login") onUnauthorized?.();
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: "对象不存在或当前账号无权读取。",
      409: "对象尚未就绪或修订已改变，请刷新状态后核实。",
      422: "请求参数无效，请检查输入。",
      503: "存储服务暂不可用，请稍后重试。",
    };
    if (response.status === 401) {
      throw new ApiError(401, path === "/auth/login" ? "账号或密码错误。" : "登录已失效，请重新登录。");
    }
    throw new ApiError(response.status, messages[response.status] ?? "服务暂不可用，请稍后重试。");
  }
  return response;
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const response = await request("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return response.json() as Promise<LoginResult>;
}

export async function readIdentity(): Promise<Identity> {
  const response = await request("/auth/me");
  return response.json() as Promise<Identity>;
}

export async function readDraft(revisionId: string): Promise<unknown> {
  const response = await request(`/api/drafts/${encodeURIComponent(revisionId)}`);
  return response.json();
}

export async function downloadMaterial(versionId: string): Promise<Blob> {
  const response = await request(`/api/materials/${encodeURIComponent(versionId)}`);
  return response.blob();
}

export async function apiJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return response.json() as Promise<T>;
}

export async function parseSource(versionId: string): Promise<unknown> {
  return (await request(`/api/sources/${encodeURIComponent(versionId)}/parse`, { method: "POST" })).json();
}
