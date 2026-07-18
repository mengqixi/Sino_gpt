import { CheckCircle2, Crop, Download, FileImage, LoaderCircle, Move, RefreshCw, RotateCcw, Save, UploadCloud, X, ZoomIn, ZoomOut } from "lucide-react";
import type { DragEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
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
  adjustments?: ImageAdjustment[];
};

type ImageAdjustment = {
  zoom: number;
  offset_x: number;
  offset_y: number;
  crop_x: number;
  crop_y: number;
  crop_width: number;
  crop_height: number;
};

type ApiRoleNote = {
  role: string;
  confidence: number;
  reason: string;
  tags: string[];
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
  ["material_texture", "材质 / 纹理"],
  ["bottom_detail", "包底细节"]
] as const;
const TAG_LABELS = Object.fromEntries(DETAIL_TAG_OPTIONS) as Record<string, string>;
const SUPPORTED_IMAGE_NAME = /\.(jpe?g|png|webp)$/i;
const ORGANIZER_PLATFORMS = [
  { id: "vip", label: "唯品会", available: true },
  { id: "jd", label: "京东", available: true }
] as const;
type OrganizerPlatform = "vip" | "jd";

const DEFAULT_ADJUSTMENT: ImageAdjustment = {
  zoom: 1,
  offset_x: 0,
  offset_y: 0,
  crop_x: 0,
  crop_y: 0,
  crop_width: 1,
  crop_height: 1
};

function normalizeAdjustment(value?: Partial<ImageAdjustment>): ImageAdjustment {
  return { ...DEFAULT_ADJUSTMENT, ...(value || {}) };
}

function UploadSection({ title, hint, items, multiple = true, disabled = false, onUpload, onPreview }: {
  title: string;
  hint: string;
  items: UploadItem[];
  multiple?: boolean;
  disabled?: boolean;
  onUpload: (files: FileList | File[] | null, skipped?: number) => void;
  onPreview: (url: string) => void;
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
        <div key={item.image_id} title={item.file_name}>
          <button type="button" onClick={() => onPreview(item.original_url || item.preview_url)} aria-label={`预览 ${item.file_name}`}>
            <img src={item.preview_url} alt={item.file_name} />
          </button>
          <small>{item.file_name}</small>
        </div>
      ))}</div>}
    </section>
  );
}

function SlotAdjustmentEditor({
  sessionId,
  slot,
  sourceIndex,
  sourceUrl,
  initialPreview,
  slots,
  productInfo,
  platform,
  onClose,
  onSave
}: {
  sessionId: string;
  slot: Slot;
  sourceIndex: number;
  sourceUrl: string;
  initialPreview?: string;
  slots: Slot[];
  productInfo: Record<string, string>;
  platform: OrganizerPlatform;
  onClose: () => void;
  onSave: (adjustment: ImageAdjustment, previewUrl?: string) => void;
}) {
  const initial = normalizeAdjustment(slot.adjustments?.[sourceIndex]);
  const [draft, setDraft] = useState<ImageAdjustment>(initial);
  const [renderedPreview, setRenderedPreview] = useState(initialPreview || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cropMode, setCropMode] = useState(false);
  const [cropSelection, setCropSelection] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const sourceStageRef = useRef<HTMLDivElement>(null);
  const sourceImageRef = useRef<HTMLImageElement>(null);
  const cropStartRef = useRef<{ x: number; y: number } | null>(null);
  const cropSelectionRef = useRef<{ left: number; top: number; width: number; height: number } | null>(null);
  const moveStartRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number } | null>(null);
  const previewRequestRef = useRef(0);

  function slotsWithDraft() {
    return slots.map((item) => {
      if (item.file_name !== slot.file_name) return item;
      const adjustments = [...(item.adjustments || [])];
      while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
      adjustments[sourceIndex] = draft;
      return { ...item, adjustments };
    });
  }

  async function refreshPreview() {
    const requestId = ++previewRequestRef.current;
    setBusy(true);
    setError("");
    try {
      const result = await api.previewVipOrganizerSlot({
        session_id: sessionId,
        slots: slotsWithDraft(),
        product_info: productInfo,
        file_name: slot.file_name,
        platform
      });
      if (requestId === previewRequestRef.current) setRenderedPreview(result.preview_url);
    } catch (requestError: any) {
      if (requestId === previewRequestRef.current) setError(requestError.message || "当前输出预览生成失败");
    } finally {
      if (requestId === previewRequestRef.current) setBusy(false);
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(refreshPreview, 260);
    return () => window.clearTimeout(timer);
  }, [draft]);

  function displayedImageRect() {
    const stage = sourceStageRef.current;
    const image = sourceImageRef.current;
    if (!stage || !image?.naturalWidth || !image.naturalHeight) return null;
    const stageWidth = stage.clientWidth;
    const stageHeight = stage.clientHeight;
    const scale = Math.min(stageWidth / image.naturalWidth, stageHeight / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    return { left: (stageWidth - width) / 2, top: (stageHeight - height) / 2, width, height };
  }

  function sourcePoint(clientX: number, clientY: number) {
    const stage = sourceStageRef.current;
    const imageRect = displayedImageRect();
    if (!stage || !imageRect) return null;
    const bounds = stage.getBoundingClientRect();
    const x = Math.max(imageRect.left, Math.min(imageRect.left + imageRect.width, clientX - bounds.left));
    const y = Math.max(imageRect.top, Math.min(imageRect.top + imageRect.height, clientY - bounds.top));
    return { x, y, imageRect };
  }

  function finishCrop() {
    const selection = cropSelectionRef.current;
    const imageRect = displayedImageRect();
    cropStartRef.current = null;
    if (!selection || !imageRect || selection.width < 8 || selection.height < 8) return;
    setDraft((current) => ({
      ...current,
      crop_x: (selection.left - imageRect.left) / imageRect.width,
      crop_y: (selection.top - imageRect.top) / imageRect.height,
      crop_width: selection.width / imageRect.width,
      crop_height: selection.height / imageRect.height,
      zoom: 1,
      offset_x: 0,
      offset_y: 0
    }));
    setCropMode(false);
    cropSelectionRef.current = null;
    setCropSelection(null);
  }

  function changeZoom(delta: number) {
    setDraft((current) => ({ ...current, zoom: Math.max(0.5, Math.min(4, Math.round((current.zoom + delta) * 20) / 20)) }));
  }

  function reset() {
    setDraft({ ...DEFAULT_ADJUSTMENT });
    cropSelectionRef.current = null;
    setCropSelection(null);
    setCropMode(false);
  }

  return (
    <div className="slot-adjustment-modal" role="dialog" aria-modal="true" aria-label={`调整 ${slot.file_name}`} onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="slot-adjustment-dialog">
        <header>
          <div>
            <strong>{slot.file_name} · {slot.title}</strong>
            <span>{slot.file_name === "606.jpg" ? `正在调整来源 ${sourceIndex + 1}` : "当前输出位置独立调整"}</span>
          </div>
          <button type="button" className="icon-button" onClick={onClose} title="关闭"><X size={21} /></button>
        </header>

        <div className="slot-adjustment-workspace">
          <div className="slot-adjustment-source">
            <div className="slot-adjustment-heading">
              <strong>原始图片</strong>
              <span>{cropMode ? "拖动框选保留区域" : "点击“裁剪”后框选区域"}</span>
            </div>
            <div
              ref={sourceStageRef}
              className={`slot-source-stage${cropMode ? " is-cropping" : ""}`}
              onPointerDown={(event) => {
                if (!cropMode) return;
                const point = sourcePoint(event.clientX, event.clientY);
                if (!point) return;
                event.currentTarget.setPointerCapture(event.pointerId);
                cropStartRef.current = { x: point.x, y: point.y };
                const nextSelection = { left: point.x, top: point.y, width: 0, height: 0 };
                cropSelectionRef.current = nextSelection;
                setCropSelection(nextSelection);
              }}
              onPointerMove={(event) => {
                if (!cropMode || !cropStartRef.current) return;
                const point = sourcePoint(event.clientX, event.clientY);
                if (!point) return;
                const start = cropStartRef.current;
                const nextSelection = {
                  left: Math.min(start.x, point.x),
                  top: Math.min(start.y, point.y),
                  width: Math.abs(point.x - start.x),
                  height: Math.abs(point.y - start.y)
                };
                cropSelectionRef.current = nextSelection;
                setCropSelection(nextSelection);
              }}
              onPointerUp={finishCrop}
            >
              <img ref={sourceImageRef} src={sourceUrl} alt="原始素材" draggable={false} />
              {cropSelection && <div className="slot-crop-selection" style={cropSelection} />}
            </div>
          </div>

          <div className="slot-adjustment-result">
            <div className="slot-adjustment-heading">
              <strong>模板成品预览</strong>
              <span>拖动图片定位，滚轮缩放</span>
            </div>
            <div
              className={`slot-result-stage${busy ? " is-loading" : ""}`}
              onWheel={(event) => {
                event.preventDefault();
                changeZoom(event.deltaY < 0 ? 0.05 : -0.05);
              }}
              onPointerDown={(event) => {
                event.currentTarget.setPointerCapture(event.pointerId);
                moveStartRef.current = { x: event.clientX, y: event.clientY, offsetX: draft.offset_x, offsetY: draft.offset_y };
              }}
              onPointerMove={(event) => {
                const start = moveStartRef.current;
                if (!start) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                setDraft((current) => ({
                  ...current,
                  offset_x: Math.max(-1.5, Math.min(1.5, start.offsetX + (event.clientX - start.x) / bounds.width)),
                  offset_y: Math.max(-1.5, Math.min(1.5, start.offsetY + (event.clientY - start.y) / bounds.height))
                }));
              }}
              onPointerUp={() => { moveStartRef.current = null; }}
            >
              {renderedPreview ? <img src={renderedPreview} alt={`${slot.file_name} 模板预览`} draggable={false} /> : <FileImage size={38} />}
              {busy && <span className="slot-preview-loading"><LoaderCircle className="spin" size={20} />更新中</span>}
            </div>
          </div>
        </div>

        <div className="slot-adjustment-controls">
          <button type="button" className={cropMode ? "active-tool" : ""} onClick={() => {
            setCropMode((current) => !current);
            cropSelectionRef.current = null;
            setCropSelection(null);
          }}><Crop size={18} />裁剪</button>
          <button type="button" onClick={() => changeZoom(-0.1)}><ZoomOut size={18} />缩小</button>
          <span className="slot-zoom-value">{Math.round(draft.zoom * 100)}%</span>
          <button type="button" onClick={() => changeZoom(0.1)}><ZoomIn size={18} />放大</button>
          <button type="button" onClick={reset}><RotateCcw size={18} />恢复自动</button>
          <button type="button" disabled={busy} onClick={refreshPreview}><RefreshCw size={18} />重试预览</button>
          <span className="slot-drag-hint"><Move size={16} />位置 {Math.round(draft.offset_x * 100)} / {Math.round(draft.offset_y * 100)}</span>
          <button type="button" className="primary" disabled={busy || !renderedPreview} onClick={() => onSave(draft, renderedPreview)}><Save size={18} />保存调整</button>
        </div>
        {error && <div className="alert warning">{error}</div>}
      </section>
    </div>
  );
}

export default function VipOrganizer() {
  const sessionStorageKey = "vip-organizer-session-id";
  const [sessionId, setSessionId] = useState("");
  const sessionIdRef = useRef("");
  const sessionPromiseRef = useRef<Promise<{ session_id: string }> | null>(null);
  const pendingUploadsRef = useRef(0);
  const [products, setProducts] = useState<UploadItem[]>([]);
  const [models, setModels] = useState<UploadItem[]>([]);
  const [tags, setTags] = useState<UploadItem[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [platform, setPlatform] = useState<OrganizerPlatform>("vip");
  const [assets, setAssets] = useState<Record<string, any[]>>({ product: [], model: [], tag: [] });
  const [assetRoles, setAssetRoles] = useState<Record<number, string>>({});
  const [assetTags, setAssetTags] = useState<Record<number, string[]>>({});
  const [apiRoleNotes, setApiRoleNotes] = useState<Record<number, ApiRoleNote>>({});
  const [analysisConfigs, setAnalysisConfigs] = useState<any[]>([]);
  const [analysisConfigId, setAnalysisConfigId] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [slotPreviews, setSlotPreviews] = useState<Record<string, string>>({});
  const [previewBusy, setPreviewBusy] = useState(false);
  const [adjustmentEditor, setAdjustmentEditor] = useState<{ fileName: string; sourceIndex: number } | null>(null);
  const previewRequestRef = useRef(0);
  const previewQueueRef = useRef<Promise<void>>(Promise.resolve());
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

  function organizerProductInfo() {
    const dimensions = [info.product_length, info.product_width, info.product_height]
      .map((value) => value.trim())
      .filter(Boolean)
      .join(" × ");
    return { ...info, dimensions: dimensions ? `${dimensions} cm` : "" };
  }

  useEffect(() => {
    if (!sessionId || !slots.length) {
      setSlotPreviews({});
      return;
    }
    const requestId = ++previewRequestRef.current;
    const timer = window.setTimeout(() => {
      const payload = {
        session_id: sessionId,
        slots,
        product_info: organizerProductInfo(),
        platform
      };
      const queued = previewQueueRef.current.catch(() => undefined).then(async () => {
        if (requestId !== previewRequestRef.current) return;
        setPreviewBusy(true);
        let lastError: any = null;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            const result = await api.previewVipOrganizer(payload);
            if (requestId === previewRequestRef.current) {
              setSlotPreviews(result.previews || {});
              setMessage("");
            }
            return;
          } catch (error: any) {
            lastError = error;
            if (attempt === 0 && requestId === previewRequestRef.current) {
              await new Promise((resolve) => window.setTimeout(resolve, 500));
            }
          }
        }
        if (requestId === previewRequestRef.current) {
          setMessage(`成品预览暂时未更新，已保留上一次预览：${lastError?.message || "请求失败"}`);
        }
      }).finally(() => {
        if (requestId === previewRequestRef.current) setPreviewBusy(false);
      });
      previewQueueRef.current = queued;
    }, 520);
    return () => window.clearTimeout(timer);
  }, [sessionId, slots, info, platform]);

  useEffect(() => {
    api.getApiConfigs("text_analysis")
      .then((rows) => {
        const enabled = rows.filter((item: any) => item.enabled && item.api_type === "text_analysis");
        setAnalysisConfigs(enabled);
        const preferred = enabled.find((item: any) => item.is_default) || enabled[0];
        setAnalysisConfigId(preferred?.id || "");
      })
      .catch((error: any) => setMessage(error.message));
  }, []);

  useEffect(() => {
    let active = true;
    const previousSessionId = window.sessionStorage.getItem(sessionStorageKey) || undefined;
    const initialSession = api.startVipOrganizerSession(previousSessionId);
    sessionPromiseRef.current = initialSession;
    initialSession.then(
      (session) => {
        if (active) applyNewSession(session.session_id);
      },
      (error: any) => {
        if (active) setMessage(error.message);
      }
    );
    initialSession.then(
      () => { if (sessionPromiseRef.current === initialSession) sessionPromiseRef.current = null; },
      () => { if (sessionPromiseRef.current === initialSession) sessionPromiseRef.current = null; }
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const cleanupCurrentSession = () => {
      const currentSessionId = sessionIdRef.current;
      if (!currentSessionId) return;
      const body = JSON.stringify({ session_id: currentSessionId });
      const sent = navigator.sendBeacon(
        "/api/vip-organizer/session/cleanup",
        new Blob([body], { type: "application/json" })
      );
      if (!sent) {
        fetch("/api/vip-organizer/session/cleanup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
          keepalive: true
        }).catch(() => undefined);
      }
    };
    window.addEventListener("pagehide", cleanupCurrentSession);
    return () => window.removeEventListener("pagehide", cleanupCurrentSession);
  }, []);

  function applyNewSession(nextSessionId: string) {
    sessionIdRef.current = nextSessionId;
    window.sessionStorage.setItem(sessionStorageKey, nextSessionId);
    setSessionId(nextSessionId);
    setProducts([]);
    setModels([]);
    setTags([]);
    setSlots([]);
    setAssets({ product: [], model: [], tag: [] });
    setAssetRoles({});
    setAssetTags({});
    setApiRoleNotes({});
    setSlotPreviews({});
    setAdjustmentEditor(null);
  }

  async function ensureSession() {
    if (sessionIdRef.current) return sessionIdRef.current;
    if (!sessionPromiseRef.current) {
      sessionPromiseRef.current = api.startVipOrganizerSession()
        .then((session) => {
          applyNewSession(session.session_id);
          return session;
        })
        .finally(() => { sessionPromiseRef.current = null; });
    }
    return sessionPromiseRef.current.then((session) => session.session_id);
  }

  async function startNewSession() {
    setBusy(true);
    setMessage("");
    try {
      const session = await api.startVipOrganizerSession(sessionIdRef.current || undefined);
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
      setSlotPreviews({});
      const skipped = preSkipped + fileItems.length - uploaded.length;
      setMessage(skipped ? `已上传 ${uploaded.length} 张图片，自动跳过 ${skipped} 个不支持、损坏或未导入的文件。` : `已上传 ${uploaded.length} 张图片。`);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      pendingUploadsRef.current -= 1;
      if (pendingUploadsRef.current === 0) setBusy(false);
    }
  }

  async function analyze(rolesOverride?: Record<number, string>, platformOverride: OrganizerPlatform = platform) {
    if (!products.length) return setMessage("请先上传商品原图");
    setBusy(true);
    setMessage("");
    setSlotPreviews({});
    try {
      const result = await api.analyzeVipOrganizer({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        model_image_ids: models.map((item) => item.image_id),
        tag_image_ids: tags.map((item) => item.image_id),
        asset_roles: rolesOverride || assetRoles,
        asset_tags: assetTags,
        platform: platformOverride
      });
      setSlots(result.slots);
      setAssets(result.assets);
      setAdjustmentEditor(null);
      setMessage("已生成自动整理初稿。黄色或红色可信度项目需要重点确认。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function analyzeWithApi() {
    if (!products.length) return setMessage("请先上传商品原图");
    if (!analysisConfigId) return setMessage("请先在 API 设置中新增并启用图文分析 API");
    setBusy(true);
    setMessage("正在用所选图文分析 API 分析全部商品图，本次只调用一次……");
    try {
      const apiResult = await api.analyzeVipOrganizerWithApi({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        api_config_id: analysisConfigId
      });
      const nextRoles = apiResult.asset_roles as Record<number, string>;
      const nextTags = (apiResult.asset_tags || {}) as Record<number, string[]>;
      setAssetRoles(nextRoles);
      setAssetTags(nextTags);
      setApiRoleNotes(Object.fromEntries(apiResult.items.map((item: any) => [
        item.image_id,
        {
          role: item.role,
          confidence: item.confidence,
          reason: item.reason,
          tags: item.tags || []
        }
      ])));
      const result = await api.analyzeVipOrganizer({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        model_image_ids: models.map((item) => item.image_id),
        tag_image_ids: tags.map((item) => item.image_id),
        asset_roles: nextRoles,
        asset_tags: nextTags,
        platform
      });
      setSlots(result.slots);
      setAssets(result.assets);
      setSlotPreviews({});
      setAdjustmentEditor(null);
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
    setMessage("细节标签已修改，请点击“按标签重新整理”更新输出位置。");
  }

  function resetAssetTags(imageId: number) {
    setAssetTags((current) => {
      const next = { ...current };
      delete next[imageId];
      return next;
    });
  }

  function optionsFor(slot: Slot) {
    if (slot.kind === "model") return assets.model || [];
    if (slot.kind === "tag") return assets.tag || [];
    return assets.product || [];
  }

  function updateSlot(fileName: string, index: number, value: number) {
    setSlots((current) => current.map((slot) => {
      const linkedNames = platform === "jd" ? ["0-无logo.jpg", "1.jpg"] : ["1.jpg", "50.jpg"];
      const linkedModelSlot = linkedNames.includes(fileName);
      const shouldUpdate = slot.file_name === fileName || (linkedModelSlot && linkedNames.includes(slot.file_name));
      if (!shouldUpdate) return slot;
      const next = [...slot.image_ids];
      next[index] = value;
      const adjustments = [...(slot.adjustments || [])];
      while (adjustments.length <= index) adjustments.push({ ...DEFAULT_ADJUSTMENT });
      adjustments[index] = { ...DEFAULT_ADJUSTMENT };
      return {
        ...slot,
        image_ids: next.filter(Boolean),
        adjustments,
        confidence: 100,
        reason: linkedModelSlot ? `${linkedNames.join("与")}已同步使用同一张模特图` : "已由设计师人工确认",
      };
    }));
  }

  function openAdjustmentEditor(fileName: string, sourceIndex = 0) {
    const slot = slots.find((item) => item.file_name === fileName);
    if (!slot?.image_ids[sourceIndex]) {
      setMessage("当前输出位置还没有可调整的来源图片");
      return;
    }
    setAdjustmentEditor({ fileName, sourceIndex });
  }

  function saveSlotAdjustment(fileName: string, sourceIndex: number, adjustment: ImageAdjustment, previewUrl?: string) {
    setSlots((current) => current.map((slot) => {
      if (slot.file_name !== fileName) return slot;
      const adjustments = [...(slot.adjustments || [])];
      while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
      adjustments[sourceIndex] = normalizeAdjustment(adjustment);
      return { ...slot, adjustments };
    }));
    if (previewUrl) setSlotPreviews((current) => ({ ...current, [fileName]: previewUrl }));
    setAdjustmentEditor(null);
    setMessage(`${fileName} 的裁剪、缩放和位置已保存`);
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
      const result = await api.exportVipOrganizer({
        session_id: sessionId,
        slots,
        product_info: organizerProductInfo(),
        platform
      });
      const anchor = document.createElement("a");
      anchor.href = result.download_url;
      anchor.download = "";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      const platformName = platform === "jd" ? "京东" : "唯品会";
      setMessage(result.missing.length ? `ZIP 已下载，共 ${result.generated_count} 张，缺少：${result.missing.join("、")}` : `${platformName}套图 ZIP 已开始下载。`);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function changePlatform(nextPlatform: OrganizerPlatform) {
    if (nextPlatform === platform) return;
    setPlatform(nextPlatform);
    setSlotPreviews({});
    setAdjustmentEditor(null);
    if (products.length && slots.length) {
      await analyze(undefined, nextPlatform);
    }
  }

  const activeEditorSlot = adjustmentEditor
    ? slots.find((slot) => slot.file_name === adjustmentEditor.fileName)
    : undefined;
  const activeEditorAsset = activeEditorSlot && adjustmentEditor
    ? selectedAsset(activeEditorSlot.image_ids[adjustmentEditor.sourceIndex])
    : undefined;

  return (
    <section className="page organizer-page">
      <header className="page-header">
        <h1>唯品会自动化整理</h1>
        <p>上传商品原图和模特图，本地生成15张唯品会套图初稿；需要时可手动调用一次图文分析 API。</p>
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
          <UploadSection title="商品原图" hint="一次可多选；正面、侧面、背面、Logo、内里、透明图等" items={products} disabled={busy} onUpload={(files) => upload("product", files)} onPreview={setPreview} />
          <UploadSection title="模特图" hint="一次可多选；建议至少3张，系统分别用于主图和详情页" items={models} disabled={busy} onUpload={(files) => upload("model", files)} onPreview={setPreview} />
          <UploadSection title="吊牌图" hint="可选；用于801.jpg" items={tags} multiple={false} disabled={busy} onUpload={(files) => upload("tag", files)} onPreview={setPreview} />
        </div>
      </section>

      <section className="panel organizer-platform-panel">
        <div className="organizer-platform-switcher" aria-label="输出平台">
          <div>
            <strong>输出平台</strong>
            <span>不同平台使用独立的尺寸、模板、文件命名和ZIP目录</span>
          </div>
          <div className="organizer-platform-tabs" role="tablist" aria-label="选择输出平台">
            {ORGANIZER_PLATFORMS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={item.id === platform}
                className={item.id === platform ? "active" : ""}
                disabled={busy}
                onClick={() => changePlatform(item.id)}
              >
                <span>{item.label}</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      {slots.length > 0 && <>
        <section className="panel organizer-analysis-panel">
          <div className="organizer-analysis-header">
            <div><h2>2. 素材分析</h2><p>主类别决定图片用途，细节标签可以多选；低可信度结果建议人工确认或调用一次API。</p></div>
            <div className="organizer-analysis-toolbar">
              <label className="organizer-api-select">
                <span>图文分析 API</span>
                <select value={analysisConfigId} onChange={(event) => setAnalysisConfigId(Number(event.target.value) || "")}>
                  {!analysisConfigs.length && <option value="">暂无图文分析 API</option>}
                  {analysisConfigs.map((item) => <option value={item.id} key={item.id}>{item.config_name}{item.is_default ? "（默认）" : ""}</option>)}
                </select>
              </label>
              <button disabled={busy || !analysisConfigId} onClick={analyzeWithApi}><RefreshCw size={18} />API 分析素材（1次）</button>
              <button className="primary" disabled={busy} onClick={() => analyze()}>
                {busy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}按标签重新整理
              </button>
            </div>
          </div>
          <div className="organizer-analysis-grid">
            {(assets.product || []).map((asset: any) => {
              const apiNote = apiRoleNotes[asset.id];
              return <article key={asset.id} className="organizer-analysis-item">
                <button className="organizer-analysis-preview" onClick={() => setPreview(asset.original_url || asset.preview_url)}>
                  <img src={asset.preview_url} alt={asset.file_name} />
                </button>
                <div>
                  <strong title={asset.file_name}>{asset.file_name}</strong>
                  <small className={`organizer-role-badge role-confidence-${asset.role_confidence >= 80 ? "high" : asset.role_confidence >= 60 ? "medium" : "low"}`} title={asset.role_reason}>
                    <span>自动 {asset.role_confidence}%</span>{ROLE_LABELS[asset.suggested_role] || "局部细节"}
                  </small>
                  <small className="organizer-auto-reason" title={asset.role_reason}>{asset.role_reason}</small>
                  {apiNote && <details className="api-role-details">
                    <summary>
                      <span>API 判断</span>
                      <strong>{apiNote.confidence}% · {ROLE_LABELS[apiNote.role] || apiNote.role}</strong>
                    </summary>
                    <dl>
                      <div><dt>主类别</dt><dd>{ROLE_LABELS[apiNote.role] || apiNote.role}</dd></div>
                      <div><dt>可信度</dt><dd>{apiNote.confidence}%</dd></div>
                      <div><dt>细节标签</dt><dd>{apiNote.tags.length ? apiNote.tags.map((tag) => TAG_LABELS[tag] || tag).join("、") : "无"}</dd></div>
                      <div><dt>判断理由</dt><dd>{apiNote.reason || "API 未提供理由"}</dd></div>
                    </dl>
                  </details>}
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
              </article>;
            })}
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
          <div className="section-title-row"><div><h2>4. 检查{platform === "jd" ? "京东7个输出位置" : "15个输出位置"}</h2><p>点击成品图或“调整”，可独立裁剪、缩放和移动来源图；模板、文字与原图曝光不会改变。</p></div>{previewBusy && <span className="organizer-preview-status"><LoaderCircle className="spin" size={16} />正在更新成品预览</span>}</div>
          <div className="organizer-slot-grid">
            {slots.map((slot) => {
              const count = slot.file_name === "606.jpg" ? 4 : 1;
              const editableSource = slot.kind !== "generated" || ["401.jpg", "5.jpg"].includes(slot.file_name);
              const renderedPreview = slotPreviews[slot.file_name];
              return <article className={`organizer-slot${slot.file_name === "606.jpg" ? " is-composite" : ""}`} key={slot.file_name}>
                <div className="organizer-slot-preview">
                  {renderedPreview
                    ? <button type="button" onClick={() => openAdjustmentEditor(slot.file_name, 0)} aria-label={`调整 ${slot.file_name} 最终成品`}><img src={renderedPreview} alt={`${slot.file_name} 最终成品`} /></button>
                    : <div className="generated-placeholder"><FileImage size={30} /><span>{previewBusy ? "正在套用模板" : "缺少素材"}</span></div>}
                </div>
                <div className="organizer-slot-body">
                  <div className="organizer-slot-title"><strong>{slot.file_name}</strong><span>{slot.title}</span><small>{slot.size}</small></div>
                  {count === 1 && <button
                      type="button"
                      className="organizer-adjust-output"
                      disabled={!slot.image_ids[0]}
                      onClick={() => openAdjustmentEditor(slot.file_name, 0)}
                    >
                      <Crop size={16} />调整成品
                    </button>}
                  {editableSource && Array.from({ length: count }).map((_, index) => {
                    return <label key={index}>{count > 1 ? `来源 ${index + 1}` : "来源图片"}
                      <span className="organizer-source-picker">
                        <select value={slot.image_ids[index] || ""} onChange={(event) => updateSlot(slot.file_name, index, Number(event.target.value))}>
                          <option value="">请选择</option>
                          {optionsFor(slot).map((asset: any) => <option value={asset.id} key={asset.id}>{assetOptionLabel(asset, slot.kind)}</option>)}
                        </select>
                        {count > 1 && <button type="button" disabled={!slot.image_ids[index]} onClick={() => openAdjustmentEditor(slot.file_name, index)} title={`调整来源 ${index + 1}`}>
                            <Crop size={16} />调整
                          </button>}
                      </span>
                    </label>;
                  })}
                  <div className={`confidence confidence-${slot.confidence >= 80 ? "high" : slot.confidence >= 50 ? "medium" : "low"}`}>
                    <span>可信度 {slot.confidence}%</span><p>{slot.reason}</p>
                  </div>
                </div>
              </article>;
            })}
          </div>
          <div className="organizer-export-bar">
            <span><CheckCircle2 size={18} />导出前请确认所有低可信度项目</span>
            <button className="primary" disabled={busy || previewBusy} onClick={exportZip}>{busy ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />}下载 ZIP</button>
          </div>
        </section>
      </>}

      {message && <div className="alert warning">{message}</div>}
      {preview && <div className="image-modal" role="dialog" aria-modal="true" aria-label="成品图片预览" onClick={() => setPreview(null)}>
        <button className="image-modal-close" type="button" onClick={() => setPreview(null)} aria-label="关闭预览"><X size={22} /></button>
        <img src={preview} alt="图片预览" onClick={(event) => event.stopPropagation()} />
      </div>}
      {adjustmentEditor && activeEditorSlot && activeEditorAsset && <SlotAdjustmentEditor
        sessionId={sessionId}
        slot={activeEditorSlot}
        sourceIndex={adjustmentEditor.sourceIndex}
        sourceUrl={activeEditorAsset.original_url || activeEditorAsset.preview_url}
        initialPreview={slotPreviews[activeEditorSlot.file_name]}
        slots={slots}
        productInfo={organizerProductInfo()}
        platform={platform}
        onClose={() => setAdjustmentEditor(null)}
        onSave={(adjustment, previewUrl) => saveSlotAdjustment(activeEditorSlot.file_name, adjustmentEditor.sourceIndex, adjustment, previewUrl)}
      />}
    </section>
  );
}
