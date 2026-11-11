import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiJson, parseSource } from "./api";
import type { Applicability, Evidence, EvidenceQuery, ManifestItem, RetrievalResult, SourceDeclaration } from "./contracts.generated";
type Resource = {
    id: string;
    kind: string;
};
type Version = {
    version_id: string;
    document_id: string;
    filename: string;
    state: string;
    revision: number;
    ready: boolean;
};
type Overview = {
    role: string;
    resources: Resource[];
    knowledge_versions: Version[];
    catalog_revision: number;
    authorization_revision: number;
    retrieval_mode: string;
    model_identity: string | null;
};
type Build = {
    id: string;
    state: string;
    mode: string;
    chunk_count: number;
    error_code: string | null;
};
type Review = {
    id: string;
    action: string;
    approved: boolean | null;
};
type Detail = Version & {
    scope: Applicability;
    catalog_revision: number;
    conflicts: string[];
    builds: Build[];
    parse_jobs: {
        id: string;
        state: string;
        error_code: string | null;
    }[];
    reviews: Review[];
};
type Input = {
    material_version_id: string;
    filename: string;
    state: string | null;
    necessity: ManifestItem["necessity"];
    error_code: string | null;
};
type Inputs = {
    complete: boolean;
    task_revision: number;
    input_revision: number;
    materials: Input[];
    unresolved: unknown[];
    unused_sources: unknown[];
};
async function encodedFile(file: File) {
    if (file.size > 20 * 1024 * 1024)
        throw new Error("单文件不能超过20 MiB。");
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192)
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    return btoa(binary);
}
export function KnowledgeWorkbench() {
    const [overview, setOverview] = useState<Overview | null>(null);
    const [tab, setTab] = useState("search");
    const [products, setProducts] = useState("synthetic-product");
    const [deployments, setDeployments] = useState("private");
    const [declaration, setDeclaration] = useState<SourceDeclaration>("synthetic");
    const [query, setQuery] = useState("");
    const [result, setResult] = useState<RetrievalResult | null>(null);
    const [evidence, setEvidence] = useState<Evidence | null>(null);
    const [detail, setDetail] = useState<Detail | null>(null);
    const [inputs, setInputs] = useState<Inputs | null>(null);
    const [taskId, setTaskId] = useState("");
    const [documentId, setDocumentId] = useState("");
    const [file, setFile] = useState<File | null>(null);
    const [reason, setReason] = useState("");
    const [replacement, setReplacement] = useState("");
    const [notice, setNotice] = useState("");
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const alive = useRef(true);
    const latest = useRef(0);
    const authorizationRevision = useRef<number | null>(null);
    const catalogRevision = useRef<number | null>(null);
    const uploadIntent = useRef<{
        fingerprint: string;
        requestId: string;
        taskRevision?: number;
    } | null>(null);
    function clearRestricted() { setResult(null); setEvidence(null); setDetail(null); setInputs(null); }
    async function refresh() {
        const current = await apiJson<Overview>("/api/workbench");
        if (!alive.current)
            return;
        if ((authorizationRevision.current !== null && current.authorization_revision !== authorizationRevision.current) ||
            (catalogRevision.current !== null && current.catalog_revision !== catalogRevision.current)) {
            clearRestricted();
            setNotice("权限或知识目录已改变，请重新读取当前内容。");
        }
        authorizationRevision.current = current.authorization_revision;
        catalogRevision.current = current.catalog_revision;
        setOverview(current);
    }
    useEffect(() => {
        alive.current = true;
        const update = () => { void refresh().catch(() => { if (alive.current) {
            clearRestricted();
            setOverview(null);
            setError("无法核实当前权限，请重新登录或刷新。");
        } }); };
        update();
        const timer = window.setInterval(update, 15000);
        return () => { alive.current = false; window.clearInterval(timer); latest.current += 1; };
    }, []);
    async function run(operation: () => Promise<void>) {
        const revision = ++latest.current;
        setBusy(true);
        setNotice("");
        setError("");
        setEvidence(null);
        setResult(null);
        try {
            await operation();
        }
        catch (caught) {
            if (alive.current && revision === latest.current) {
                clearRestricted();
                setError(caught instanceof Error ? caught.message : "操作失败，请核实当前状态。");
            }
        }
        finally {
            if (alive.current && revision === latest.current)
                setBusy(false);
        }
    }
    function scope(): Applicability { return { products: products.split(",").map(v => v.trim()).filter(Boolean), deployments: deployments.split(",").map(v => v.trim()).filter(Boolean) }; }
    async function search(event: FormEvent) {
        event.preventDefault();
        const body: EvidenceQuery = { query, scope: scope(), declaration, source_scope: "published_knowledge" };
        await run(async () => { const value = await apiJson<RetrievalResult>("/api/evidence/retrieve", body); if (alive.current)
            setResult(value); });
    }
    async function loadDetail(id: string) {
        await run(async () => { const value = await apiJson<Detail>(`/api/knowledge/${id}`); if (alive.current)
            setDetail(value); });
    }
    async function upload(event: FormEvent) {
        event.preventDefault();
        await run(async () => {
            if (!file)
                throw new Error("请选择材料文件。");
            const content = await encodedFile(file);
            const fingerprint = JSON.stringify([tab, documentId, taskId, file.name, content, declaration, scope()]);
            if (uploadIntent.current?.fingerprint !== fingerprint)
                uploadIntent.current = { fingerprint, requestId: crypto.randomUUID(), taskRevision: inputs?.task_revision };
            const body = { filename: file.name, content_base64: content, declaration, request_id: uploadIntent.current.requestId };
            const path = tab === "knowledge" ? `/api/knowledge/documents/${documentId}/versions` : `/api/materials/${taskId}/versions`;
            const value = await apiJson<{
                version_id?: string;
                material_version_id?: string;
                source_version_id?: string;
            }>(path, tab === "knowledge" ? { ...body, scope: scope() } : { ...body, expected_task_revision: uploadIntent.current.taskRevision });
            if (alive.current) {
                setNotice(`文件已登记；请单独解析。版本：${value.version_id ?? value.material_version_id ?? value.source_version_id ?? "请刷新任务输入"}`);
                await refresh();
            }
        });
    }
    async function loadInputs() {
        await run(async () => { const value = await apiJson<Inputs>(`/api/tasks/${taskId}/inputs`); if (alive.current)
            setInputs(value); });
    }
    async function decide(review: Review, approved: boolean) {
        if (!reason.trim()) {
            setError("请填写审核理由。");
            return;
        }
        await run(async () => {
            await apiJson(`/api/knowledge-reviews/${review.id}/decision`, { approved, reason, request_id: crypto.randomUUID() });
            setDetail(null);
            await refresh();
            setNotice("审核决定已保存，请重新读取版本状态。");
        });
    }
    const architect = overview?.role === "architect";
    return <section className="knowledge-workbench" aria-label="材料与知识工作区">
    <div className="section-heading"><div><p className="eyebrow">EVIDENCE WORKSPACE</p><h2>材料与知识</h2></div><button disabled={busy} onClick={() => void run(refresh)}>刷新当前权限</button></div>
    <p className="mode-banner">检索模式：{overview?.retrieval_mode ?? "待核实"} · {overview?.model_identity ?? "未配置Embedding"}{overview?.retrieval_mode === "fake" && " · 确定性工程模式，不能证明语义质量"}</p>
    <nav className="workbench-tabs" aria-label="业务页面">{[["search", "检索证据"], ["materials", "任务材料"], ["knowledge", "知识审核"]].map(([key, label]) => <button key={key} aria-current={tab === key ? "page" : undefined} disabled={busy} onClick={() => { setTab(key); clearRestricted(); setError(""); setNotice(""); }}>{label}</button>)}</nav>
    <fieldset className="scope-fields" disabled={busy}><legend>适用范围与资料声明</legend><label>产品（逗号分隔）<input value={products} onChange={e => setProducts(e.target.value)}/></label><label>部署条件（逗号分隔）<input value={deployments} onChange={e => setDeployments(e.target.value)}/></label><label>查询 / 上传内容来源<select value={declaration} onChange={e => setDeclaration(e.target.value as SourceDeclaration)}><option value="synthetic">合成资料</option><option value="owned">自有资料</option><option value="public_authorized">获授权公开资料</option></select></label></fieldset>
    {tab === "search" && <><form className="business-form" onSubmit={search}><label htmlFor="evidence-query">检索问题</label><textarea id="evidence-query" value={query} onChange={e => setQuery(e.target.value)} maxLength={2000} required/><button disabled={busy || !overview || overview.retrieval_mode === "disabled"}>检索已发布知识</button></form>
      {result && <div aria-live="polite"><p>返回 {result.hits.length} 条 · {result.mode} · 目录修订 {result.catalog_revision}{result.cache_hit && " · 已重验缓存"}</p>{result.degradation.map(value => <p className="degradation" key={value}>降级：{value}</p>)}{result.hits.length === 0 && <p>当前权限和适用范围内没有匹配知识。</p>}<ol className="evidence-list">{result.hits.map(hit => <li key={hit.evidence_id}><p>{hit.text}</p><small>来源版本 {hit.source_version_id}</small><button disabled={busy} onClick={() => void run(async () => { const value = await apiJson<Evidence>(`/api/evidence/${hit.evidence_id}`); if (alive.current)
            setEvidence(value); })}>查看当前可访问出处</button></li>)}</ol></div>}
      {evidence && <article className="evidence-detail"><h3>原始出处</h3><p>{evidence.text}</p><pre>{JSON.stringify(evidence.location, null, 2)}</pre><p>字符范围 {evidence.start_offset}–{evidence.end_offset} · 证据 {evidence.evidence_id}</p></article>}</>}
    {tab === "materials" && <><div className="business-form"><label>任务<select value={taskId} onChange={e => { setTaskId(e.target.value); setInputs(null); }}><option value="">请选择有权任务</option>{overview?.resources.filter(v => v.kind === "task").map(v => <option key={v.id}>{v.id}</option>)}</select></label><button disabled={busy || !taskId} onClick={() => void loadInputs()}>读取材料状态</button></div>{inputs && <><p>输入修订 {inputs.input_revision} · {inputs.complete ? "输入完整" : "输入待分类 / 补充"}</p>{inputs.materials.map(item => <article className="material-row" key={item.material_version_id}><strong>{item.filename}</strong><span>{item.state ?? "未解析"} {item.error_code}</span><label>必要性<select disabled={busy || !architect} value={item.necessity} onChange={e => setInputs({ ...inputs, materials: inputs.materials.map(v => v.material_version_id === item.material_version_id ? { ...v, necessity: e.target.value as ManifestItem["necessity"] } : v) })}><option value="unclassified">未分类</option><option value="required">必需</option><option value="optional">可选</option></select></label><button disabled={busy || !architect} onClick={() => void run(async () => { await parseSource(item.material_version_id); const value = await apiJson<Inputs>(`/api/tasks/${taskId}/inputs`); if (alive.current)
        setInputs(value); })}>解析材料</button></article>)}{architect && <button disabled={busy} onClick={() => void run(async () => { const value = await apiJson<Inputs>(`/api/tasks/${taskId}/manifest`, { expected_revision: inputs.task_revision, items: inputs.materials.map(({ material_version_id, necessity }) => ({ material_version_id, necessity })) }); if (alive.current) {
        setInputs(null);
        setNotice(`清单已保存，输入修订 ${value.input_revision}；请重新读取材料状态。`);
    } })}>保存完整输入清单</button>}</>}</>}
    {tab === "knowledge" && <><div className="version-list">{overview?.knowledge_versions.map(version => <button disabled={busy} key={version.version_id} onClick={() => void loadDetail(version.version_id)}><strong>{version.filename}</strong><span>{version.state} · 修订 {version.revision}</span></button>)}</div>{!overview?.knowledge_versions.length && <p>当前无可访问知识版本。文档授权由维护流程管理。</p>}{detail && <article className="version-detail"><h3>版本 {detail.version_id}</h3><p>{detail.state} · 修订 {detail.revision} · 文件{detail.ready ? "就绪" : "未就绪"}</p><p>适用范围：{JSON.stringify(detail.scope)}</p>{detail.conflicts.length > 0 && <p className="degradation">存在范围重叠：{detail.conflicts.join(", ")}</p>}{detail.parse_jobs.map(job => <p key={job.id}>解析：{job.state} {job.error_code}</p>)}{architect && <><button disabled={busy} onClick={() => void run(async () => { const value = await parseSource(detail.version_id); setNotice(JSON.stringify(value)); setDetail(null); })}>解析此版本</button><label>明确替换的旧版本（仅替换时填写）<input value={replacement} onChange={e => setReplacement(e.target.value)}/></label><button disabled={busy || detail.state !== "candidate"} onClick={() => void run(async () => { const value = await apiJson<Build>(`/api/knowledge/${detail.version_id}/index`, { replaces: replacement || null }); setNotice(JSON.stringify(value)); setDetail(null); })}>构建索引</button></>}{detail.builds.map(build => <div key={build.id}><p>索引 {build.id} · {build.state} · {build.mode} · {build.chunk_count} 块 {build.error_code}</p>{architect && build.state === "ready" && <button disabled={busy || !["candidate", "published"].includes(detail.state)} onClick={() => void run(async () => { await apiJson(`/api/knowledge/${detail.version_id}/submit`, { build_id: build.id, expected_revision: detail.revision, expected_catalog: detail.catalog_revision, request_id: crypto.randomUUID(), action: detail.state === "published" ? "retire" : replacement ? "replace" : "publish", replaces: detail.state === "published" ? null : replacement || null }); setDetail(null); await refresh(); setNotice("已提交固定版本审核。"); })}>{detail.state === "published" ? "提交停用审核" : "提交发布 / 替换审核"}</button>}</div>)}{overview?.role === "reviewer" && <><label>独立审核理由<input value={reason} maxLength={1000} onChange={e => setReason(e.target.value)}/></label>{detail.reviews.filter(v => v.approved === null).map(review => <div key={review.id}><p>待审核动作：{review.action}</p><button disabled={busy} onClick={() => void decide(review, true)}>批准此固定动作</button><button disabled={busy} onClick={() => void decide(review, false)}>拒绝此固定动作</button></div>)}<button disabled={busy || !reason.trim()} onClick={() => void run(async () => { await apiJson(`/api/knowledge/${detail.version_id}/scope`, { scope: scope(), expected_revision: detail.revision, expected_catalog: detail.catalog_revision, reason, request_id: crypto.randomUUID() }); setDetail(null); await refresh(); })}>按上方范围调整候选适用性</button></>}</article>}</>}
    {architect && ["materials", "knowledge"].includes(tab) && <form className="business-form" onSubmit={upload}><h3>{tab === "knowledge" ? "登记知识候选版本" : "登记任务材料"}</h3>{tab === "knowledge" && <label>已授权文档<select value={documentId} onChange={e => setDocumentId(e.target.value)} required><option value="">请选择文档</option>{overview?.resources.filter(v => v.kind === "knowledge").map(v => <option key={v.id}>{v.id}</option>)}</select></label>}<label>文件（PDF / DOCX / MD / JSON，20 MiB以内）<input type="file" accept=".pdf,.docx,.md,.json" onChange={e => setFile(e.target.files?.[0] ?? null)} required/></label><button disabled={busy || (tab === "materials" && (!taskId || !inputs))}>登记文件</button><p className="muted">登记不代表解析成功、发布批准或分析完成。提交响应未知时先刷新状态核实，勿重复登记。</p></form>}
    {busy && <p role="status">正在处理，请等待服务端结果…</p>}{notice && <p role="status" className="result-message">{notice}</p>}{error && <p role="alert" className="result-message error-message">{error}</p>}
  </section>;
}
