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

type LogoColor = "black" | "white";

type Slot = {
  file_name: string;
  title: string;
  size: string;
  kind: string;
  image_ids: number[];
  confidence: number;
  reason: string;
  adjustments?: ImageAdjustment[];
  logo_color?: LogoColor;
};

type ImageAdjustment = {
  zoom: number;
  offset_x: number;
  offset_y: number;
  crop_x: number;
  crop_y: number;
  crop_width: number;
  crop_height: number;
  phone_scale?: number;
  phone_offset_x?: number;
  phone_offset_y?: number;
  phone_alignment?: "center" | "bottom";
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
  ["logo", "Logo细节"],
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
type PreviewFolder = "800" | "750";
const JD_SINGLE_FOLDER_FILES = new Set(["0-无logo.jpg", "透明.png"]);

const DEFAULT_ADJUSTMENT: ImageAdjustment = {
  zoom: 1,
  offset_x: 0,
  offset_y: 0,
  crop_x: 0,
  crop_y: 0,
  crop_width: 1,
  crop_height: 1,
  phone_scale: 1,
  phone_offset_x: 0,
  phone_offset_y: 0,
  phone_alignment: "center"
};

function normalizeAdjustment(value?: Partial<ImageAdjustment>): ImageAdjustment {
  return { ...DEFAULT_ADJUSTMENT, ...(value || {}) };
}

function isManuallyConfirmedSlot(slot: Slot) {
  return slot.reason.includes("已由设计师人工确认")
    || slot.reason.includes("已同步使用同一张模特图");
}

function mergeAnalyzedSlots(current: Slot[], incoming: Slot[]) {
  const currentByName = new Map(current.map((slot) => [slot.file_name, slot]));
  return incoming.map((nextSlot) => {
    const previous = currentByName.get(nextSlot.file_name);
    if (!previous) return nextSlot;

    const preserveManualSources = isManuallyConfirmedSlot(previous);
    const imageIds = preserveManualSources ? previous.image_ids : nextSlot.image_ids;
    const adjustments = imageIds.map((imageId, index) => {
      if (previous.image_ids[index] === imageId && previous.adjustments?.[index]) {
        return previous.adjustments[index];
      }
      return nextSlot.adjustments?.[index] || { ...DEFAULT_ADJUSTMENT };
    });

    return {
      ...nextSlot,
      image_ids: imageIds,
      adjustments,
      logo_color: previous.logo_color || nextSlot.logo_color,
      confidence: preserveManualSources ? previous.confidence : nextSlot.confidence,
      reason: preserveManualSources ? previous.reason : nextSlot.reason
    };
  });
}

function previewFoldersForSlot(slot: Slot, platform: OrganizerPlatform): PreviewFolder[] {
  if (platform !== "jd" || JD_SINGLE_FOLDER_FILES.has(slot.file_name)) return ["800"];
  return ["800", "750"];
}

function slotPreviewKey(platform: OrganizerPlatform, fileName: string, targetFolder: PreviewFolder = "800") {
  return platform === "jd" ? `${targetFolder}/${fileName}` : fileName;
}

function slotCanvasSize(size: string, platform?: OrganizerPlatform, targetFolder: PreviewFolder = "800") {
  if (platform === "jd") {
    return targetFolder === "750" ? { width: 750, height: 1000 } : { width: 800, height: 800 };
  }
  const match = size.match(/(\d+)\s*[×x]\s*(\d+)/i);
  return match ? { width: Number(match[1]), height: Number(match[2]) } : { width: 800, height: 800 };
}

function slotPreviewLayout(slot: Slot, platform: OrganizerPlatform, sourceIndex: number, targetFolder: PreviewFolder) {
  if (platform === "jd") {
    if (["0-无logo.jpg", "1.jpg", "3.jpg", "4.jpg"].includes(slot.file_name)) {
      return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
    }
    if (slot.file_name === "2.jpg") {
      return targetFolder === "750"
        ? { x: 100 / 750, y: 145 / 1000, width: 550 / 750, height: 755 / 1000, mode: "contain" as const }
        : { x: 100 / 800, y: 135 / 800, width: 600 / 800, height: 565 / 800, mode: "contain" as const };
    }
    if (slot.file_name === "5.jpg") {
      return targetFolder === "750"
        ? { x: 0.08, y: 0.16, width: 0.43, height: 0.58, mode: "contain" as const }
        : { x: 0.08, y: 0.16, width: 0.43, height: 0.58, mode: "contain" as const };
    }
    return { x: 0.15, y: 0.2125, width: 0.7, height: 0.675, mode: "contain" as const };
  }
  if (["1.jpg", "50.jpg"].includes(slot.file_name)) {
    return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
  }
  if (slot.file_name === "401.jpg") {
    return { x: 346 / 750, y: 258 / 665, width: 287 / 750, height: 200 / 665, mode: "contain" as const };
  }
  if (slot.file_name === "606.jpg") {
    const positions = [
      { x: 78 / 750, y: 195 / 750, width: 245 / 750, height: 170 / 750, mode: "contain" as const },
      { x: 427 / 750, y: 195 / 750, width: 245 / 750, height: 170 / 750, mode: "contain" as const },
      { x: 78 / 750, y: 500 / 750, width: 245 / 750, height: 180 / 750, mode: "contain" as const },
      { x: 427 / 750, y: 500 / 750, width: 245 / 750, height: 180 / 750, mode: "contain" as const }
    ];
    return positions[sourceIndex] || positions[0];
  }
  if (["4.jpg", "15.jpg"].includes(slot.file_name)) {
    return { x: 0, y: 0, width: 1, height: 1, mode: "contain" as const };
  }
  if (["601.jpg", "602.jpg", "603.jpg"].includes(slot.file_name)) {
    return { x: 56 / 750, y: 65 / 750, width: 638 / 750, height: 634 / 750, mode: "cover" as const };
  }
  if (["604.jpg", "605.jpg"].includes(slot.file_name)) {
    return { x: 52 / 750, y: 181 / 750, width: 643 / 750, height: 523 / 750, mode: "cover" as const };
  }
  if (slot.file_name === "801.jpg") {
    return { x: 90 / 750, y: 105 / 750, width: 570 / 750, height: 560 / 750, mode: "contain" as const };
  }
  return { x: 0.15, y: 0.2125, width: 0.7, height: 0.675, mode: "contain" as const };
}

function slotSafeAreaLayout(slot: Slot, platform: OrganizerPlatform, sourceIndex: number, targetFolder: PreviewFolder) {
  if (platform === "jd" && slot.file_name === "5.jpg") {
    return { x: 0.04, y: 0.12, width: 0.92, height: 0.72 };
  }
  return slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
}

function SlotSafeAreaOverlay({ slot, platform, sourceIndex, targetFolder }: {
  slot: Slot;
  platform: OrganizerPlatform;
  sourceIndex: number;
  targetFolder: PreviewFolder;
}) {
  const output = slotCanvasSize(slot.size, platform, targetFolder);
  const area = slotSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
  const x = area.x * output.width;
  const y = area.y * output.height;
  const width = area.width * output.width;
  const height = area.height * output.height;
  const labelY = y > 24 ? y - 8 : y + 20;

  return <svg
    className="slot-safe-area-overlay"
    viewBox={`0 0 ${output.width} ${output.height}`}
    preserveAspectRatio="xMidYMid meet"
    aria-hidden="true"
  >
    <rect x={x} y={y} width={width} height={height} />
    <text x={x + 8} y={labelY}>模板安全区</text>
  </svg>;
}

const livePreviewImageCache = new Map<string, HTMLImageElement>();
const livePreviewBoundsCache = new Map<string, { left: number; top: number; right: number; bottom: number }>();

function livePreviewImage(url: string) {
  const cached = livePreviewImageCache.get(url);
  if (cached) return cached;
  const image = new Image();
  image.decoding = "async";
  image.src = url;
  livePreviewImageCache.set(url, image);
  return image;
}

function livePreviewContentBounds(url: string, image: HTMLImageElement) {
  const cached = livePreviewBoundsCache.get(url);
  if (cached) return cached;
  const scratch = document.createElement("canvas");
  scratch.width = image.naturalWidth;
  scratch.height = image.naturalHeight;
  const context = scratch.getContext("2d", { willReadFrequently: true });
  if (!context) return { left: 0, top: 0, right: image.naturalWidth, bottom: image.naturalHeight };
  context.drawImage(image, 0, 0);
  const pixels = context.getImageData(0, 0, scratch.width, scratch.height).data;
  let left = scratch.width;
  let top = scratch.height;
  let right = 0;
  let bottom = 0;
  for (let y = 0; y < scratch.height; y += 2) {
    for (let x = 0; x < scratch.width; x += 2) {
      const index = (y * scratch.width + x) * 4;
      const alpha = pixels[index + 3];
      const darkest = Math.min(pixels[index], pixels[index + 1], pixels[index + 2]);
      if (alpha > 18 && darkest < 242) {
        left = Math.min(left, x);
        top = Math.min(top, y);
        right = Math.max(right, x + 2);
        bottom = Math.max(bottom, y + 2);
      }
    }
  }
  const bounds = right > left && bottom > top
    ? { left, top, right: Math.min(scratch.width, right), bottom: Math.min(scratch.height, bottom) }
    : { left: 0, top: 0, right: image.naturalWidth, bottom: image.naturalHeight };
  livePreviewBoundsCache.set(url, bounds);
  return bounds;
}

function LiveSlotPreview({ sourceUrl, templateUrl, slot, draft, platform, sourceIndex, targetFolder, productInfo }: {
  sourceUrl: string;
  templateUrl?: string;
  slot: Slot;
  draft: ImageAdjustment;
  platform: OrganizerPlatform;
  sourceIndex: number;
  targetFolder: PreviewFolder;
  productInfo: Record<string, string>;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const output = slotCanvasSize(slot.size, platform, targetFolder);
    canvas.width = output.width;
    canvas.height = output.height;
    const image = livePreviewImage(sourceUrl);
    const template = templateUrl && slot.file_name !== "5.jpg" ? livePreviewImage(templateUrl) : null;
    const phoneReference = platform === "jd" && slot.file_name === "5.jpg"
      ? livePreviewImage("/organizer-assets/iphone_reference.png")
      : null;
    const draw = () => {
      if (!image.complete || !image.naturalWidth) return;
      if (template && (!template.complete || !template.naturalWidth)) return;
      context.clearRect(0, 0, output.width, output.height);
      context.fillStyle = slot.file_name === "5.jpg" && platform === "jd" ? "#f3f3f3" : "#fff";
      context.fillRect(0, 0, output.width, output.height);
      if (template) context.drawImage(template, 0, 0, output.width, output.height);

      const area = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
      const areaX = area.x * output.width;
      const areaY = area.y * output.height;
      const areaWidth = area.width * output.width;
      const areaHeight = area.height * output.height;
      const usesProductCutout = platform === "jd"
        ? ["2.jpg", "5.jpg", "透明.png"].includes(slot.file_name)
        : ["2.jpg", "3.jpg", "30.png", "401.jpg", "606.jpg", "801.jpg"].includes(slot.file_name);
      const hasManualCrop = draft.crop_x > 0.0001
        || draft.crop_y > 0.0001
        || draft.crop_width < 0.9999
        || draft.crop_height < 0.9999;
      // A manual crop is expressed in full-image coordinates. Automatic content
      // bounds are only useful before the designer chooses an explicit region.
      const useContentBounds = usesProductCutout && !hasManualCrop;
      const bounds = useContentBounds ? livePreviewContentBounds(sourceUrl, image) : {
        left: 0,
        top: 0,
        right: image.naturalWidth,
        bottom: image.naturalHeight
      };
      const contentWidth = bounds.right - bounds.left;
      const contentHeight = bounds.bottom - bounds.top;
      const sourceX = Math.max(0, Math.min(image.naturalWidth - 1, bounds.left + draft.crop_x * contentWidth));
      const sourceY = Math.max(0, Math.min(image.naturalHeight - 1, bounds.top + draft.crop_y * contentHeight));
      const sourceWidth = Math.max(1, Math.min(image.naturalWidth - sourceX, draft.crop_width * contentWidth));
      const sourceHeight = Math.max(1, Math.min(image.naturalHeight - sourceY, draft.crop_height * contentHeight));
      const fitScale = area.mode === "cover"
        ? Math.max(areaWidth / sourceWidth, areaHeight / sourceHeight)
        : Math.min(areaWidth / sourceWidth, areaHeight / sourceHeight);
      const drawWidth = sourceWidth * fitScale * draft.zoom;
      const drawHeight = sourceHeight * fitScale * draft.zoom;
      const drawX = areaX + (areaWidth - drawWidth) / 2 + draft.offset_x * areaWidth;
      const drawY = areaY + (areaHeight - drawHeight) / 2 + draft.offset_y * areaHeight;

      context.fillStyle = "#fff";
      context.fillRect(areaX, areaY, areaWidth, areaHeight);
      context.save();
      context.beginPath();
      context.rect(areaX, areaY, areaWidth, areaHeight);
      context.clip();
      context.drawImage(
        image,
        sourceX,
        sourceY,
        sourceWidth,
        sourceHeight,
        drawX,
        drawY,
        drawWidth,
        drawHeight
      );
      context.restore();

      if (platform === "jd" && slot.file_name === "5.jpg") {
        const heightValue = Number.parseFloat(productInfo.product_height || "") || 75;
        const bodyHeight = Math.max(40, drawHeight * 0.68);
        const phoneHeight = Math.max(output.height * 0.095, Math.min(output.height * 0.34, bodyHeight * (163 / heightValue) * 0.7 * (draft.phone_scale || 1)));
        const pairWidth = phoneReference?.naturalWidth && phoneReference.naturalHeight
          ? phoneHeight * phoneReference.naturalWidth / phoneReference.naturalHeight
          : phoneHeight * 0.78;
        const phoneCenterX = output.width * 0.75 + (draft.phone_offset_x || 0) * output.width * 0.18;
        const phoneLeft = phoneCenterX - pairWidth / 2;
        const phoneTop = (draft.phone_alignment || "center") === "bottom"
          ? drawY + drawHeight - phoneHeight
          : drawY + (drawHeight - phoneHeight) / 2;
        const adjustedPhoneTop = phoneTop + (draft.phone_offset_y || 0) * output.height * 0.18;
        context.save();
        if (phoneReference?.complete && phoneReference.naturalWidth) {
          context.drawImage(phoneReference, phoneLeft, adjustedPhoneTop, pairWidth, phoneHeight);
        }
        const phoneRight = phoneLeft + pairWidth;
        const phoneRulerX = Math.min(output.width * 0.94, phoneRight + output.width * 0.055);
        context.strokeStyle = "#777";
        context.lineWidth = Math.max(1, output.width * 0.002);
        context.beginPath();
        context.moveTo(phoneRulerX, adjustedPhoneTop);
        context.lineTo(phoneRulerX, adjustedPhoneTop + phoneHeight);
        context.moveTo(phoneRulerX - 7, adjustedPhoneTop);
        context.lineTo(phoneRulerX + 7, adjustedPhoneTop);
        context.moveTo(phoneRulerX - 7, adjustedPhoneTop + phoneHeight);
        context.lineTo(phoneRulerX + 7, adjustedPhoneTop + phoneHeight);
        context.stroke();
        context.restore();
      }

      const safeArea = slotSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
      const safeX = safeArea.x * output.width;
      const safeY = safeArea.y * output.height;
      const safeWidth = safeArea.width * output.width;
      const safeHeight = safeArea.height * output.height;
      context.save();
      context.setLineDash([Math.max(5, output.width * 0.008), Math.max(4, output.width * 0.006)]);
      context.lineWidth = Math.max(2, output.width * 0.002);
      context.strokeStyle = "rgba(27, 91, 73, .78)";
      context.strokeRect(safeX, safeY, safeWidth, safeHeight);
      context.fillStyle = "rgba(27, 91, 73, .08)";
      context.fillRect(safeX, safeY, safeWidth, safeHeight);
      context.fillStyle = "rgba(27, 91, 73, .82)";
      context.font = `600 ${Math.max(13, Math.round(output.width * 0.018))}px sans-serif`;
      context.fillText("模板安全区（确认预览后不会写入成品）", safeX + 8, Math.max(18, safeY - 8));
      context.restore();
    };
    image.addEventListener("load", draw);
    if (template) template.addEventListener("load", draw);
    if (phoneReference) phoneReference.addEventListener("load", draw);
    draw();
    return () => {
      image.removeEventListener("load", draw);
      if (template) template.removeEventListener("load", draw);
      if (phoneReference) phoneReference.removeEventListener("load", draw);
    };
  }, [draft, platform, slot.file_name, slot.size, sourceIndex, sourceUrl, targetFolder, templateUrl]);

  return <canvas ref={canvasRef} aria-label={`${slot.file_name} 前端即时预览`} />;
}

function slotPreviewSignature(
  slot: Slot,
  productInfo: Record<string, string>,
  platform: OrganizerPlatform,
  targetFolder: PreviewFolder = "800"
) {
  return JSON.stringify({
    platform,
    targetFolder,
    slot,
    productInfo: slot.file_name === "401.jpg" || (platform === "jd" && slot.file_name === "5.jpg")
      ? productInfo
      : undefined
  });
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
  productInfo,
  platform,
  targetFolder,
  onClose,
  onSave
}: {
  sessionId: string;
  slot: Slot;
  sourceIndex: number;
  sourceUrl: string;
  initialPreview?: string;
  productInfo: Record<string, string>;
  platform: OrganizerPlatform;
  targetFolder: PreviewFolder;
  onClose: () => void;
  onSave: (adjustment: ImageAdjustment, logoColor: LogoColor, previewUrl?: string) => void;
}) {
  const initial = normalizeAdjustment(slot.adjustments?.[sourceIndex]);
  const supportsLogoColor = platform === "jd" && /^[1-5]\.jpg$/.test(slot.file_name);
  const [draft, setDraft] = useState<ImageAdjustment>(initial);
  const [logoColor, setLogoColor] = useState<LogoColor>(slot.logo_color === "white" ? "white" : "black");
  const [renderedPreview, setRenderedPreview] = useState(initialPreview || "");
  const [previewSynced, setPreviewSynced] = useState(Boolean(initialPreview));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cropMode, setCropMode] = useState(false);
  const isPhoneComparison = platform === "jd" && slot.file_name === "5.jpg";
  const [moveTarget, setMoveTarget] = useState<"product" | "phone">("product");
  const [cropSelection, setCropSelection] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const sourceStageRef = useRef<HTMLDivElement>(null);
  const resultStageRef = useRef<HTMLDivElement>(null);
  const sourceImageRef = useRef<HTMLImageElement>(null);
  const cropStartRef = useRef<{ x: number; y: number } | null>(null);
  const cropSelectionRef = useRef<{ left: number; top: number; width: number; height: number } | null>(null);
  const moveStartRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number; target: "product" | "phone" } | null>(null);
  const pendingMoveRef = useRef<ImageAdjustment | null>(null);
  const moveFrameRef = useRef<number | null>(null);
  const draftRef = useRef<ImageAdjustment>(initial);
  const logoColorRef = useRef<LogoColor>(slot.logo_color === "white" ? "white" : "black");
  const renderedPreviewRef = useRef(initialPreview || "");
  const draftVersionRef = useRef(0);
  const syncedVersionRef = useRef(initialPreview ? 0 : -1);
  const previewRequestRef = useRef(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const moveTargetRef = useRef<"product" | "phone">("product");

  moveTargetRef.current = moveTarget;

  function slotWithDraft(nextDraft: ImageAdjustment) {
    const adjustments = [...(slot.adjustments || [])];
    while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
    adjustments[sourceIndex] = nextDraft;
    return { ...slot, adjustments, logo_color: logoColorRef.current };
  }

  function cancelStalePreview() {
    if (!previewAbortRef.current) return;
    previewAbortRef.current.abort();
    previewAbortRef.current = null;
    previewRequestRef.current += 1;
    setBusy(false);
  }

  function applyDraft(nextDraft: ImageAdjustment) {
    cancelStalePreview();
    draftVersionRef.current += 1;
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    setPreviewSynced(false);
  }

  function changeLogoColor(nextColor: LogoColor) {
    if (logoColorRef.current === nextColor) return;
    cancelStalePreview();
    logoColorRef.current = nextColor;
    setLogoColor(nextColor);
    draftVersionRef.current += 1;
    setPreviewSynced(false);
  }

  async function refreshPreview(
    nextDraft: ImageAdjustment = draftRef.current,
    version = draftVersionRef.current
  ): Promise<string | undefined> {
    const requestId = ++previewRequestRef.current;
    previewAbortRef.current?.abort();
    const controller = new AbortController();
    previewAbortRef.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = await api.previewVipOrganizerSlot({
        session_id: sessionId,
        slots: [slotWithDraft(nextDraft)],
        product_info: productInfo,
        file_name: slot.file_name,
        platform,
        target_folder: targetFolder
      }, controller.signal);
      if (requestId === previewRequestRef.current) {
        renderedPreviewRef.current = result.preview_url;
        setRenderedPreview(result.preview_url);
        if (version === draftVersionRef.current) {
          syncedVersionRef.current = version;
          setPreviewSynced(true);
        }
      }
      return result.preview_url;
    } catch (requestError: any) {
      if (requestError?.name === "AbortError") return;
      if (requestId === previewRequestRef.current) setError(requestError.message || "当前输出预览生成失败");
      return undefined;
    } finally {
      if (requestId === previewRequestRef.current) {
        previewAbortRef.current = null;
        setBusy(false);
      }
    }
  }

  useEffect(() => {
    if (!initialPreview) void refreshPreview(initial, 0);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
      previewAbortRef.current?.abort();
      if (moveFrameRef.current !== null) window.cancelAnimationFrame(moveFrameRef.current);
    };
  }, []);

  useEffect(() => {
    const stage = resultStageRef.current;
    if (!stage) return;
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      const delta = event.deltaY < 0 ? 0.02 : -0.02;
      const current = draftRef.current;
      if (isPhoneComparison && moveTargetRef.current === "phone") {
        applyDraft({
          ...current,
          phone_scale: Math.max(0.5, Math.min(1.8, Math.round(((current.phone_scale || 1) + delta) * 100) / 100))
        });
      } else {
        applyDraft({
          ...current,
          zoom: Math.max(0.5, Math.min(4, Math.round((current.zoom + delta) * 100) / 100))
        });
      }
    };
    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [isPhoneComparison]);

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
    applyDraft({
      ...draftRef.current,
      crop_x: (selection.left - imageRect.left) / imageRect.width,
      crop_y: (selection.top - imageRect.top) / imageRect.height,
      crop_width: selection.width / imageRect.width,
      crop_height: selection.height / imageRect.height,
      zoom: 1,
      offset_x: 0,
      offset_y: 0
    });
    setCropMode(false);
    cropSelectionRef.current = null;
    setCropSelection(null);
  }

  function toggleCropMode() {
    const nextCropMode = !cropMode;
    setCropMode(nextCropMode);
    cropSelectionRef.current = null;
    setCropSelection(null);
    if (!nextCropMode) return;

    const current = draftRef.current;
    const hasManualCrop = current.crop_x > 0.0001
      || current.crop_y > 0.0001
      || current.crop_width < 0.9999
      || current.crop_height < 0.9999;
    if (!hasManualCrop) return;
    window.requestAnimationFrame(() => {
      const imageRect = displayedImageRect();
      if (!imageRect) return;
      const selection = {
        left: imageRect.left + current.crop_x * imageRect.width,
        top: imageRect.top + current.crop_y * imageRect.height,
        width: current.crop_width * imageRect.width,
        height: current.crop_height * imageRect.height
      };
      cropSelectionRef.current = selection;
      setCropSelection(selection);
    });
  }

  function changeZoom(delta: number) {
    const current = draftRef.current;
    if (isPhoneComparison && moveTarget === "phone") {
      applyDraft({
        ...current,
        phone_scale: Math.max(0.5, Math.min(1.8, Math.round(((current.phone_scale || 1) + delta) * 100) / 100))
      });
      return;
    }
    applyDraft({
      ...current,
      zoom: Math.max(0.5, Math.min(4, Math.round((current.zoom + delta) * 100) / 100))
    });
  }

  function reset() {
    logoColorRef.current = "black";
    setLogoColor("black");
    applyDraft({ ...DEFAULT_ADJUSTMENT });
    cropSelectionRef.current = null;
    setCropSelection(null);
    setCropMode(false);
  }

  function flushPendingMove() {
    if (moveFrameRef.current !== null) {
      window.cancelAnimationFrame(moveFrameRef.current);
      moveFrameRef.current = null;
    }
    const nextDraft = pendingMoveRef.current;
    pendingMoveRef.current = null;
    if (nextDraft) applyDraft(nextDraft);
  }

  function finishMove() {
    flushPendingMove();
    moveStartRef.current = null;
  }

  async function saveAdjustment() {
    const currentDraft = draftRef.current;
    let previewUrl = renderedPreviewRef.current;
    if (syncedVersionRef.current !== draftVersionRef.current) {
      previewUrl = await refreshPreview(currentDraft, draftVersionRef.current) || "";
    }
    if (previewUrl) onSave(currentDraft, logoColorRef.current, previewUrl);
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
              onPointerCancel={finishCrop}
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
              ref={resultStageRef}
              className={`slot-result-stage${busy ? " is-loading" : ""}`}
              onPointerDown={(event) => {
                event.currentTarget.setPointerCapture(event.pointerId);
                moveStartRef.current = {
                  x: event.clientX,
                  y: event.clientY,
                  offsetX: moveTarget === "phone" ? draft.phone_offset_x || 0 : draft.offset_x,
                  offsetY: moveTarget === "phone" ? draft.phone_offset_y || 0 : draft.offset_y,
                  target: moveTarget
                };
              }}
              onPointerMove={(event) => {
                const start = moveStartRef.current;
                if (!start) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                const nextOffsetX = Math.max(-1.5, Math.min(1.5, start.offsetX + (event.clientX - start.x) / bounds.width));
                const nextOffsetY = Math.max(-1.5, Math.min(1.5, start.offsetY + (event.clientY - start.y) / bounds.height));
                pendingMoveRef.current = start.target === "phone"
                  ? { ...draftRef.current, phone_offset_x: nextOffsetX, phone_offset_y: nextOffsetY }
                  : { ...draftRef.current, offset_x: nextOffsetX, offset_y: nextOffsetY };
                if (moveFrameRef.current === null) {
                  moveFrameRef.current = window.requestAnimationFrame(() => {
                    moveFrameRef.current = null;
                    const nextDraft = pendingMoveRef.current;
                    pendingMoveRef.current = null;
                    if (nextDraft) applyDraft(nextDraft);
                  });
                }
              }}
              onPointerUp={finishMove}
              onPointerCancel={finishMove}
            >
              <LiveSlotPreview
                sourceUrl={sourceUrl}
                templateUrl={renderedPreview || initialPreview}
                slot={slot}
                draft={draft}
                platform={platform}
                sourceIndex={sourceIndex}
                targetFolder={targetFolder}
                productInfo={productInfo}
              />
              {renderedPreview && <img
                className={`slot-exact-preview${previewSynced ? " is-visible" : ""}`}
                src={renderedPreview}
                alt={`${slot.file_name} 精确成品预览`}
                draggable={false}
              />}
              {previewSynced && <SlotSafeAreaOverlay
                slot={slot}
                platform={platform}
                sourceIndex={sourceIndex}
                targetFolder={targetFolder}
              />}
              <span className="slot-preview-loading">
                {busy ? <LoaderCircle className="spin" size={17} /> : <Move size={16} />}
                {busy ? "正在生成精确模板" : previewSynced ? "精确成品预览" : "前端即时预览"}
              </span>
            </div>
          </div>
        </div>

        <div className="slot-adjustment-controls">
          {isPhoneComparison && <div className="slot-phone-controls" role="group" aria-label="手机对比调整">
            <span>调整对象</span>
            <button type="button" className={moveTarget === "product" ? "active-tool" : ""} onClick={() => setMoveTarget("product")}>包</button>
            <button type="button" className={moveTarget === "phone" ? "active-tool" : ""} onClick={() => setMoveTarget("phone")}>手机和标线</button>
            <span>对齐</span>
            <button type="button" className={(draft.phone_alignment || "center") === "center" ? "active-tool" : ""} onClick={() => applyDraft({ ...draftRef.current, phone_alignment: "center" })}>中心同高</button>
            <button type="button" className={draft.phone_alignment === "bottom" ? "active-tool" : ""} onClick={() => applyDraft({ ...draftRef.current, phone_alignment: "bottom" })}>底部齐平</button>
          </div>}
          <button type="button" className={cropMode ? "active-tool" : ""} onClick={toggleCropMode}><Crop size={18} />裁剪</button>
          <button type="button" onClick={() => changeZoom(-0.05)}><ZoomOut size={18} />缩小</button>
          <span className="slot-zoom-value">{Math.round((isPhoneComparison && moveTarget === "phone" ? draft.phone_scale || 1 : draft.zoom) * 100)}%</span>
          <button type="button" onClick={() => changeZoom(0.05)}><ZoomIn size={18} />放大</button>
          {supportsLogoColor && <div className="slot-logo-color" role="group" aria-label="左上角 Logo 颜色">
            <span>Logo</span>
            <button type="button" className={logoColor === "black" ? "active-tool" : ""} onClick={() => changeLogoColor("black")}>
              <i className="logo-color-swatch black" />黑色
            </button>
            <button type="button" className={logoColor === "white" ? "active-tool" : ""} onClick={() => changeLogoColor("white")}>
              <i className="logo-color-swatch white" />白色
            </button>
          </div>}
          <button type="button" onClick={reset}><RotateCcw size={18} />恢复自动</button>
          <button
            type="button"
            className={!previewSynced ? "confirm-preview" : ""}
            disabled={busy || previewSynced}
            onClick={() => void refreshPreview(draftRef.current, draftVersionRef.current)}
          ><CheckCircle2 size={18} />{previewSynced ? "预览已确认" : "确认预览"}</button>
          <span className="slot-drag-hint"><Move size={16} />位置 {
            Math.round((isPhoneComparison && moveTarget === "phone" ? draft.phone_offset_x || 0 : draft.offset_x) * 100)
          } / {
            Math.round((isPhoneComparison && moveTarget === "phone" ? draft.phone_offset_y || 0 : draft.offset_y) * 100)
          }</span>
          {!previewSynced && <span className="slot-preview-pending">调整后请先确认预览</span>}
          <button type="button" className="primary" disabled={busy || !previewSynced} onClick={() => void saveAdjustment()}><Save size={18} />保存调整</button>
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
  const productsRef = useRef<UploadItem[]>([]);
  const modelsRef = useRef<UploadItem[]>([]);
  const tagsRef = useRef<UploadItem[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const [platform, setPlatform] = useState<OrganizerPlatform>("vip");
  const [assets, setAssets] = useState<Record<string, any[]>>({ product: [], model: [], tag: [] });
  const [assetRoles, setAssetRoles] = useState<Record<number, string>>({});
  const [assetTags, setAssetTags] = useState<Record<number, string[]>>({});
  const [manualAssetIds, setManualAssetIds] = useState<Set<number>>(() => new Set());
  const [apiRoleNotes, setApiRoleNotes] = useState<Record<number, ApiRoleNote>>({});
  const [analysisConfigs, setAnalysisConfigs] = useState<any[]>([]);
  const [analysisConfigId, setAnalysisConfigId] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [slotPreviews, setSlotPreviews] = useState<Record<string, string>>({});
  const [previewBusy, setPreviewBusy] = useState(false);
  const [adjustmentEditor, setAdjustmentEditor] = useState<{
    fileName: string;
    sourceIndex: number;
    targetFolder: PreviewFolder;
  } | null>(null);
  const previewRequestRef = useRef(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const slotPreviewSignaturesRef = useRef<Record<string, string>>({});
  const platformWorkspaceRef = useRef<Partial<Record<OrganizerPlatform, {
    slots: Slot[];
    previews: Record<string, string>;
    signatures: Record<string, string>;
  }>>>({});
  const jdBackgroundPreparedRef = useRef(false);
  const reanalyzeTimerRef = useRef<number | null>(null);
  const assetRolesRef = useRef<Record<number, string>>({});
  const assetTagsRef = useRef<Record<number, string[]>>({});
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
      previewAbortRef.current?.abort();
      setSlotPreviews({});
      return;
    }
    const productInfo = organizerProductInfo();
    const previewTargets = slots.flatMap((slot) =>
      previewFoldersForSlot(slot, platform).map((targetFolder) => ({
        slot,
        targetFolder,
        key: slotPreviewKey(platform, slot.file_name, targetFolder)
      }))
    );
    const signatures = Object.fromEntries(previewTargets.map((target) => [
      target.key,
      slotPreviewSignature(target.slot, productInfo, platform, target.targetFolder)
    ]));
    const changedTargets = previewTargets.filter((target) =>
      !slotPreviews[target.key]
      || slotPreviewSignaturesRef.current[target.key] !== signatures[target.key]
    );
    if (!changedTargets.length) return;

    const requestId = ++previewRequestRef.current;
    previewAbortRef.current?.abort();
    const controller = new AbortController();
    previewAbortRef.current = controller;
    const timer = window.setTimeout(async () => {
      if (requestId !== previewRequestRef.current) return;
      setPreviewBusy(true);
      let partialPreviewFailure = false;
      try {
        if (changedTargets.length > 5) {
          const folders = [...new Set(changedTargets.map((target) => target.targetFolder))];
          const groupedResults = await Promise.allSettled(folders.map(async (targetFolder) => {
            const result = await api.previewVipOrganizer({
              session_id: sessionId,
              slots,
              product_info: productInfo,
              platform,
              target_folder: targetFolder
            }, controller.signal);
            return Object.entries(result.previews || {}).flatMap(([fileName, previewUrl]) => (
              typeof previewUrl === "string"
                ? [[slotPreviewKey(platform, fileName, targetFolder), previewUrl] as const]
                : []
            ));
          }));
          if (requestId === previewRequestRef.current) {
            const successfulGroups = groupedResults
              .filter((result): result is PromiseFulfilledResult<(readonly [string, string])[]> => result.status === "fulfilled")
              .flatMap((result) => result.value);
            const successfulKeys = new Set(successfulGroups.map(([key]) => key));
            const previewEntries = Object.fromEntries(successfulGroups);
            setSlotPreviews((current) => ({
              ...current,
              ...previewEntries
            }));
            const workspace = platformWorkspaceRef.current[platform];
            platformWorkspaceRef.current[platform] = {
              slots,
              previews: { ...(workspace?.previews || {}), ...previewEntries },
              signatures: { ...(workspace?.signatures || {}), ...Object.fromEntries([...successfulKeys].filter((key) => signatures[key]).map((key) => [key, signatures[key]])) }
            };
            slotPreviewSignaturesRef.current = {
              ...slotPreviewSignaturesRef.current,
              ...Object.fromEntries(
                [...successfulKeys]
                  .filter((key) => signatures[key])
                  .map((key) => [key, signatures[key]])
              )
            };
            const failedCount = groupedResults.filter((result) => result.status === "rejected").length;
            if (failedCount) {
              partialPreviewFailure = true;
              setMessage(`${failedCount} 组预览暂未生成，其他预览已更新`);
            }
          }
        } else {
          const results = await Promise.allSettled(changedTargets.map(async (target) => {
            const result = await api.previewVipOrganizerSlot({
              session_id: sessionId,
              slots: [target.slot],
              product_info: productInfo,
              file_name: target.slot.file_name,
              platform,
              target_folder: target.targetFolder
            }, controller.signal);
            return [target.key, result.preview_url] as const;
          }));
          if (requestId === previewRequestRef.current) {
            const successfulResults = results
              .filter((result): result is PromiseFulfilledResult<readonly [string, string]> => result.status === "fulfilled" && typeof result.value[1] === "string")
              .map((result) => result.value);
            const successfulKeys = new Set(successfulResults.map(([key]) => key));
            const previewEntries = Object.fromEntries(successfulResults);
            setSlotPreviews((current) => ({ ...current, ...previewEntries }));
            const workspace = platformWorkspaceRef.current[platform];
            platformWorkspaceRef.current[platform] = {
              slots,
              previews: { ...(workspace?.previews || {}), ...previewEntries },
              signatures: { ...(workspace?.signatures || {}), ...Object.fromEntries([...successfulKeys].filter((key) => signatures[key]).map((key) => [key, signatures[key]])) }
            };
            slotPreviewSignaturesRef.current = {
              ...slotPreviewSignaturesRef.current,
              ...Object.fromEntries(
                [...successfulKeys]
                  .filter((key) => signatures[key])
                  .map((key) => [key, signatures[key]])
              )
            };
            const failedCount = results.length - successfulResults.length;
            if (failedCount) {
              partialPreviewFailure = true;
              setMessage(`${failedCount} 个预览暂未生成，其他预览已更新`);
            }
          }
        }
        if (requestId === previewRequestRef.current && !partialPreviewFailure) setMessage("");
      } catch (error: any) {
        if (error?.name === "AbortError") return;
        if (requestId === previewRequestRef.current) {
          setMessage(`成品预览暂时未更新，已保留上一次预览：${error?.message || "请求失败"}`);
        }
      } finally {
        if (requestId === previewRequestRef.current) setPreviewBusy(false);
      }
    }, 320);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [sessionId, slots, info, platform]);

  useEffect(() => {
    if (
      platform !== "vip"
      || previewBusy
      || !sessionId
      || !slots.length
      || !Object.keys(slotPreviews).length
      || !productsRef.current.length
      || jdBackgroundPreparedRef.current
      || platformWorkspaceRef.current.jd
    ) return;
    jdBackgroundPreparedRef.current = true;
    const timer = window.setTimeout(async () => {
      try {
        const productInfo = organizerProductInfo();
        const result = await api.analyzeVipOrganizer({
          session_id: sessionId,
          product_image_ids: productsRef.current.map((item) => item.image_id),
          model_image_ids: modelsRef.current.map((item) => item.image_id),
          tag_image_ids: tagsRef.current.map((item) => item.image_id),
          asset_roles: assetRolesRef.current,
          asset_tags: assetTagsRef.current,
          platform: "jd"
        });
        const folderResults = await Promise.allSettled((["800", "750"] as PreviewFolder[]).map(async (targetFolder) => {
          const previews = await api.previewVipOrganizer({
            session_id: sessionId,
            slots: result.slots,
            product_info: productInfo,
            platform: "jd",
            target_folder: targetFolder
          });
          return Object.entries(previews.previews || {}).flatMap(([fileName, previewUrl]) => (
            typeof previewUrl === "string"
              ? [[slotPreviewKey("jd", fileName, targetFolder), previewUrl] as const]
              : []
          ));
        }));
        const entries = folderResults
          .filter((item): item is PromiseFulfilledResult<(readonly [string, string])[]> => item.status === "fulfilled")
          .flatMap((item) => item.value);
        const signatures = Object.fromEntries(result.slots.flatMap((slot: Slot) =>
          previewFoldersForSlot(slot, "jd").map((targetFolder) => [
            slotPreviewKey("jd", slot.file_name, targetFolder),
            slotPreviewSignature(slot, productInfo, "jd", targetFolder)
          ])
        ));
        platformWorkspaceRef.current.jd = {
          slots: result.slots,
          previews: Object.fromEntries(entries),
          signatures
        };
      } catch {
        jdBackgroundPreparedRef.current = false;
      }
    }, 450);
    return () => window.clearTimeout(timer);
  }, [platform, previewBusy, sessionId, slots, slotPreviews, info]);

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
    productsRef.current = [];
    modelsRef.current = [];
    tagsRef.current = [];
    setSlots([]);
    setAssets({ product: [], model: [], tag: [] });
    setAssetRoles({});
    setAssetTags({});
    setManualAssetIds(new Set());
    assetRolesRef.current = {};
    assetTagsRef.current = {};
    setApiRoleNotes({});
    setSlotPreviews({});
    slotPreviewSignaturesRef.current = {};
    platformWorkspaceRef.current = {};
    jdBackgroundPreparedRef.current = false;
    if (reanalyzeTimerRef.current !== null) {
      window.clearTimeout(reanalyzeTimerRef.current);
      reanalyzeTimerRef.current = null;
    }
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
      if (kind === "product") {
        productsRef.current = [...productsRef.current, ...uploaded];
        setProducts(productsRef.current);
      }
      if (kind === "model") {
        modelsRef.current = [...modelsRef.current, ...uploaded];
        setModels(modelsRef.current);
      }
      if (kind === "tag") {
        tagsRef.current = uploaded.slice(-1);
        setTags(tagsRef.current);
      }
      const skipped = preSkipped + fileItems.length - uploaded.length;
      const canAutoAnalyze = productsRef.current.length > 0 && modelsRef.current.length > 0;
      if (canAutoAnalyze) {
        setMessage(kind === "tag" ? "吊牌已上传，正在增量刷新吊牌相关输出……" : "商品图和模特图已到齐，正在自动整理初稿……");
        await analyze(
          undefined,
          platform,
          undefined,
          {
            products: productsRef.current,
            models: modelsRef.current,
            tags: tagsRef.current
          },
          kind === "tag" && slots.length > 0 ? "tag" : undefined,
          false
        );
      } else {
        setMessage(skipped ? `已上传 ${uploaded.length} 张图片，自动跳过 ${skipped} 个不支持、损坏或未导入的文件。` : `已上传 ${uploaded.length} 张图片。`);
      }
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      pendingUploadsRef.current -= 1;
      if (pendingUploadsRef.current === 0) setBusy(false);
    }
  }

  async function analyze(
    rolesOverride?: Record<number, string>,
    platformOverride: OrganizerPlatform = platform,
    tagsOverride?: Record<number, string[]>,
    collections?: { products: UploadItem[]; models: UploadItem[]; tags: UploadItem[] },
    incrementalKind?: "tag",
    manageBusy = true
  ) {
    const productItems = collections?.products || productsRef.current;
    const modelItems = collections?.models || modelsRef.current;
    const tagItems = collections?.tags || tagsRef.current;
    if (!productItems.length) return setMessage("请先上传商品原图");
    if (manageBusy) setBusy(true);
    setMessage("");
    try {
      const result = await api.analyzeVipOrganizer({
        session_id: sessionIdRef.current || sessionId,
        product_image_ids: productItems.map((item) => item.image_id),
        model_image_ids: modelItems.map((item) => item.image_id),
        tag_image_ids: tagItems.map((item) => item.image_id),
        asset_roles: rolesOverride || assetRolesRef.current,
        asset_tags: tagsOverride || assetTagsRef.current,
        platform: platformOverride
      });
      setSlots((current) => {
        const merged = mergeAnalyzedSlots(current, result.slots as Slot[]);
        if (incrementalKind !== "tag") return merged;
        const mergedByName = new Map(merged.map((slot) => [slot.file_name, slot]));
        return current.map((slot) => slot.kind === "tag" ? mergedByName.get(slot.file_name) || slot : slot);
      });
      if (platformOverride === "vip" && incrementalKind !== "tag") {
        delete platformWorkspaceRef.current.jd;
        jdBackgroundPreparedRef.current = false;
      }
      setAssets(result.assets);
      setAdjustmentEditor(null);
      setMessage(incrementalKind === "tag" ? "吊牌相关输出已增量更新，其他预览保持不变。" : "已生成自动整理初稿。黄色或红色可信度项目需要重点确认。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      if (manageBusy) setBusy(false);
    }
  }

  function scheduleReanalyze(
    nextRoles: Record<number, string>,
    nextTags: Record<number, string[]>
  ) {
    if (reanalyzeTimerRef.current !== null) window.clearTimeout(reanalyzeTimerRef.current);
    reanalyzeTimerRef.current = window.setTimeout(() => {
      reanalyzeTimerRef.current = null;
      void analyze(nextRoles, platform, nextTags);
    }, 220);
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
      assetRolesRef.current = nextRoles;
      assetTagsRef.current = nextTags;
      setAssetRoles(nextRoles);
      setAssetTags(nextTags);
      setManualAssetIds(new Set());
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
      setSlots((current) => mergeAnalyzedSlots(current, result.slots as Slot[]));
      setAssets(result.assets);
      setAdjustmentEditor(null);
      setMessage("API 已完成一次素材分类，并按固定标签重新整理。请检查低可信度位置。");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  function updateAssetRole(imageId: number, role: string) {
    const next = { ...assetRolesRef.current };
    if (role === "auto") delete next[imageId];
    else next[imageId] = role;
    if (role === "logo") {
      const currentTags = assetTagsRef.current[imageId] || [];
      const nextTags = Array.from(new Set([...currentTags, "logo"]));
      assetTagsRef.current = { ...assetTagsRef.current, [imageId]: nextTags };
      setAssetTags(assetTagsRef.current);
    }
    assetRolesRef.current = next;
    setAssetRoles(next);
    setManualAssetIds((current) => {
      const updated = new Set(current);
      if (role === "auto" && assetTagsRef.current[imageId] === undefined) updated.delete(imageId);
      else updated.add(imageId);
      return updated;
    });
    scheduleReanalyze(next, assetTagsRef.current);
    setMessage("固定标签已修改，正在只更新受影响的输出位置。");
  }

  function effectiveAssetTags(asset: any) {
    return assetTags[asset.id] ?? asset.suggested_tags ?? [];
  }

  function toggleAssetTag(asset: any, tag: string) {
    const selected = assetTagsRef.current[asset.id] ?? asset.suggested_tags ?? [];
    const nextTags = selected.includes(tag) ? selected.filter((item: string) => item !== tag) : [...selected, tag];
    const next = { ...assetTagsRef.current, [asset.id]: nextTags };
    assetTagsRef.current = next;
    setAssetTags(next);
    setManualAssetIds((current) => new Set(current).add(asset.id));
    scheduleReanalyze(assetRolesRef.current, next);
    setMessage("细节标签已修改，正在只更新受影响的输出位置。");
  }

  function resetAssetTags(imageId: number) {
    const next = { ...assetTagsRef.current };
    delete next[imageId];
    assetTagsRef.current = next;
    setAssetTags(next);
    if (!assetRolesRef.current[imageId]) {
      setManualAssetIds((current) => {
        const updated = new Set(current);
        updated.delete(imageId);
        return updated;
      });
    }
    scheduleReanalyze(assetRolesRef.current, next);
  }

  function optionsFor(slot: Slot) {
    if (slot.kind === "model") return assets.model || [];
    if (slot.kind === "tag") return assets.tag || [];
    return assets.product || [];
  }

  function updateSlot(fileName: string, index: number, value: number) {
    const linkedNames = platform === "jd" ? ["0-无logo.jpg", "1.jpg"] : ["1.jpg", "50.jpg"];
    const affectedNames = linkedNames.includes(fileName) ? linkedNames : [fileName];
    const fallbackPreview = selectedAsset(value)?.preview_url || "";
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    setSlotPreviews((current) => {
      const next = { ...current };
      affectedNames.forEach((affectedFileName) => {
        const preview800 = slotPreviewKey(platform, affectedFileName, "800");
        const preview750 = slotPreviewKey(platform, affectedFileName, "750");
        if (fallbackPreview) {
          next[preview800] = fallbackPreview;
          next[preview750] = fallbackPreview;
        } else {
          delete next[preview800];
          delete next[preview750];
        }
      });
      return next;
    });
    affectedNames.forEach((affectedFileName) => {
      delete slotPreviewSignaturesRef.current[slotPreviewKey(platform, affectedFileName, "800")];
      delete slotPreviewSignaturesRef.current[slotPreviewKey(platform, affectedFileName, "750")];
    });
    if (adjustmentEditor && affectedNames.includes(adjustmentEditor.fileName)) setAdjustmentEditor(null);
    setSlots((current) => current.map((slot) => {
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

  function openAdjustmentEditor(
    fileName: string,
    sourceIndex = 0,
    targetFolder: PreviewFolder = "800"
  ) {
    const slot = slots.find((item) => item.file_name === fileName);
    if (!slot?.image_ids[sourceIndex]) {
      setMessage("当前输出位置还没有可调整的来源图片");
      return;
    }
    setAdjustmentEditor({ fileName, sourceIndex, targetFolder });
  }

  function saveSlotAdjustment(
    fileName: string,
    sourceIndex: number,
    targetFolder: PreviewFolder,
    adjustment: ImageAdjustment,
    logoColor: LogoColor,
    previewUrl?: string
  ) {
    const currentSlot = slots.find((slot) => slot.file_name === fileName);
    if (!currentSlot) return;
    const adjustments = [...(currentSlot.adjustments || [])];
    while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
    adjustments[sourceIndex] = normalizeAdjustment(adjustment);
    const updatedSlot = { ...currentSlot, adjustments, logo_color: logoColor };
    setSlots((current) => current.map((slot) => slot.file_name === fileName ? updatedSlot : slot));
    if (previewUrl) {
      const previewKey = slotPreviewKey(platform, fileName, targetFolder);
      slotPreviewSignaturesRef.current[previewKey] = slotPreviewSignature(
        updatedSlot,
        organizerProductInfo(),
        platform,
        targetFolder
      );
      setSlotPreviews((current) => ({ ...current, [previewKey]: previewUrl }));
    }
    setAdjustmentEditor(null);
    setMessage(`${fileName} 的裁剪、缩放和位置已保存`);
  }

  function selectedAsset(id?: number) {
    return allAssets.find((item) => item.id === id);
  }

  function isManualAsset(imageId: number) {
    return manualAssetIds.has(imageId);
  }

  function assetOptionLabel(asset: any, kind: string) {
    if (kind === "model" || kind === "tag") return asset.file_name;
    const fixedRole = assetRoles[asset.id];
    const role = fixedRole || asset.suggested_role || "detail";
    const tags = effectiveAssetTags(asset);
    const tagText = tags.length ? `·${TAG_LABELS[tags[0]] || tags[0]}` : "";
    if (!isManualAsset(asset.id)) return `【${ROLE_LABELS[role] || "局部细节"}${tagText}】${asset.file_name}`;
    return `【人工·${ROLE_LABELS[fixedRole || role] || "局部细节"}${fixedRole ? "" : tagText}】${asset.file_name}`;
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
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    platformWorkspaceRef.current[platform] = {
      slots,
      previews: slotPreviews,
      signatures: { ...slotPreviewSignaturesRef.current }
    };
    setAdjustmentEditor(null);
    const cached = platformWorkspaceRef.current[nextPlatform];
    if (cached) {
      setSlots(cached.slots);
      setSlotPreviews({ ...cached.previews });
      slotPreviewSignaturesRef.current = { ...cached.signatures };
      setPlatform(nextPlatform);
      setMessage(`已切换到${nextPlatform === "jd" ? "京东" : "唯品会"}，直接恢复该平台上次预览。`);
      return;
    }
    setSlots([]);
    setSlotPreviews({});
    slotPreviewSignaturesRef.current = {};
    setPlatform(nextPlatform);
    setMessage(`正在准备${nextPlatform === "jd" ? "京东" : "唯品会"}预览……`);
    if (productsRef.current.length) {
      await analyze(undefined, nextPlatform, assetTagsRef.current);
    }
  }

  const activeEditorSlot = adjustmentEditor
    ? slots.find((slot) => slot.file_name === adjustmentEditor.fileName)
    : undefined;
  const activeEditorAsset = activeEditorSlot && adjustmentEditor
    ? selectedAsset(activeEditorSlot.image_ids[adjustmentEditor.sourceIndex])
    : undefined;
  const previewGroups = useMemo(() => {
    if (platform !== "jd") {
      return [{
        folder: "800" as PreviewFolder,
        label: "",
        description: "",
        slots
      }];
    }
    return [
      {
        folder: "800" as PreviewFolder,
        label: "800 文件夹",
        description: "800 × 800",
        slots
      },
      {
        folder: "750" as PreviewFolder,
        label: "750 文件夹",
        description: "750 × 1000",
        slots: slots.filter((slot) => !JD_SINGLE_FOLDER_FILES.has(slot.file_name))
      }
    ];
  }, [platform, slots]);

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
          <div className="organizer-platform-switcher" aria-label="输出平台">
            <div>
              <strong>输出平台</strong>
              <span>图片、标签和商品信息通用；这里选择最终输出模板、尺寸、文件名和 ZIP 目录</span>
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
          <div className="section-title-row"><div><h2>4. 检查{platform === "jd" ? "京东7个输出位置" : "15个输出位置"}</h2><p>点击成品图或“调整”，可独立裁剪、缩放和移动来源图；模板、文字与原图曝光不会改变。</p></div>{previewBusy && <span className="organizer-preview-status"><LoaderCircle className="spin" size={16} />正在更新成品预览</span>}</div>
          <div className="organizer-preview-groups">
            {previewGroups.map((group) => <section className="organizer-preview-group" key={group.folder}>
              {group.label && <header className="organizer-preview-group-header">
                <div><strong>{group.label}</strong><span>{group.description}</span></div>
                <small>{group.slots.length} 张</small>
              </header>}
              <div className="organizer-slot-grid">
                {group.slots.map((slot) => {
                  const count = slot.file_name === "606.jpg" ? 4 : 1;
                  const editableSource = slot.kind !== "generated" || ["401.jpg", "5.jpg"].includes(slot.file_name);
                  const previewKey = slotPreviewKey(platform, slot.file_name, group.folder);
                  const renderedPreview = slotPreviews[previewKey];
                  const outputSize = slotCanvasSize(slot.size, platform, group.folder);
                  return <article className={`organizer-slot${slot.file_name === "606.jpg" ? " is-composite" : ""}`} key={previewKey}>
                    <div
                      className="organizer-slot-preview"
                      style={{ aspectRatio: `${outputSize.width} / ${outputSize.height}` }}
                    >
                      {renderedPreview
                        ? <button type="button" onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder)} aria-label={`调整 ${slot.file_name} 最终成品`}><img src={renderedPreview} alt={`${slot.file_name} 最终成品`} onError={() => {
                            setSlotPreviews((current) => {
                              const next = { ...current };
                              delete next[previewKey];
                              return next;
                            });
                            delete slotPreviewSignaturesRef.current[previewKey];
                          }} /></button>
                        : <div className="generated-placeholder"><FileImage size={30} /><span>{previewBusy ? "正在套用模板" : "缺少素材"}</span></div>}
                    </div>
                    <div className="organizer-slot-body">
                      <div className="organizer-slot-title"><strong>{slot.file_name}</strong><span>{slot.title}</span><small>{outputSize.width}×{outputSize.height}</small></div>
                      {count === 1 && <button
                          type="button"
                          className="organizer-adjust-output"
                          disabled={!slot.image_ids[0]}
                          onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder)}
                        >
                          <Crop size={16} />调整成品
                        </button>}
                      {editableSource && Array.from({ length: count }).map((_, index) => {
                        const currentAsset = selectedAsset(slot.image_ids[index]);
                        return <label key={index}>{count > 1 ? `来源 ${index + 1}` : "来源图片"}
                          <span className="organizer-source-picker">
                            <select
                              className={currentAsset && isManualAsset(currentAsset.id) ? "is-manual-source" : ""}
                              value={slot.image_ids[index] || ""}
                              onChange={(event) => updateSlot(slot.file_name, index, Number(event.target.value))}
                            >
                              <option value="">请选择</option>
                              {optionsFor(slot).map((asset: any) => <option className={isManualAsset(asset.id) ? "manual-option" : ""} value={asset.id} key={asset.id}>{assetOptionLabel(asset, slot.kind)}</option>)}
                            </select>
                            {count > 1 && <button type="button" disabled={!slot.image_ids[index]} onClick={() => openAdjustmentEditor(slot.file_name, index, group.folder)} title={`调整来源 ${index + 1}`}>
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
            </section>)}
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
        key={`${platform}:${adjustmentEditor.targetFolder}:${activeEditorSlot.file_name}:${adjustmentEditor.sourceIndex}:${activeEditorAsset.id}`}
        sessionId={sessionId}
        slot={activeEditorSlot}
        sourceIndex={adjustmentEditor.sourceIndex}
        sourceUrl={activeEditorAsset.original_url || activeEditorAsset.preview_url}
        initialPreview={slotPreviews[slotPreviewKey(platform, activeEditorSlot.file_name, adjustmentEditor.targetFolder)]}
        productInfo={organizerProductInfo()}
        platform={platform}
        targetFolder={adjustmentEditor.targetFolder}
        onClose={() => setAdjustmentEditor(null)}
        onSave={(adjustment, logoColor, previewUrl) => saveSlotAdjustment(
          activeEditorSlot.file_name,
          adjustmentEditor.sourceIndex,
          adjustmentEditor.targetFolder,
          adjustment,
          logoColor,
          previewUrl
        )}
      />}
    </section>
  );
}
