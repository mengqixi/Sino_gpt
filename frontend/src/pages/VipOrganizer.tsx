import { Archive, CheckCircle2, Download, Eye, FileImage, LoaderCircle, RefreshCw, UploadCloud } from "lucide-react";
import type { DragEvent } from "react";
import { useMemo, useRef, useState } from "react";
import { api } from "../api/client";

type UploadItem = {
  image_id: number;
  file_name: string;
  preview_url: string;
  original_url?: string;
  width: number;
  height: number;
};

type Slot = {
  file_name: string;
  title: string;
  size: string;
  kind: string;
  image_ids: number[];
  confidence: number;
  reason: string;
};

const PRODUCT_ROLE_OPTIONS = [
  ["auto", "使用自动判断"],
  ["front", "正面主图"],
  ["semi_side", "半侧面 / 三分之二角度"],
  ["side", "完整侧面"],
  ["back", "背面"],
  ["top", "顶部 / 开口全景"],
  ["bottom", "底部"],
  ["transparent", "透明正面"],
  ["strap", "肩带完整展示"],
  ["detail", "局部细节"],
  ["ignore", "忽略此图"]
] as const;

const ROLE_LABELS = Object.fromEntries(PRODUCT_ROLE_OPTIONS) as Record<string, string>;
const DETAIL_TAG_OPTIONS = [
  ["logo", "ELLE Logo"],
  ["hardware", "五金"],
  ["strap_chain", "肩带 / 链条"],
  ["zipper_opening", "拉链 / 开口"],
  ["interior", "内里"],
  ["inner_pocket_label", "内袋 / 内标"],
  ["material_texture", "材质 / 纹理"]
] as const;
const TAG_LABELS = Object.fromEntries(DETAIL_TAG_OPTIONS) as Record<string, string>;
const SUPPORTED_IMAGE_NAME = /\.(jpe?g|png|webp)$/i;

function displayMillimeters(value: string) {
  const numeric = Number.parseFloat(value);
  return Number.isFinite(numeric) ? `${Math.round(numeric * 10)}mm` : "--mm";
}

function UploadSection({ title, hint, items, multiple = true, disabled = false, onUpload }: {
  title: string;
  hint: string;
  items: UploadItem[];
  multiple?: boolean;
  disabled?: boolean;
  onUpload: (files: FileList | File[] | null, skipped?: number) => void;
}) {
  const [dragging, setDragging] = useState(false);

  function acceptFiles(files: FileList | File[]) {
    const incoming = Array.from(files);
    const supported = incoming.filter((file) => SUPPORTED_IMAGE_NAME.test(file.name));
    const selected = multiple ? supported : supported.slice(0, 1);
    onUpload(selected, incoming.length - selected.length);
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    setDragging(false);
    if (disabled) return;
    acceptFiles(event.dataTransfer.files);
  }

  return (
    <section
      className={`organizer-upload-block${dragging ? " is-dragging" : ""}${disabled ? " is-disabled" : ""}`}
      onDragEnter={(event) => { event.preventDefault(); if (!disabled) setDragging(true); }}
      onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = disabled ? "none" : "copy"; if (!disabled) setDragging(true); }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={handleDrop}
    >
      <div><strong>{title}</strong><span>{hint}</span></div>
      <label className="organizer-upload-button">
        <UploadCloud size={22} />
        <span>{dragging ? "松开即可上传" : items.length ? `已上传 ${items.length} 张，可继续拖入或点击添加` : multiple ? "拖入多张图片，或点击选择" : "拖入图片，或点击选择"}</span>
        <input
          type="file"
          accept="image/*"
          multiple={multiple}
          disabled={disabled}
          onChange={(event) => {
            if (event.target.files) acceptFiles(event.target.files);
            event.currentTarget.value = "";
          }}
        />
      </label>
      {items.length > 0 && <div className="organizer-thumb-row">{items.map((item) => (
        <div key={item.image_id} title={item.file_name}><img src={item.preview_url} alt={item.file_name} /><small>{item.file_name}</small></div>
      ))}</div>}
    </section>
  );
}

export default function VipOrganizer() {
  const [sessionId, setSessionId] = useState("");
  const sessionIdRef = useRef("");
  const sessionPromiseRef = useRef<Promise<string> | null>(null);
  const pendingUploadsRef = useRef(0);
  const [products, setProducts] = useState<UploadItem[]>([]);
  const [models, setModels] = useState<UploadItem[]>([]);
  const [tags, setTags] = useState<UploadItem[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [assets, setAssets] = useState<Record<string, any[]>>({ product: [], model: [], tag: [] });
  const [assetRoles, setAssetRoles] = useState<Record<number, string>>({});
  const [assetTags, setAssetTags] = useState<Record<number, string[]>>({});
  const [apiRoleNotes, setApiRoleNotes] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [exportResult, setExportResult] = useState<any>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [info, setInfo] = useState({
    product_name: "ELLE箱包",
    product_length: "",
    product_width: "",
    product_height: "",
    main_material: "",
    lining_material: "",
    wearing_method: "",
    disclaimer: "包身长宽高测量均为最长部分\n误差在1-2cm之间因手工测量均属正常"
  });

  const allAssets = useMemo(() => [...(assets.product || []), ...(assets.model || []), ...(assets.tag || [])], [assets]);

  function applyNewSession(nextSessionId: string) {
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
    setProducts([]);
    setModels([]);
    setTags([]);
    setSlots([]);
    setAssets({ product: [], model: [], tag: [] });
    setAssetRoles({});
    setAssetTags({});
    setApiRoleNotes({});
    setExportResult(null);
  }

  async function ensureSession() {
    if (sessionIdRef.current) return sessionIdRef.current;
    if (!sessionPromiseRef.current) {
      sessionPromiseRef.current = api.startVipOrganizerSession()
        .then((session) => {
          applyNewSession(session.session_id);
          return session.session_id;
        })
        .finally(() => { sessionPromiseRef.current = null; });
    }
    return sessionPromiseRef.current;
  }

  async function startNewSession() {
    setBusy(true);
    setMessage("");
    try {
      const session = await api.startVipOrganizerSession();
      applyNewSession(session.session_id);
      setMessage("已开始新一轮，上一轮自动化整理素材和ZIP已删除。AI生成记录不受影响。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function upload(kind: "product" | "model" | "tag", files: FileList | File[] | null, preSkipped = 0) {
    const fileItems = Array.from(files || []);
    if (!fileItems.length) {
      if (preSkipped) setMessage(`已跳过 ${preSkipped} 个不支持或未导入的文件。`);
      return;
    }
    pendingUploadsRef.current += 1;
    setBusy(true);
    setMessage(`正在一次性上传 ${fileItems.length} 张原图，请勿关闭页面……`);
    try {
      const currentSession = await ensureSession();
      const uploaded = await api.uploadVipOrganizerAssets(currentSession, kind, fileItems);
      if (kind === "product") setProducts((current) => [...current, ...uploaded]);
      if (kind === "model") setModels((current) => [...current, ...uploaded]);
      if (kind === "tag") setTags(uploaded.slice(-1));
      setSlots([]);
      setExportResult(null);
      const skipped = preSkipped + fileItems.length - uploaded.length;
      setMessage(skipped ? `已上传 ${uploaded.length} 张图片，自动跳过 ${skipped} 个不支持、损坏或未导入的文件。` : `已上传 ${uploaded.length} 张图片。`);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      pendingUploadsRef.current -= 1;
      if (pendingUploadsRef.current === 0) setBusy(false);
    }
  }

  async function analyze(rolesOverride?: Record<number, string>) {
    if (!products.length) return setMessage("请先上传商品原图");
    setBusy(true);
    setMessage("");
    setExportResult(null);
    try {
      const result = await api.analyzeVipOrganizer({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        model_image_ids: models.map((item) => item.image_id),
        tag_image_ids: tags.map((item) => item.image_id),
        asset_roles: rolesOverride || assetRoles,
        asset_tags: assetTags
      });
      setSlots(result.slots);
      setAssets(result.assets);
      setMessage("已生成自动整理初稿。黄色或红色可信度项目需要重点确认。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function analyzeWithApi() {
    if (!products.length) return setMessage("请先上传商品原图");
    setBusy(true);
    setMessage("正在用 API 分析全部商品图，本次只调用一次……");
    try {
      const apiResult = await api.analyzeVipOrganizerWithApi({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id)
      });
      const nextRoles = apiResult.asset_roles as Record<number, string>;
      const nextTags = (apiResult.asset_tags || {}) as Record<number, string[]>;
      setAssetRoles(nextRoles);
      setAssetTags(nextTags);
      setApiRoleNotes(Object.fromEntries(apiResult.items.map((item: any) => [
        item.image_id,
        `${item.confidence}% · ${item.reason}${item.tags?.length ? ` · ${item.tags.map((tag: string) => TAG_LABELS[tag] || tag).join("、")}` : ""}`
      ])));
      const result = await api.analyzeVipOrganizer({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        model_image_ids: models.map((item) => item.image_id),
        tag_image_ids: tags.map((item) => item.image_id),
        asset_roles: nextRoles,
        asset_tags: nextTags
      });
      setSlots(result.slots);
      setAssets(result.assets);
      setExportResult(null);
      setMessage("API 已完成一次素材分类，并按固定标签重新整理。请检查低可信度位置。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  function updateAssetRole(imageId: number, role: string) {
    setAssetRoles((current) => {
      const next = { ...current };
      if (role === "auto") delete next[imageId];
      else next[imageId] = role;
      return next;
    });
    setExportResult(null);
    setMessage("固定标签已修改，请点击“按标签重新整理”更新输出位置。");
  }

  function effectiveAssetTags(asset: any) {
    return assetTags[asset.id] ?? asset.suggested_tags ?? [];
  }

  function toggleAssetTag(asset: any, tag: string) {
    setAssetTags((current) => {
      const selected = current[asset.id] ?? asset.suggested_tags ?? [];
      const nextTags = selected.includes(tag) ? selected.filter((item: string) => item !== tag) : [...selected, tag];
      return { ...current, [asset.id]: nextTags };
    });
    setExportResult(null);
    setMessage("细节标签已修改，请点击“按标签重新整理”更新输出位置。");
  }

  function resetAssetTags(imageId: number) {
    setAssetTags((current) => {
      const next = { ...current };
      delete next[imageId];
      return next;
    });
    setExportResult(null);
  }

  function optionsFor(slot: Slot) {
    if (slot.kind === "model") return assets.model || [];
    if (slot.kind === "tag") return assets.tag || [];
    return assets.product || [];
  }

  function updateSlot(fileName: string, index: number, value: number) {
    setSlots((current) => current.map((slot) => {
      const linkedModelSlot = fileName === "1.jpg" || fileName === "50.jpg";
      const shouldUpdate = slot.file_name === fileName || (linkedModelSlot && (slot.file_name === "1.jpg" || slot.file_name === "50.jpg"));
      if (!shouldUpdate) return slot;
      const next = [...slot.image_ids];
      next[index] = value;
      return {
        ...slot,
        image_ids: next.filter(Boolean),
        confidence: 100,
        reason: linkedModelSlot ? "1.jpg与50.jpg已同步使用同一张模特图" : "已由设计师人工确认",
      };
    }));
    setExportResult(null);
  }

  function selectedAsset(id?: number) {
    return allAssets.find((item) => item.id === id);
  }

  function assetOptionLabel(asset: any, kind: string) {
    if (kind === "model") return `【模特图】${asset.file_name}`;
    if (kind === "tag") return `【吊牌图】${asset.file_name}`;
    const fixedRole = assetRoles[asset.id];
    const role = fixedRole || asset.suggested_role || "detail";
    const source = fixedRole ? "固定" : "自动";
    const tagText = effectiveAssetTags(asset).slice(0, 2).map((tag: string) => TAG_LABELS[tag] || tag).join("+");
    return `【${source}·${ROLE_LABELS[role] || "局部细节"}${tagText ? `·${tagText}` : ""}】${asset.file_name}`;
  }

  async function exportZip() {
    setBusy(true);
    setMessage("");
    try {
      const dimensions = [info.product_length, info.product_width, info.product_height]
        .map((value) => value.trim())
        .filter(Boolean)
        .join(" × ");
      const result = await api.exportVipOrganizer({
        session_id: sessionId,
        slots,
        product_info: { ...info, dimensions: dimensions ? `${dimensions} cm` : "" }
      });
      setExportResult(result);
      setMessage(result.missing.length ? `已生成 ${result.generated_count} 张，缺少：${result.missing.join("、")}` : "15张唯品会套图已生成，可以下载ZIP。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page organizer-page">
      <header className="page-header">
        <h1>唯品会自动化整理</h1>
        <p>上传商品原图和模特图，本地生成15张唯品会套图初稿；需要时可手动调用一次API分析。</p>
      </header>

      <section className="panel organizer-source-panel">
        <div className="section-title-row">
          <div><h2>1. 上传素材</h2><p>商品图和模特图分开上传，可以显著减少错误分类。</p></div>
          <div className="button-row">
            {sessionId && <button disabled={busy} onClick={startNewSession}><RefreshCw size={18} />开始新一轮</button>}
            <button className="primary" disabled={busy || !products.length} onClick={() => analyze()}>
              {busy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}自动整理初稿
            </button>
          </div>
        </div>
        <div className="organizer-upload-columns">
          <UploadSection title="商品原图" hint="一次可多选；正面、侧面、背面、Logo、内里、透明图等" items={products} disabled={busy} onUpload={(files) => upload("product", files)} />
          <UploadSection title="模特图" hint="一次可多选；建议至少3张，系统分别用于主图和详情页" items={models} disabled={busy} onUpload={(files) => upload("model", files)} />
          <UploadSection title="吊牌图" hint="可选；用于801.jpg" items={tags} multiple={false} disabled={busy} onUpload={(files) => upload("tag", files)} />
        </div>
      </section>

      {slots.length > 0 && <>
        <section className="panel organizer-analysis-panel">
          <div className="section-title-row">
            <div><h2>2. 素材分析</h2><p>主类别决定图片用途，细节标签可以多选；低可信度结果建议人工确认或调用一次API。</p></div>
            <div className="button-row">
              <button disabled={busy} onClick={analyzeWithApi}><RefreshCw size={18} />API 分析素材（1次）</button>
              <button className="primary" disabled={busy} onClick={() => analyze()}>
                {busy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}按标签重新整理
              </button>
            </div>
          </div>
          <div className="organizer-analysis-grid">
            {(assets.product || []).map((asset: any) => (
              <article key={asset.id} className="organizer-analysis-item">
                <button className="organizer-analysis-preview" onClick={() => setPreview(asset.original_url || asset.preview_url)}>
                  <img src={asset.preview_url} alt={asset.file_name} />
                </button>
                <div>
                  <strong title={asset.file_name}>{asset.file_name}</strong>
                  <small className={`organizer-role-badge role-confidence-${asset.role_confidence >= 80 ? "high" : asset.role_confidence >= 60 ? "medium" : "low"}`} title={asset.role_reason}>
                    <span>自动 {asset.role_confidence}%</span>{ROLE_LABELS[asset.suggested_role] || "局部细节"}
                  </small>
                  <small className="organizer-auto-reason" title={asset.role_reason}>{asset.role_reason}</small>
                  {apiRoleNotes[asset.id] && <small className="api-role-note">API：{apiRoleNotes[asset.id]}</small>}
                  <select value={assetRoles[asset.id] || "auto"} onChange={(event) => updateAssetRole(asset.id, event.target.value)}>
                    {PRODUCT_ROLE_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                  </select>
                  <div className="organizer-tag-editor">
                    <span>细节标签（可多选）</span>
                    <div>{DETAIL_TAG_OPTIONS.map(([value, label]) => {
                      const active = effectiveAssetTags(asset).includes(value);
                      return <button type="button" className={active ? "is-active" : ""} aria-pressed={active} key={value} onClick={() => toggleAssetTag(asset, value)}>{label}</button>;
                    })}</div>
                    {assetTags[asset.id] !== undefined && <button type="button" className="organizer-tags-reset" onClick={() => resetAssetTags(asset.id)}>恢复自动标签</button>}
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="panel organizer-info-panel">
          <div className="section-title-row"><div><h2>3. 商品信息</h2><p>用于自动生成401.jpg产品信息页。</p></div></div>
          <div className="organizer-info-grid">
            <label>商品名称<input value={info.product_name} onChange={(event) => setInfo({ ...info, product_name: event.target.value })} /></label>
            <label>长（cm）<input inputMode="decimal" placeholder="例如：20" value={info.product_length} onChange={(event) => setInfo({ ...info, product_length: event.target.value })} /></label>
            <label>宽（cm）<input inputMode="decimal" placeholder="例如：8" value={info.product_width} onChange={(event) => setInfo({ ...info, product_width: event.target.value })} /></label>
            <label>高（cm）<input inputMode="decimal" placeholder="例如：14" value={info.product_height} onChange={(event) => setInfo({ ...info, product_height: event.target.value })} /></label>
            <label>主要材质<input value={info.main_material} onChange={(event) => setInfo({ ...info, main_material: event.target.value })} /></label>
            <label>里料材质<input value={info.lining_material} onChange={(event) => setInfo({ ...info, lining_material: event.target.value })} /></label>
            <label>包型背法<input placeholder="例如：单肩/斜挎" value={info.wearing_method} onChange={(event) => setInfo({ ...info, wearing_method: event.target.value })} /></label>
            <label className="wide">免责声明<textarea rows={2} value={info.disclaimer} onChange={(event) => setInfo({ ...info, disclaimer: event.target.value })} /></label>
          </div>
        </section>

        <section className="panel organizer-slots-panel">
          <div className="section-title-row"><div><h2>4. 检查15个输出位置</h2><p>固定标签会自动带入；这里仍可对每个输出位置单独换图。</p></div></div>
          <div className="organizer-slot-grid">
            {slots.map((slot) => {
              const selected = selectedAsset(slot.image_ids[0]);
              const count = slot.file_name === "606.jpg" ? 4 : 1;
              const collageAssets = slot.file_name === "606.jpg"
                ? slot.image_ids.slice(0, 4).map((id) => selectedAsset(id)).filter(Boolean)
                : [];
              const editableSource = slot.kind !== "generated" || slot.file_name === "401.jpg";
              return <article className="organizer-slot" key={slot.file_name}>
                <div className="organizer-slot-preview">
                  {slot.file_name === "401.jpg" ? <div className="organizer-info-preview">
                    <strong>产品信息</strong>
                    <dl>
                      <div><dt>材质</dt><dd>{info.main_material || "待填写"}</dd></div>
                      <div><dt>里料</dt><dd>{info.lining_material || "待填写"}</dd></div>
                      <div><dt>背法</dt><dd>{info.wearing_method || "待填写"}</dd></div>
                    </dl>
                    <div className="organizer-info-product">
                      {selected ? <button onClick={() => setPreview(selected.original_url || selected.preview_url)}><img src={selected.preview_url} alt="产品信息来源" /></button> : <FileImage size={24} />}
                      <span className="measure measure-height">{displayMillimeters(info.product_height)}</span>
                      <span className="measure measure-length">{displayMillimeters(info.product_length)}</span>
                      <span className="measure measure-width">{displayMillimeters(info.product_width)}</span>
                    </div>
                    <div className="organizer-info-notes"><span>* 包身长宽高测量均为最长部分</span><span>* 手工测量存在1-2cm误差属于正常</span></div>
                  </div> : slot.file_name === "606.jpg" && collageAssets.length ? <div className="organizer-collage-preview">
                    <strong>多角度展示</strong>
                    <div>{collageAssets.map((asset: any, index: number) => <button key={`${asset.id}-${index}`} onClick={() => setPreview(asset.original_url || asset.preview_url)}><img src={asset.preview_url} alt={`多角度${index + 1}`} /></button>)}</div>
                    <span aria-hidden="true">＋</span>
                  </div> : slot.kind === "generated" ? <div className="generated-placeholder"><FileImage size={30} /><span>自动排版</span></div> : selected ? <button onClick={() => setPreview(selected.original_url || selected.preview_url)}><img src={selected.preview_url} alt={slot.title} /></button> : <div className="generated-placeholder"><FileImage size={30} /><span>缺少素材</span></div>}
                </div>
                <div className="organizer-slot-body">
                  <div className="organizer-slot-title"><strong>{slot.file_name}</strong><span>{slot.title}</span><small>{slot.size}</small></div>
                  {editableSource && Array.from({ length: count }).map((_, index) => (
                    <label key={index}>{count > 1 ? `来源 ${index + 1}` : "来源图片"}
                      <select value={slot.image_ids[index] || ""} onChange={(event) => updateSlot(slot.file_name, index, Number(event.target.value))}>
                        <option value="">请选择</option>
                        {optionsFor(slot).map((asset: any) => <option value={asset.id} key={asset.id}>{assetOptionLabel(asset, slot.kind)}</option>)}
                      </select>
                    </label>
                  ))}
                  <div className={`confidence confidence-${slot.confidence >= 80 ? "high" : slot.confidence >= 50 ? "medium" : "low"}`}>
                    <span>可信度 {slot.confidence}%</span><p>{slot.reason}</p>
                  </div>
                </div>
              </article>;
            })}
          </div>
          <div className="organizer-export-bar">
            <span><CheckCircle2 size={18} />导出前请确认所有低可信度项目</span>
            <button className="primary" disabled={busy} onClick={exportZip}><Archive size={18} />生成套图并打包</button>
          </div>
        </section>
      </>}

      {message && <div className="alert warning">{message}</div>}
      {exportResult && <section className="panel organizer-export-result">
        <div><h2>导出结果</h2><p>已生成 {exportResult.generated_count} 张图片。</p></div>
        <a className="primary" href={exportResult.download_url} download><Download size={18} />下载唯品会套图 ZIP</a>
        <div className="organizer-export-previews">{exportResult.previews.map((url: string) => <button key={url} onClick={() => setPreview(url)}><img src={url} alt="导出预览" /><Eye size={15} /></button>)}</div>
      </section>}
      {preview && <div className="image-modal" onClick={() => setPreview(null)}><button onClick={() => setPreview(null)}>关闭</button><img src={preview} alt="图片预览" onClick={(event) => event.stopPropagation()} /></div>}
    </section>
  );
}
