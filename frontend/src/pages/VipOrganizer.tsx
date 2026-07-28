import { ClipboardPaste, Crop, Download, Eye, FileImage, LoaderCircle, Move, RefreshCw, RotateCcw, Save, Smartphone, UploadCloud, X, ZoomIn, ZoomOut } from "lucide-react";
import type { ClipboardEvent, DragEvent } from "react";
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
  folder_adjustments?: Partial<Record<PreviewFolder, ImageAdjustment[]>>;
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
  product_show_ruler?: boolean;
  phone_show_ruler?: boolean;
  product_ruler_group_scale?: number;
  product_ruler_group_offset_x?: number;
  product_ruler_group_offset_y?: number;
  product_ruler_base_left?: number;
  product_ruler_base_top?: number;
  product_ruler_base_right?: number;
  product_ruler_base_bottom?: number;
  length_ruler_scale?: number;
  length_ruler_offset_x?: number;
  length_ruler_offset_y?: number;
  height_ruler_scale?: number;
  height_ruler_offset_x?: number;
  height_ruler_offset_y?: number;
  width_ruler_scale?: number;
  width_ruler_offset_x?: number;
  width_ruler_offset_y?: number;
  phone_ruler_scale?: number;
  phone_ruler_offset_x?: number;
  phone_ruler_offset_y?: number;
};

type CropSelection = { left: number; top: number; width: number; height: number };
type AdjustmentTarget = "product" | "phone" | "length_ruler" | "height_ruler" | "width_ruler" | "phone_ruler";
type InfoMoveTarget = "product" | "product_rulers" | "length_ruler" | "height_ruler" | "width_ruler";

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
  phone_alignment: "bottom",
  product_show_ruler: true,
  phone_show_ruler: true,
  product_ruler_group_scale: 1,
  product_ruler_group_offset_x: 0,
  product_ruler_group_offset_y: 0,
  length_ruler_scale: 1,
  length_ruler_offset_x: 0,
  length_ruler_offset_y: 0,
  height_ruler_scale: 1,
  height_ruler_offset_x: 0,
  height_ruler_offset_y: 0,
  width_ruler_scale: 1,
  width_ruler_offset_x: 0,
  width_ruler_offset_y: 0,
  phone_ruler_scale: 1,
  phone_ruler_offset_x: 0,
  phone_ruler_offset_y: 0
};

const VIP_INFO_PRODUCT_BOX = { left: 294, top: 238, right: 687, bottom: 511 } as const;

function slotDisplayTitle(platform: OrganizerPlatform, fileName: string, fallback: string) {
  const vipTitles: Record<string, string> = {
    "1.jpg": "模特主图",
    "2.jpg": "半侧/全侧图",
    "3.jpg": "背面图",
    "4.jpg": "Logo图",
    "15.jpg": "内里图",
    "30.png": "透明图",
    "50.jpg": "模特竖图",
    "401.jpg": "产品信息",
    "601.jpg": "模特展示1",
    "602.jpg": "模特展示2",
    "603.jpg": "模特展示3",
    "604.jpg": "内里细节",
    "605.jpg": "Logo/五金细节",
    "606.jpg": "多角度图",
    "801.jpg": "吊牌图"
  };
  const jdTitles: Record<string, string> = {
    "0-无logo.jpg": "模特主图（无Logo）",
    "1.jpg": "模特主图",
    "2.jpg": "半侧产品图",
    "3.jpg": "Logo图",
    "4.jpg": "内里图",
    "5.jpg": "尺寸对比图",
    "透明.png": "透明图"
  };
  return (platform === "jd" ? jdTitles : vipTitles)[fileName] || fallback;
}

function normalizeAdjustment(value?: Partial<ImageAdjustment>): ImageAdjustment {
  return { ...DEFAULT_ADJUSTMENT, ...(value || {}) };
}

function targetScale(draft: ImageAdjustment, target: AdjustmentTarget) {
  if (target === "phone") return draft.phone_scale || 1;
  if (target === "length_ruler") return draft.length_ruler_scale || 1;
  if (target === "height_ruler") return draft.height_ruler_scale || 1;
  if (target === "width_ruler") return draft.width_ruler_scale || 1;
  if (target === "phone_ruler") return draft.phone_ruler_scale || 1;
  return draft.zoom;
}

function targetOffset(draft: ImageAdjustment, target: AdjustmentTarget) {
  if (target === "phone") return { x: draft.phone_offset_x || 0, y: draft.phone_offset_y || 0 };
  if (target === "length_ruler") return { x: draft.length_ruler_offset_x || 0, y: draft.length_ruler_offset_y || 0 };
  if (target === "height_ruler") return { x: draft.height_ruler_offset_x || 0, y: draft.height_ruler_offset_y || 0 };
  if (target === "width_ruler") return { x: draft.width_ruler_offset_x || 0, y: draft.width_ruler_offset_y || 0 };
  if (target === "phone_ruler") return { x: draft.phone_ruler_offset_x || 0, y: draft.phone_ruler_offset_y || 0 };
  return { x: draft.offset_x, y: draft.offset_y };
}

function withTargetScale(draft: ImageAdjustment, target: AdjustmentTarget, scale: number): ImageAdjustment {
  if (target === "phone") return { ...draft, phone_scale: scale };
  if (target === "length_ruler") return { ...draft, length_ruler_scale: scale };
  if (target === "height_ruler") return { ...draft, height_ruler_scale: scale };
  if (target === "width_ruler") return { ...draft, width_ruler_scale: scale };
  if (target === "phone_ruler") return { ...draft, phone_ruler_scale: scale };
  return { ...draft, zoom: scale };
}

function withTargetOffset(draft: ImageAdjustment, target: AdjustmentTarget, x: number, y: number): ImageAdjustment {
  if (target === "phone") return { ...draft, phone_offset_x: x, phone_offset_y: y };
  if (target === "length_ruler") return { ...draft, length_ruler_offset_x: x, length_ruler_offset_y: y };
  if (target === "height_ruler") return { ...draft, height_ruler_offset_x: x, height_ruler_offset_y: y };
  if (target === "width_ruler") return { ...draft, width_ruler_offset_x: x, width_ruler_offset_y: y };
  if (target === "phone_ruler") return { ...draft, phone_ruler_offset_x: x, phone_ruler_offset_y: y };
  return { ...draft, offset_x: x, offset_y: y };
}

function withLinkedProductOffset(
  draft: ImageAdjustment,
  x: number,
  y: number
): ImageAdjustment {
  return {
    ...draft,
    offset_x: x,
    offset_y: y
  };
}

function withLinkedProductScale(draft: ImageAdjustment, scale: number): ImageAdjustment {
  return {
    ...draft,
    zoom: scale
  };
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

    const forceAssignedSource = nextSlot.file_name === "401.jpg" || nextSlot.file_name === "5.jpg";
    const preserveManualSources = isManuallyConfirmedSlot(previous) && !forceAssignedSource;
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
      folder_adjustments: previous.folder_adjustments,
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

function slotForPreviewFolder(
  slot: Slot,
  platform: OrganizerPlatform,
  targetFolder: PreviewFolder
): Slot {
  if (platform !== "jd") return slot;
  const { folder_adjustments: folderAdjustments, ...baseSlot } = slot;
  return {
    ...baseSlot,
    adjustments: folderAdjustments?.[targetFolder] || slot.adjustments
  };
}

function slotsForPreviewFolder(
  slots: Slot[],
  platform: OrganizerPlatform,
  targetFolder: PreviewFolder
) {
  return slots.map((slot) => slotForPreviewFolder(slot, platform, targetFolder));
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
    if (["0-无logo.jpg", "1.jpg"].includes(slot.file_name)) {
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
    if (slot.file_name === "3.jpg") {
      return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
    }
    if (slot.file_name === "4.jpg") {
      return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
    }
    return { x: 0.15, y: 0.2125, width: 0.7, height: 0.675, mode: "contain" as const };
  }
  if (["1.jpg", "50.jpg"].includes(slot.file_name)) {
    return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
  }
  if (slot.file_name === "401.jpg") {
    return {
      x: VIP_INFO_PRODUCT_BOX.left / 750,
      y: VIP_INFO_PRODUCT_BOX.top / 665,
      width: (VIP_INFO_PRODUCT_BOX.right - VIP_INFO_PRODUCT_BOX.left) / 750,
      height: (VIP_INFO_PRODUCT_BOX.bottom - VIP_INFO_PRODUCT_BOX.top) / 665,
      mode: "contain" as const
    };
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
  if (slot.file_name === "4.jpg") {
    return { x: 0, y: 0, width: 1, height: 1, mode: "cover" as const };
  }
  if (slot.file_name === "15.jpg") {
    return { x: 0, y: 0, width: 1, height: 1, mode: "contain" as const };
  }
  if (["601.jpg", "602.jpg", "603.jpg"].includes(slot.file_name)) {
    return { x: 56 / 750, y: 65 / 750, width: 638 / 750, height: 634 / 750, mode: "cover" as const };
  }
  if (["604.jpg", "605.jpg"].includes(slot.file_name)) {
    return { x: 52 / 750, y: 181 / 750, width: 643 / 750, height: 523 / 750, mode: "cover" as const };
  }
  if (slot.file_name === "801.jpg") {
    return { x: 0, y: 0, width: 1, height: 1, mode: "contain" as const };
  }
  return { x: 0.15, y: 0.2125, width: 0.7, height: 0.675, mode: "contain" as const };
}

function slotSafeAreaLayout(slot: Slot, platform: OrganizerPlatform, sourceIndex: number, targetFolder: PreviewFolder) {
  if (platform === "jd" && slot.file_name === "2.jpg") {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  if (platform === "jd" && slot.file_name === "5.jpg") {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  return slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
}

function slotEditorSafeAreaLayout(slot: Slot, platform: OrganizerPlatform, sourceIndex: number, targetFolder: PreviewFolder) {
  if (platform === "vip" && slot.file_name === "401.jpg") {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  if (platform === "jd" && slot.file_name === "5.jpg") {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  if (platform === "vip" && ["604.jpg", "605.jpg"].includes(slot.file_name)) {
    return { x: 0.04, y: 0.18, width: 0.92, height: 0.78 };
  }
  if (platform === "jd" && slot.file_name === "2.jpg") {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  if (platform === "vip" && ["2.jpg", "3.jpg", "4.jpg"].includes(slot.file_name)) {
    return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  }
  if (slot.file_name.endsWith(".png")) return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  const template = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
  if (template.x <= 0.04 && template.y <= 0.04 && template.x + template.width >= 0.96 && template.y + template.height >= 0.96) {
    return template;
  }
  const padding = slot.file_name === "606.jpg"
    ? 0.06
    : ["604.jpg", "605.jpg"].includes(slot.file_name) ? 0.14 : 0.055;
  const left = Math.max(0.04, template.x - padding);
  const top = Math.max(0.04, template.y - padding);
  const right = Math.min(0.96, template.x + template.width + padding);
  const bottom = Math.min(0.96, template.y + template.height + padding);
  return { x: left, y: top, width: right - left, height: bottom - top };
}

function clampLayerOrigin(position: number, layerSize: number, minimum: number, maximum: number) {
  const available = Math.max(1, maximum - minimum);
  return layerSize <= available
    ? Math.max(minimum, Math.min(position, maximum - layerSize))
    : Math.max(maximum - layerSize, Math.min(position, minimum));
}

function adjustmentOffsetBasis(
  slot: Slot,
  platform: OrganizerPlatform,
  sourceIndex: number,
  targetFolder: PreviewFolder,
  target: AdjustmentTarget
) {
  const output = slotCanvasSize(slot.size, platform, targetFolder);
  if (target !== "product" || (platform === "jd" && slot.file_name === "5.jpg")) {
    return { x: output.width * 0.18, y: output.height * 0.18 };
  }
  const area = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
  return {
    x: Math.max(1, area.width * output.width),
    y: Math.max(1, area.height * output.height)
  };
}

function cropSelectionForTemplate(
  start: { x: number; y: number },
  point: { x: number; y: number },
  imageRect: CropSelection,
  aspectRatio: number | null
): CropSelection {
  const directionX = point.x < start.x ? -1 : 1;
  const directionY = point.y < start.y ? -1 : 1;
  const rawWidth = Math.max(1, Math.abs(point.x - start.x));
  const rawHeight = Math.max(1, Math.abs(point.y - start.y));
  if (aspectRatio === null) {
    return {
      left: Math.min(start.x, point.x),
      top: Math.min(start.y, point.y),
      width: rawWidth,
      height: rawHeight
    };
  }
  const ratio = Math.max(0.05, aspectRatio);
  let width: number;
  let height: number;
  if (rawWidth / rawHeight >= ratio) {
    width = rawWidth;
    height = width / ratio;
  } else {
    height = rawHeight;
    width = height * ratio;
  }
  const maxWidth = directionX > 0
    ? imageRect.left + imageRect.width - start.x
    : start.x - imageRect.left;
  const maxHeight = directionY > 0
    ? imageRect.top + imageRect.height - start.y
    : start.y - imageRect.top;
  const clampScale = Math.min(1, maxWidth / width, maxHeight / height);
  width = Math.max(1, width * clampScale);
  height = Math.max(1, height * clampScale);
  return {
    left: directionX > 0 ? start.x : start.x - width,
    top: directionY > 0 ? start.y : start.y - height,
    width,
    height
  };
}

function fitCropSelectionToTemplate(
  selection: CropSelection,
  imageRect: CropSelection,
  aspectRatio: number | null
): CropSelection {
  const centerX = selection.left + selection.width / 2;
  const centerY = selection.top + selection.height / 2;
  if (aspectRatio === null) {
    const width = Math.min(selection.width, imageRect.width);
    const height = Math.min(selection.height, imageRect.height);
    const left = Math.max(imageRect.left, Math.min(centerX - width / 2, imageRect.left + imageRect.width - width));
    const top = Math.max(imageRect.top, Math.min(centerY - height / 2, imageRect.top + imageRect.height - height));
    return { left, top, width, height };
  }
  const ratio = Math.max(0.05, aspectRatio);
  let width = selection.width;
  let height = selection.height;
  if (width / Math.max(1, height) > ratio) height = width / ratio;
  else width = height * ratio;
  const shrink = Math.min(1, imageRect.width / width, imageRect.height / height);
  width *= shrink;
  height *= shrink;
  const left = Math.max(imageRect.left, Math.min(centerX - width / 2, imageRect.left + imageRect.width - width));
  const top = Math.max(imageRect.top, Math.min(centerY - height / 2, imageRect.top + imageRect.height - height));
  return { left, top, width, height };
}

function SlotSafeAreaOverlay({ slot, platform, sourceIndex, targetFolder }: {
  slot: Slot;
  platform: OrganizerPlatform;
  sourceIndex: number;
  targetFolder: PreviewFolder;
}) {
  const output = slotCanvasSize(slot.size, platform, targetFolder);
  const template = slotSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
  const area = slotEditorSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
  const x = area.x * output.width;
  const y = area.y * output.height;
  const width = area.width * output.width;
  const height = area.height * output.height;
  const labelY = y > 24 ? y - 8 : y + 20;
  const templateDiffers = Math.abs(template.x - area.x) + Math.abs(template.y - area.y)
    + Math.abs(template.width - area.width) + Math.abs(template.height - area.height) > 0.001;
  const showTemplate = templateDiffers && !(platform === "vip" && slot.file_name === "401.jpg");

  return <svg
    className="slot-safe-area-overlay"
    viewBox={`0 0 ${output.width} ${output.height}`}
    preserveAspectRatio="xMidYMid meet"
    aria-hidden="true"
  >
    {showTemplate && <>
      <rect className="template-area" x={template.x * output.width} y={template.y * output.height} width={template.width * output.width} height={template.height * output.height} />
      <text className="template-label" x={template.x * output.width + 8} y={template.y * output.height + 20}>模板区域</text>
    </>}
    <rect className="adjustment-area" x={x} y={y} width={width} height={height} />
    <text x={x + 8} y={labelY}>调整安全区</text>
  </svg>;
}

const livePreviewImageCache = new Map<string, HTMLImageElement>();
const livePreviewBoundsCache = new Map<string, { left: number; top: number; right: number; bottom: number }>();
const livePreviewCutoutCache = new Map<string, HTMLCanvasElement>();
const livePreviewLightBorderCache = new Map<string, boolean>();
let liveHandleLiftCache = new WeakMap<HTMLCanvasElement, number>();
type PixelBounds = { left: number; top: number; right: number; bottom: number };
type LiveProductLayer = { canvas: HTMLCanvasElement; body: PixelBounds };
const liveJdProductLayerCache = new Map<string, LiveProductLayer>();

function clearLivePreviewCaches() {
  livePreviewImageCache.clear();
  livePreviewBoundsCache.clear();
  livePreviewCutoutCache.clear();
  livePreviewLightBorderCache.clear();
  liveHandleLiftCache = new WeakMap<HTMLCanvasElement, number>();
  liveJdProductLayerCache.clear();
}

function livePreviewImage(url: string) {
  const cached = livePreviewImageCache.get(url);
  if (cached) return cached;
  const image = new Image();
  image.decoding = "async";
  image.src = url;
  livePreviewImageCache.set(url, image);
  return image;
}

function livePreviewProductCutout(url: string, image: HTMLImageElement) {
  const cached = livePreviewCutoutCache.get(url);
  if (cached) return cached;
  const scale = Math.min(1, 1100 / Math.max(image.naturalWidth, image.naturalHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return canvas;
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
  const pixels = imageData.data;
  const cornerSize = Math.max(2, Math.round(Math.min(canvas.width, canvas.height) * 0.012));
  let red = 0;
  let green = 0;
  let blue = 0;
  let samples = 0;
  for (const origin of [[0, 0], [canvas.width - cornerSize, 0], [0, canvas.height - cornerSize], [canvas.width - cornerSize, canvas.height - cornerSize]]) {
    for (let y = origin[1]; y < origin[1] + cornerSize; y += 1) {
      for (let x = origin[0]; x < origin[0] + cornerSize; x += 1) {
        const offset = (y * canvas.width + x) * 4;
        if (pixels[offset + 3] < 16) continue;
        red += pixels[offset];
        green += pixels[offset + 1];
        blue += pixels[offset + 2];
        samples += 1;
      }
    }
  }
  const background = samples > 0
    ? [red / samples, green / samples, blue / samples]
    : [255, 255, 255];
  const visited = new Uint8Array(canvas.width * canvas.height);
  const queue = new Int32Array(canvas.width * canvas.height);
  let queueStart = 0;
  let queueEnd = 0;
  const isBackground = (pixelIndex: number) => {
    const offset = pixelIndex * 4;
    if (pixels[offset + 3] < 24) return true;
    const distance = Math.max(
      Math.abs(pixels[offset] - background[0]),
      Math.abs(pixels[offset + 1] - background[1]),
      Math.abs(pixels[offset + 2] - background[2])
    );
    const neutralWhite = background[0] > 235 && background[1] > 235 && background[2] > 235
      && pixels[offset] > 238 && pixels[offset + 1] > 238 && pixels[offset + 2] > 238;
    return distance <= 24 || neutralWhite;
  };
  const enqueue = (pixelIndex: number) => {
    if (visited[pixelIndex] || !isBackground(pixelIndex)) return;
    visited[pixelIndex] = 1;
    queue[queueEnd++] = pixelIndex;
  };
  for (let x = 0; x < canvas.width; x += 1) {
    enqueue(x);
    enqueue((canvas.height - 1) * canvas.width + x);
  }
  for (let y = 1; y < canvas.height - 1; y += 1) {
    enqueue(y * canvas.width);
    enqueue(y * canvas.width + canvas.width - 1);
  }
  while (queueStart < queueEnd) {
    const pixelIndex = queue[queueStart++];
    const x = pixelIndex % canvas.width;
    const y = Math.floor(pixelIndex / canvas.width);
    if (x > 0) enqueue(pixelIndex - 1);
    if (x + 1 < canvas.width) enqueue(pixelIndex + 1);
    if (y > 0) enqueue(pixelIndex - canvas.width);
    if (y + 1 < canvas.height) enqueue(pixelIndex + canvas.width);
  }
  for (let pixelIndex = 0; pixelIndex < visited.length; pixelIndex += 1) {
    if (visited[pixelIndex]) pixels[pixelIndex * 4 + 3] = 0;
  }
  context.putImageData(imageData, 0, 0);
  livePreviewCutoutCache.set(url, canvas);
  return canvas;
}

function livePreviewHasLightStudioBorder(url: string, image: HTMLImageElement) {
  const cached = livePreviewLightBorderCache.get(url);
  if (cached !== undefined) return cached;
  const canvas = document.createElement("canvas");
  const scale = Math.min(1, 96 / Math.max(image.naturalWidth, image.naturalHeight));
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return false;
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const borderSize = Math.max(2, Math.floor(Math.min(canvas.width, canvas.height) / 18));
  let matching = 0;
  let total = 0;
  for (let y = 0; y < canvas.height; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      if (x >= borderSize && x < canvas.width - borderSize && y >= borderSize && y < canvas.height - borderSize) continue;
      const offset = (y * canvas.width + x) * 4;
      const minimum = Math.min(pixels[offset], pixels[offset + 1], pixels[offset + 2]);
      const maximum = Math.max(pixels[offset], pixels[offset + 1], pixels[offset + 2]);
      if (minimum >= 232 && maximum - minimum <= 22) matching += 1;
      total += 1;
    }
  }
  const result = total > 0 && matching / total >= 0.78;
  livePreviewLightBorderCache.set(url, result);
  return result;
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

function longestTrueRun(values: boolean[]) {
  let bestStart = 0;
  let bestEnd = 0;
  let currentStart = -1;
  values.forEach((value, index) => {
    if (value && currentStart < 0) currentStart = index;
    if ((!value || index === values.length - 1) && currentStart >= 0) {
      const end = value && index === values.length - 1 ? index + 1 : index;
      if (end - currentStart > bestEnd - bestStart) {
        bestStart = currentStart;
        bestEnd = end;
      }
      currentStart = -1;
    }
  });
  return bestEnd > bestStart ? { start: bestStart, end: bestEnd } : null;
}

function liveProductBodyBounds(canvas: HTMLCanvasElement): PixelBounds {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return { left: 0, top: 0, right: canvas.width, bottom: canvas.height };
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const rowCounts = new Array<number>(canvas.height).fill(0);
  let fullLeft = canvas.width;
  let fullTop = canvas.height;
  let fullRight = 0;
  let fullBottom = 0;
  for (let y = 0; y < canvas.height; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      if (pixels[(y * canvas.width + x) * 4 + 3] <= 28) continue;
      rowCounts[y] += 1;
      fullLeft = Math.min(fullLeft, x);
      fullTop = Math.min(fullTop, y);
      fullRight = Math.max(fullRight, x + 1);
      fullBottom = Math.max(fullBottom, y + 1);
    }
  }
  if (fullRight <= fullLeft || fullBottom <= fullTop) {
    return { left: 0, top: 0, right: canvas.width, bottom: canvas.height };
  }
  const maxRowWidth = Math.max(...rowCounts);
  const broadRun = longestTrueRun(rowCounts.map((count) => count >= Math.max(10, Math.round(maxRowWidth * 0.65))));
  if (!broadRun) return { left: fullLeft, top: fullTop, right: fullRight, bottom: fullBottom };
  const paddingY = Math.max(1, Math.round((broadRun.end - broadRun.start) * 0.04));
  const bodyTop = Math.max(fullTop, broadRun.start - paddingY);
  const bodyBottom = Math.min(fullBottom, broadRun.end + paddingY);
  const columnCounts = new Array<number>(canvas.width).fill(0);
  for (let y = bodyTop; y < bodyBottom; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      if (pixels[(y * canvas.width + x) * 4 + 3] > 28) columnCounts[x] += 1;
    }
  }
  const columnRun = longestTrueRun(columnCounts.map((count) => count >= Math.max(2, Math.round((bodyBottom - bodyTop) * 0.14))));
  if (!columnRun) return { left: fullLeft, top: bodyTop, right: fullRight, bottom: bodyBottom };
  const paddingX = Math.max(1, Math.round((columnRun.end - columnRun.start) * 0.025));
  const body = {
    left: Math.max(fullLeft, columnRun.start - paddingX),
    top: bodyTop,
    right: Math.min(fullRight, columnRun.end + paddingX),
    bottom: bodyBottom
  };
  if (body.right - body.left < canvas.width * 0.18 || body.bottom - body.top < canvas.height * 0.12) {
    return { left: fullLeft, top: fullTop, right: fullRight, bottom: fullBottom };
  }
  return body;
}

function liveInfoMeasurementBounds(canvas: HTMLCanvasElement): PixelBounds {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return { left: 0, top: 0, right: canvas.width, bottom: canvas.height };
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const rowCounts = new Array<number>(canvas.height).fill(0);
  const rowSpans = new Array<number>(canvas.height).fill(0);
  const rowLongestSegments = new Array<number>(canvas.height).fill(0);
  let fullLeft = canvas.width;
  let fullTop = canvas.height;
  let fullRight = 0;
  let fullBottom = 0;
  for (let y = 0; y < canvas.height; y += 1) {
    let rowFirst = canvas.width;
    let rowLast = -1;
    let currentSegment = 0;
    for (let x = 0; x < canvas.width; x += 1) {
      if (pixels[(y * canvas.width + x) * 4 + 3] <= 28) {
        currentSegment = 0;
        continue;
      }
      rowCounts[y] += 1;
      rowFirst = Math.min(rowFirst, x);
      rowLast = x;
      currentSegment += 1;
      rowLongestSegments[y] = Math.max(rowLongestSegments[y], currentSegment);
      fullLeft = Math.min(fullLeft, x);
      fullTop = Math.min(fullTop, y);
      fullRight = Math.max(fullRight, x + 1);
      fullBottom = Math.max(fullBottom, y + 1);
    }
    if (rowLast >= rowFirst) rowSpans[y] = rowLast - rowFirst + 1;
  }
  if (fullRight <= fullLeft || fullBottom <= fullTop) {
    return { left: 0, top: 0, right: canvas.width, bottom: canvas.height };
  }
  const maxRowWidth = Math.max(...rowCounts);
  const fullWidth = Math.max(1, fullRight - fullLeft);
  const broadRun = longestTrueRun(rowCounts.map((count, index) => {
    const coreBody = count >= Math.max(8, Math.round(maxRowWidth * 0.65));
    const fill = rowSpans[index] > 0 ? count / rowSpans[index] : 0;
    const crescentShoulder = count >= Math.max(6, Math.round(maxRowWidth * 0.10))
      && rowSpans[index] >= Math.max(12, Math.round(fullWidth * 0.55))
      && rowLongestSegments[index] >= Math.max(6, Math.round(fullWidth * 0.07))
      && fill >= 0.12
      && fill <= 0.82;
    return coreBody || crescentShoulder;
  }));
  if (!broadRun) return { left: fullLeft, top: fullTop, right: fullRight, bottom: fullBottom };
  const columnCounts = new Array<number>(canvas.width).fill(0);
  for (let y = broadRun.start; y < broadRun.end; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      if (pixels[(y * canvas.width + x) * 4 + 3] > 28) columnCounts[x] += 1;
    }
  }
  const columnRun = longestTrueRun(columnCounts.map((count) => count >= Math.max(2, Math.round((broadRun.end - broadRun.start) * 0.08))));
  return {
    left: columnRun ? Math.max(fullLeft, columnRun.start) : fullLeft,
    top: Math.max(fullTop, broadRun.start),
    right: columnRun ? Math.min(fullRight, columnRun.end) : fullRight,
    bottom: Math.min(fullBottom, broadRun.end)
  };
}

function liveHandleVisualLift(canvas: HTMLCanvasElement) {
  const cached = liveHandleLiftCache.get(canvas);
  if (cached !== undefined) return cached;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return 0;
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  let fullTop = canvas.height;
  let fullBottom = 0;
  for (let y = 0; y < canvas.height; y += 1) {
    for (let x = 0; x < canvas.width; x += 1) {
      if (pixels[(y * canvas.width + x) * 4 + 3] <= 28) continue;
      fullTop = Math.min(fullTop, y);
      fullBottom = Math.max(fullBottom, y + 1);
    }
  }
  if (fullBottom <= fullTop) return 0;
  const body = liveInfoMeasurementBounds(canvas);
  const headroom = Math.max(0, body.top - fullTop);
  const bodyHeight = Math.max(1, body.bottom - body.top);
  const lift = headroom < Math.max(6, Math.round((fullBottom - fullTop) * 0.04))
    ? 0
    : Math.max(0, Math.min(1, (headroom / bodyHeight - 0.06) / 0.28));
  liveHandleLiftCache.set(canvas, lift);
  return lift;
}

function infoRulerGeometry(body: PixelBounds) {
  const rulerGap = 34;
  const left = Math.round(body.left + 4);
  const right = Math.round(body.right - 4);
  const bottom = Math.round(body.bottom - 9);
  const top = Math.round(body.top - 5);
  return {
    left,
    right,
    top,
    bottom,
    verticalX: Math.max(285, left - rulerGap),
    horizontalY: Math.min(535, bottom + rulerGap)
  };
}

function transformCanvasRulerSegment(
  start: { x: number; y: number },
  end: { x: number; y: number },
  scale: number,
  offsetX: number,
  offsetY: number,
  output: { width: number; height: number },
  origin?: { x: number; y: number }
) {
  const center = origin || { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  const move = { x: offsetX * output.width * 0.18, y: offsetY * output.height * 0.18 };
  const transform = (point: { x: number; y: number }) => ({
    x: center.x + (point.x - center.x) * scale + move.x,
    y: center.y + (point.y - center.y) * scale + move.y
  });
  return { start: transform(start), end: transform(end) };
}

function transformProductRulerSegment(
  start: { x: number; y: number },
  end: { x: number; y: number },
  productCenter: { x: number; y: number },
  draft: ImageAdjustment,
  rulerScale: number,
  rulerOffsetX: number,
  rulerOffsetY: number,
  output: { width: number; height: number }
) {
  const grouped = transformCanvasRulerSegment(
    start,
    end,
    draft.product_ruler_group_scale || 1,
    draft.product_ruler_group_offset_x || 0,
    draft.product_ruler_group_offset_y || 0,
    output,
    productCenter
  );
  return transformCanvasRulerSegment(
    grouped.start,
    grouped.end,
    rulerScale,
    rulerOffsetX,
    rulerOffsetY,
    output
  );
}

function infoWidthRulerGeometry(
  baseBody: PixelBounds,
  productCenter: { x: number; y: number },
  draft: ImageAdjustment,
  output: { width: number; height: number }
) {
  const rulerGap = 34;
  const anchorDirection = Math.hypot(22, 18);
  const start = {
    x: baseBody.right + 22 / anchorDirection * rulerGap,
    y: baseBody.bottom + 18 / anchorDirection * rulerGap
  };
  const end = { x: start.x + 51, y: start.y - 27 };
  const transform = (
    segmentStart: { x: number; y: number },
    segmentEnd: { x: number; y: number }
  ) => transformProductRulerSegment(
    segmentStart,
    segmentEnd,
    productCenter,
    draft,
    draft.width_ruler_scale || 1,
    draft.width_ruler_offset_x || 0,
    draft.width_ruler_offset_y || 0,
    output
  );
  const deltaX = end.x - start.x;
  const deltaY = end.y - start.y;
  const length = Math.max(1, Math.hypot(deltaX, deltaY));
  const perpendicular = { x: -deltaY / length * 9, y: deltaX / length * 9 };
  const segments = [
    [start, end],
    [
      { x: start.x - perpendicular.x, y: start.y - perpendicular.y },
      { x: start.x + perpendicular.x, y: start.y + perpendicular.y }
    ],
    [
      { x: end.x - perpendicular.x, y: end.y - perpendicular.y },
      { x: end.x + perpendicular.x, y: end.y + perpendicular.y }
    ]
  ];
  const transformedText = transform(
    { x: start.x + 8, y: start.y + 8 },
    { x: start.x + 8, y: start.y + 8 }
  ).start;
  return {
    segments: segments.map(([segmentStart, segmentEnd]) => {
      const transformed = transform(segmentStart, segmentEnd);
      return [transformed.start, transformed.end];
    }),
    text: transformedText
  };
}

function liveJdProductLayer(url: string, image: HTMLImageElement, draft: ImageAdjustment): LiveProductLayer {
  const cropKey = [draft.crop_x, draft.crop_y, draft.crop_width, draft.crop_height]
    .map((value) => value.toFixed(5))
    .join(":");
  const key = `${url}|${cropKey}`;
  const cached = liveJdProductLayerCache.get(key);
  if (cached) return cached;
  const cutout = livePreviewProductCutout(url, image);
  const cropLeft = Math.max(0, Math.min(cutout.width - 1, Math.round(draft.crop_x * cutout.width)));
  const cropTop = Math.max(0, Math.min(cutout.height - 1, Math.round(draft.crop_y * cutout.height)));
  const cropRight = Math.max(cropLeft + 1, Math.min(cutout.width, Math.round((draft.crop_x + draft.crop_width) * cutout.width)));
  const cropBottom = Math.max(cropTop + 1, Math.min(cutout.height, Math.round((draft.crop_y + draft.crop_height) * cutout.height)));
  const context = cutout.getContext("2d", { willReadFrequently: true });
  const pixels = context?.getImageData(cropLeft, cropTop, cropRight - cropLeft, cropBottom - cropTop).data;
  let left = cropRight - cropLeft;
  let top = cropBottom - cropTop;
  let right = 0;
  let bottom = 0;
  if (pixels) {
    const width = cropRight - cropLeft;
    const height = cropBottom - cropTop;
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        if (pixels[(y * width + x) * 4 + 3] <= 18) continue;
        left = Math.min(left, x);
        top = Math.min(top, y);
        right = Math.max(right, x + 1);
        bottom = Math.max(bottom, y + 1);
      }
    }
  }
  if (right <= left || bottom <= top) {
    left = 0;
    top = 0;
    right = cropRight - cropLeft;
    bottom = cropBottom - cropTop;
  }
  const padding = Math.max(2, Math.round(Math.max(right - left, bottom - top) * 0.012));
  left = Math.max(0, left - padding);
  top = Math.max(0, top - padding);
  right = Math.min(cropRight - cropLeft, right + padding);
  bottom = Math.min(cropBottom - cropTop, bottom + padding);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, right - left);
  canvas.height = Math.max(1, bottom - top);
  canvas.getContext("2d")?.drawImage(
    cutout,
    cropLeft + left,
    cropTop + top,
    canvas.width,
    canvas.height,
    0,
    0,
    canvas.width,
    canvas.height
  );
  const layer = { canvas, body: liveInfoMeasurementBounds(canvas) };
  liveJdProductLayerCache.set(key, layer);
  return layer;
}

function storedProductRulerBase(draft: ImageAdjustment): PixelBounds | null {
  const values = [
    draft.product_ruler_base_left,
    draft.product_ruler_base_top,
    draft.product_ruler_base_right,
    draft.product_ruler_base_bottom
  ];
  if (!values.every((value) => typeof value === "number" && Number.isFinite(value))) return null;
  const [left, top, right, bottom] = values as number[];
  return right > left && bottom > top ? { left, top, right, bottom } : null;
}

function liveInfoProductBody(
  sourceUrl: string,
  image: HTMLImageElement,
  draft: ImageAdjustment
): PixelBounds {
  const output = { width: 750, height: 665 };
  const area = {
    x: VIP_INFO_PRODUCT_BOX.left / 750,
    y: VIP_INFO_PRODUCT_BOX.top / 665,
    width: (VIP_INFO_PRODUCT_BOX.right - VIP_INFO_PRODUCT_BOX.left) / 750,
    height: (VIP_INFO_PRODUCT_BOX.bottom - VIP_INFO_PRODUCT_BOX.top) / 665
  };
  const areaX = area.x * output.width;
  const areaY = area.y * output.height;
  const areaWidth = area.width * output.width;
  const areaHeight = area.height * output.height;
  const hasManualCrop = draft.crop_x > 0.0001
    || draft.crop_y > 0.0001
    || draft.crop_width < 0.9999
    || draft.crop_height < 0.9999;
  const automaticBounds = livePreviewContentBounds(sourceUrl, image);
  const bounds = hasManualCrop ? {
    left: 0,
    top: 0,
    right: image.naturalWidth,
    bottom: image.naturalHeight
  } : automaticBounds;
  const contentWidth = Math.max(1, bounds.right - bounds.left);
  const contentHeight = Math.max(1, bounds.bottom - bounds.top);
  const sourceX = Math.max(0, Math.min(image.naturalWidth - 1, bounds.left + draft.crop_x * contentWidth));
  const sourceY = Math.max(0, Math.min(image.naturalHeight - 1, bounds.top + draft.crop_y * contentHeight));
  const sourceWidth = Math.max(1, Math.min(image.naturalWidth - sourceX, draft.crop_width * contentWidth));
  const sourceHeight = Math.max(1, Math.min(image.naturalHeight - sourceY, draft.crop_height * contentHeight));
  const productLayer = livePreviewProductCutout(sourceUrl, image);
  const croppedProductLayer = hasManualCrop ? liveJdProductLayer(sourceUrl, image, draft) : null;
  const drawSourceScaleX = productLayer.width / Math.max(1, image.naturalWidth);
  const drawSourceScaleY = productLayer.height / Math.max(1, image.naturalHeight);
  const drawSourceX = croppedProductLayer ? 0 : sourceX * drawSourceScaleX;
  const drawSourceY = croppedProductLayer ? 0 : sourceY * drawSourceScaleY;
  const drawSourceWidth = croppedProductLayer ? croppedProductLayer.canvas.width : sourceWidth * drawSourceScaleX;
  const drawSourceHeight = croppedProductLayer ? croppedProductLayer.canvas.height : sourceHeight * drawSourceScaleY;
  const fitScale = Math.min(areaWidth / drawSourceWidth, areaHeight / drawSourceHeight);
  const drawWidth = drawSourceWidth * fitScale * draft.zoom;
  const drawHeight = drawSourceHeight * fitScale * draft.zoom;
  let drawX = areaX + (areaWidth - drawWidth) / 2 + draft.offset_x * areaWidth;
  let drawY = areaY + (areaHeight - drawHeight) / 2 + draft.offset_y * areaHeight;
  const layerBounds = croppedProductLayer?.body || liveInfoMeasurementBounds(productLayer);

  if (!hasManualCrop) {
    drawX += (drawSourceX + drawSourceWidth / 2 - (layerBounds.left + layerBounds.right) / 2)
      * drawWidth / drawSourceWidth;
    drawY += (drawSourceY + drawSourceHeight / 2 - (layerBounds.top + layerBounds.bottom) / 2)
      * drawHeight / drawSourceHeight;
  }

  const measuredLeft = Math.max(drawSourceX, layerBounds.left);
  const measuredTop = Math.max(drawSourceY, layerBounds.top);
  const measuredRight = Math.min(drawSourceX + drawSourceWidth, layerBounds.right);
  const measuredBottom = Math.min(drawSourceY + drawSourceHeight, layerBounds.bottom);
  if (measuredRight <= measuredLeft || measuredBottom <= measuredTop) {
    return { left: drawX, top: drawY, right: drawX + drawWidth, bottom: drawY + drawHeight };
  }
  return {
    left: drawX + (measuredLeft - drawSourceX) / drawSourceWidth * drawWidth,
    top: drawY + (measuredTop - drawSourceY) / drawSourceHeight * drawHeight,
    right: drawX + (measuredRight - drawSourceX) / drawSourceWidth * drawWidth,
    bottom: drawY + (measuredBottom - drawSourceY) / drawSourceHeight * drawHeight
  };
}

function jdProductShapeProfile(bodyWidth: number, bodyHeight: number, physicalRatio?: number) {
  const visualRatio = bodyWidth / Math.max(1, bodyHeight);
  const ratio = physicalRatio && physicalRatio >= 0.2 && physicalRatio <= 5
    ? visualRatio * 0.20 + physicalRatio * 0.80
    : visualRatio;
  if (ratio < 0.55) return { shape: "very_tall", maxWidth: 0.25, maxHeight: 0.44, preferredWidth: 0.24 };
  if (ratio < 0.78) return { shape: "tall", maxWidth: 0.30, maxHeight: 0.42, preferredWidth: 0.29 };
  if (ratio > 2) return { shape: "very_wide", maxWidth: 0.43, maxHeight: 0.26, preferredWidth: 0.39 };
  if (ratio > 1.35) return { shape: "wide", maxWidth: 0.40, maxHeight: 0.31, preferredWidth: 0.36 };
  return { shape: "balanced", maxWidth: 0.35, maxHeight: 0.37, preferredWidth: 0.34 };
}

function jdProductGeometry(
  output: { width: number; height: number },
  layer: LiveProductLayer,
  draft: ImageAdjustment,
  productInfo: Record<string, string>,
  enforceLogoClearance = true,
  clampToSafe = true
) {
  const bodyWidth = Math.max(1, layer.body.right - layer.body.left);
  const bodyHeight = Math.max(1, layer.body.bottom - layer.body.top);
  const lengthMm = Number.parseFloat(productInfo.product_length || "") || 200;
  const providedHeight = Number.parseFloat(productInfo.product_height || "");
  const heightMm = Number.isFinite(providedHeight) && providedHeight > 0
    ? providedHeight
    : Math.max(60, lengthMm * bodyHeight / bodyWidth);
  const profile = jdProductShapeProfile(bodyWidth, bodyHeight, lengthMm / Math.max(1, heightMm));
  const preferredBodyWidth = output.width * profile.preferredWidth * Math.max(0.82, Math.min(1.08, lengthMm / 205));
  const baseScale = Math.min(
    output.width * profile.maxWidth / bodyWidth,
    output.height * profile.maxHeight / bodyHeight,
    output.width * 0.46 / layer.canvas.width,
    output.height * 0.60 / layer.canvas.height,
    preferredBodyWidth / bodyWidth
  );
  const scale = baseScale * draft.zoom;
  const width = layer.canvas.width * scale;
  const height = layer.canvas.height * scale;
  const scaledBody = {
    left: layer.body.left * scale,
    top: layer.body.top * scale,
    right: layer.body.right * scale,
    bottom: layer.body.bottom * scale
  };
  let x = output.width * 0.34 + draft.offset_x * output.width * 0.18 - (scaledBody.left + scaledBody.right) / 2;
  let y = output.height * (output.height > output.width ? 0.70 : 0.73) + draft.offset_y * output.height * 0.18 - scaledBody.bottom;
  const safe = { left: output.width * 0.04, top: output.height * 0.04, right: output.width * 0.96, bottom: output.height * 0.96 };
  const clampOrigin = (position: number, layerSize: number, minimum: number, maximum: number) => layerSize <= maximum - minimum
    ? Math.max(minimum, Math.min(position, maximum - layerSize))
    : Math.max(maximum - layerSize, Math.min(position, minimum));
  if (clampToSafe) x = clampOrigin(x, width, safe.left, safe.right);
  let effectiveSafeTop = safe.top;
  if (enforceLogoClearance) {
    const logo = output.width === 750 && output.height === 1000
      ? { left: 56, top: 45 }
      : { left: 32, top: 38 };
    const horizontalGap = output.width * 0.02;
    const overlapsLogoColumns = x < logo.left + 190 + horizontalGap
      && x + width > logo.left - horizontalGap;
    if (overlapsLogoColumns) {
      const isTallHandleBag = bodyWidth / Math.max(1, bodyHeight) <= 1.15
        && liveHandleVisualLift(layer.canvas) >= 0.55;
      const clearance = output.width === 800 && output.height === 800
        ? isTallHandleBag ? 97 : output.height * 0.09
        : output.height * (isTallHandleBag ? 0.07 : 0.04);
      effectiveSafeTop = Math.max(effectiveSafeTop, logo.top + 60 + clearance);
    }
  }
  if (clampToSafe) y = clampOrigin(y, height, effectiveSafeTop, safe.bottom);
  return {
    x,
    y,
    width,
    height,
    body: {
      left: x + scaledBody.left,
      top: y + scaledBody.top,
      right: x + scaledBody.right,
      bottom: y + scaledBody.bottom
    },
    heightMm,
    baseBodyHeight: bodyHeight * baseScale,
    safe
  };
}

function jdComparisonProductGeometry(
  output: { width: number; height: number },
  layer: LiveProductLayer,
  draft: ImageAdjustment,
  productInfo: Record<string, string>
) {
  const hasManualLayout = draft.crop_x > 0.0001
    || draft.crop_y > 0.0001
    || draft.crop_width < 0.9999
    || draft.crop_height < 0.9999
    || Math.abs(draft.zoom - 1) > 0.0001
    || Math.abs(draft.offset_x) > 0.0001
    || Math.abs(draft.offset_y) > 0.0001;
  const baseGeometry = jdProductGeometry(output, layer, {
    ...draft,
    zoom: 1,
    offset_x: 0,
    offset_y: 0
  }, productInfo);
  if (!hasManualLayout) return { geometry: baseGeometry, baseGeometry };

  const rawGeometry = jdProductGeometry(output, layer, draft, productInfo, false, false);
  const baselineShiftX = (baseGeometry.body.left + baseGeometry.body.right) / 2 - output.width * 0.34;
  const baselineShiftY = baseGeometry.body.bottom
    - output.height * (output.height > output.width ? 0.70 : 0.73);
  return {
    geometry: {
      ...rawGeometry,
      x: rawGeometry.x + baselineShiftX,
      y: rawGeometry.y + baselineShiftY,
      body: {
        left: rawGeometry.body.left + baselineShiftX,
        top: rawGeometry.body.top + baselineShiftY,
        right: rawGeometry.body.right + baselineShiftX,
        bottom: rawGeometry.body.bottom + baselineShiftY
      }
    },
    baseGeometry
  };
}

function jdComparisonPhoneLayout(
  output: { width: number; height: number },
  geometry: {
    baseBodyHeight: number;
    heightMm: number;
    safe: PixelBounds;
  },
  baseGeometry: { body: PixelBounds },
  draft: ImageAdjustment,
  phoneReference: HTMLImageElement | null
) {
  const phoneRulerGap = Math.max(22, output.width * 0.035);
  const phoneLabelClearance = Math.max(40, output.width * 0.05);
  const phoneRightAllowance = phoneRulerGap + phoneLabelClearance;
  const phoneBottomAllowance = Math.max(28, output.height * 0.055);
  const phoneHeightForScale = (scale: number) => Math.max(
    output.height * 0.095,
    Math.min(output.height * 0.46, geometry.baseBodyHeight * (163 / geometry.heightMm) * scale)
  );
  const phoneWidthForHeight = (height: number) => phoneReference?.naturalWidth && phoneReference.naturalHeight
    ? height * phoneReference.naturalWidth / phoneReference.naturalHeight
    : height * 0.78;
  const basePhoneHeight = phoneHeightForScale(1);
  const basePhoneWidth = phoneWidthForHeight(basePhoneHeight);
  let basePhoneLeft = output.width * 0.75 - basePhoneWidth / 2;
  let basePhoneTop = (draft.phone_alignment || "bottom") === "bottom"
    ? baseGeometry.body.bottom - basePhoneHeight
    : (baseGeometry.body.top + baseGeometry.body.bottom - basePhoneHeight) / 2;
  basePhoneLeft = Math.max(
    geometry.safe.left,
    Math.min(basePhoneLeft, geometry.safe.right - basePhoneWidth - phoneRightAllowance)
  );
  basePhoneTop = Math.max(
    geometry.safe.top,
    Math.min(basePhoneTop, geometry.safe.bottom - basePhoneHeight - phoneBottomAllowance)
  );
  const basePhoneCenterX = basePhoneLeft + basePhoneWidth / 2;
  const basePhoneAnchorY = (draft.phone_alignment || "bottom") === "bottom"
    ? basePhoneTop + basePhoneHeight
    : basePhoneTop + basePhoneHeight / 2;
  const placePhone = (scale: number, offsetX: number, offsetY: number) => {
    const height = phoneHeightForScale(scale);
    const width = phoneWidthForHeight(height);
    const left = basePhoneCenterX + offsetX * output.width * 0.18 - width / 2;
    const top = basePhoneAnchorY + offsetY * output.height * 0.18
      - ((draft.phone_alignment || "bottom") === "bottom" ? height : height / 2);
    return { left, top, width, height };
  };
  return {
    phoneRulerGap,
    phone: placePhone(draft.phone_scale || 1, draft.phone_offset_x || 0, draft.phone_offset_y || 0),
    basePhone: placePhone(1, 0, 0)
  };
}

function drawAdjustmentGuide(
  context: CanvasRenderingContext2D,
  output: { width: number; height: number },
  slot: Slot,
  platform: OrganizerPlatform,
  sourceIndex: number,
  targetFolder: PreviewFolder
) {
  const safeArea = slotEditorSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
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
  context.fillText("调整安全区（确认预览后不会写入成品）", safeX + 8, Math.max(18, safeY - 8));
  context.restore();
}

function drawCanvasRuler(
  context: CanvasRenderingContext2D,
  start: { x: number; y: number },
  end: { x: number; y: number },
  label: string,
  output: { width: number; height: number },
  vertical = false,
  labelOnRight = false
) {
  const cap = Math.max(7, output.width * 0.011);
  context.save();
  context.strokeStyle = "#707070";
  context.fillStyle = "#707070";
  context.lineWidth = Math.max(1, output.width * 0.002);
  context.font = `500 ${Math.max(12, Math.round(output.width * 0.017))}px sans-serif`;
  context.textAlign = "center";
  context.beginPath();
  context.moveTo(start.x, start.y);
  context.lineTo(end.x, end.y);
  if (vertical) {
    context.moveTo(start.x - cap, start.y);
    context.lineTo(start.x + cap, start.y);
    context.moveTo(end.x - cap, end.y);
    context.lineTo(end.x + cap, end.y);
  } else {
    context.moveTo(start.x, start.y - cap);
    context.lineTo(start.x, start.y + cap);
    context.moveTo(end.x, end.y - cap);
    context.lineTo(end.x, end.y + cap);
  }
  context.stroke();
  if (vertical) {
    context.save();
    context.translate(start.x + (labelOnRight ? cap + 14 : -cap - 14), (start.y + end.y) / 2);
    context.rotate(-Math.PI / 2);
    context.fillText(label, 0, 0);
    context.restore();
  } else {
    context.fillText(label, (start.x + end.x) / 2, start.y + cap + 19);
  }
  context.restore();
}

function drawJdComparisonPreview(
  context: CanvasRenderingContext2D,
  output: { width: number; height: number },
  image: HTMLImageElement,
  sourceUrl: string,
  phoneReference: HTMLImageElement | null,
  logoReference: HTMLImageElement | null,
  draft: ImageAdjustment,
  productInfo: Record<string, string>
) {
  const layer = liveJdProductLayer(sourceUrl, image, draft);
  const { geometry, baseGeometry } = jdComparisonProductGeometry(output, layer, draft, productInfo);
  context.fillStyle = "#f3f3f3";
  context.fillRect(0, 0, output.width, output.height);
  if (logoReference?.complete && logoReference.naturalWidth) {
    const logoX = output.width === 750 && output.height === 1000 ? 56 : 32;
    const logoY = output.width === 750 && output.height === 1000 ? 45 : 38;
    context.drawImage(logoReference, logoX, logoY, 190, 60);
  }
  context.drawImage(layer.canvas, geometry.x, geometry.y, geometry.width, geometry.height);

  const rulerGap = Math.max(28, output.width * 0.045);
  const lengthValue = Number.parseFloat(productInfo.product_length || "");
  const heightValue = Number.parseFloat(productInfo.product_height || "");
  const lengthLabel = Number.isFinite(lengthValue) ? `${Math.round(lengthValue)}mm` : "200mm";
  const heightLabel = Number.isFinite(heightValue) ? `${Math.round(heightValue)}mm` : `${Math.round(geometry.heightMm)}mm`;
  const productRulerBody = storedProductRulerBase(draft) || geometry.body;
  const horizontalY = Math.min(output.height - 70, productRulerBody.bottom + rulerGap);
  const verticalX = Math.max(30, productRulerBody.left - rulerGap);
  const productRulerCenter = {
    x: (productRulerBody.left + productRulerBody.right) / 2,
    // JD5 keeps the bag body bottom fixed while the product is scaled.
    // Use that same anchor so the height ruler does not drift vertically.
    y: productRulerBody.bottom
  };
  const lengthRuler = transformProductRulerSegment(
    { x: productRulerBody.left, y: horizontalY },
    { x: productRulerBody.right, y: horizontalY },
    productRulerCenter,
    draft,
    draft.length_ruler_scale || 1,
    draft.length_ruler_offset_x || 0,
    draft.length_ruler_offset_y || 0,
    output
  );
  const heightRuler = transformProductRulerSegment(
    { x: verticalX, y: productRulerBody.top },
    { x: verticalX, y: productRulerBody.bottom },
    productRulerCenter,
    draft,
    draft.height_ruler_scale || 1,
    draft.height_ruler_offset_x || 0,
    draft.height_ruler_offset_y || 0,
    output
  );
  drawCanvasRuler(context, lengthRuler.start, lengthRuler.end, lengthLabel, output);
  drawCanvasRuler(context, heightRuler.start, heightRuler.end, heightLabel, output, true);

  const phoneLayout = jdComparisonPhoneLayout(
    output,
    geometry,
    baseGeometry,
    draft,
    phoneReference
  );
  const phone = phoneLayout.phone;
  if (phoneReference?.complete && phoneReference.naturalWidth) {
    context.drawImage(phoneReference, phone.left, phone.top, phone.width, phone.height);
  }
  const phoneRuler = draft.phone_show_ruler !== false ? phone : phoneLayout.basePhone;
  const phoneRulerX = Math.min(
    geometry.safe.right - 12,
    phoneRuler.left + phoneRuler.width + phoneLayout.phoneRulerGap
  );
  const phoneRulerSegment = transformCanvasRulerSegment(
    { x: phoneRulerX, y: phoneRuler.top },
    { x: phoneRulerX, y: phoneRuler.top + phoneRuler.height },
    draft.phone_ruler_scale || 1,
    draft.phone_ruler_offset_x || 0,
    draft.phone_ruler_offset_y || 0,
    output
  );
  drawCanvasRuler(
    context,
    phoneRulerSegment.start,
    phoneRulerSegment.end,
    "163mm",
    output,
    true,
    true
  );
  context.save();
  context.fillStyle = "#707070";
  context.font = `500 ${Math.max(12, Math.round(output.width * 0.017))}px sans-serif`;
  context.textAlign = "center";
  context.fillText("iPhone 17 Pro Max", phone.left + phone.width / 2, phone.top + phone.height + 22);
  context.restore();
}

function LiveSlotPreview({ sourceUrl, compositePrimaryUrl, templateUrl, slot, draft, platform, sourceIndex, targetFolder, productInfo, logoColor }: {
  sourceUrl: string;
  compositePrimaryUrl?: string;
  templateUrl?: string;
  slot: Slot;
  draft: ImageAdjustment;
  platform: OrganizerPlatform;
  sourceIndex: number;
  targetFolder: PreviewFolder;
  productInfo: Record<string, string>;
  logoColor: LogoColor;
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
    const compositePrimary = platform === "vip" && slot.file_name === "606.jpg"
      ? livePreviewImage(compositePrimaryUrl || sourceUrl)
      : null;
    const template = templateUrl && platform !== "jd" ? livePreviewImage(templateUrl) : null;
    const phoneReference = platform === "jd" && slot.file_name === "5.jpg"
      ? livePreviewImage("/organizer-assets/iphone_reference.png")
      : null;
    const logoReference = platform === "jd" && /^[1-5]\.jpg$/.test(slot.file_name)
      ? livePreviewImage(`/organizer-assets/elle_logo_${logoColor}.png`)
      : null;
    const draw = () => {
      if (!image.complete || !image.naturalWidth) return;
      if (compositePrimary && (!compositePrimary.complete || !compositePrimary.naturalWidth)) return;
      if (template && (!template.complete || !template.naturalWidth)) return;
      context.clearRect(0, 0, output.width, output.height);
      context.fillStyle = slot.file_name === "5.jpg" && platform === "jd" ? "#f3f3f3" : "#fff";
      context.fillRect(0, 0, output.width, output.height);
      if (template) context.drawImage(template, 0, 0, output.width, output.height);
      if (platform === "vip" && ["604.jpg", "605.jpg"].includes(slot.file_name)) {
        context.fillStyle = "#fff";
        context.fillRect(0, output.height * 0.18, output.width, output.height * 0.82);
      }
      if (platform === "jd" && slot.file_name === "5.jpg") {
        drawJdComparisonPreview(context, output, image, sourceUrl, phoneReference, logoReference, draft, productInfo);
        drawAdjustmentGuide(context, output, slot, platform, sourceIndex, targetFolder);
        return;
      }

      let area = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
      const areaX = area.x * output.width;
      const areaY = area.y * output.height;
      const areaWidth = area.width * output.width;
      const areaHeight = area.height * output.height;
      const hasManualCrop = draft.crop_x > 0.0001
        || draft.crop_y > 0.0001
        || draft.crop_width < 0.9999
        || draft.crop_height < 0.9999;
      const hasManualLayout = hasManualCrop
        || Math.abs(draft.zoom - 1) > 0.0001
        || Math.abs(draft.offset_x) > 0.0001
        || Math.abs(draft.offset_y) > 0.0001;
      const automaticInteriorDetail = (platform === "vip" && slot.file_name === "15.jpg")
        || (platform === "jd" && slot.file_name === "4.jpg");
      const automaticDetailCandidate = automaticInteriorDetail
        || (platform === "vip" && ["604.jpg", "605.jpg"].includes(slot.file_name));
      const usesAutomaticDetailCutout = automaticDetailCandidate
        && !hasManualCrop
        && livePreviewHasLightStudioBorder(sourceUrl, image);
      const usesProductCutout = (platform === "jd"
        ? ["2.jpg", "透明.png"].includes(slot.file_name)
        : ["2.jpg", "3.jpg", "30.png", "401.jpg", "606.jpg"].includes(slot.file_name))
        || usesAutomaticDetailCutout;
      const automaticBounds = usesProductCutout ? livePreviewContentBounds(sourceUrl, image) : {
        left: 0,
        top: 0,
        right: image.naturalWidth,
        bottom: image.naturalHeight
      };
      // A manual crop is expressed in full-image coordinates. Automatic content
      // bounds are only useful before the designer chooses an explicit region.
      const bounds = usesProductCutout && !hasManualCrop ? automaticBounds : {
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
      const productLayer = usesProductCutout ? livePreviewProductCutout(sourceUrl, image) : null;
      // The backend removes whitespace again after a manual product crop. Use
      // the same cropped, tightly bounded layer in the live preview so crop,
      // zoom and drag do not jump when the exact preview is generated.
      const croppedProductLayer = usesProductCutout && hasManualCrop
        ? liveJdProductLayer(sourceUrl, image, draft)
        : null;
      const drawSource = croppedProductLayer?.canvas || productLayer || image;
      const drawSourceScaleX = productLayer ? productLayer.width / image.naturalWidth : 1;
      const drawSourceScaleY = productLayer ? productLayer.height / image.naturalHeight : 1;
      const drawSourceX = croppedProductLayer ? 0 : sourceX * drawSourceScaleX;
      const drawSourceY = croppedProductLayer ? 0 : sourceY * drawSourceScaleY;
      const drawSourceWidth = croppedProductLayer ? croppedProductLayer.canvas.width : sourceWidth * drawSourceScaleX;
      const drawSourceHeight = croppedProductLayer ? croppedProductLayer.canvas.height : sourceHeight * drawSourceScaleY;
      const fitScale = area.mode === "cover" && !hasManualCrop && !usesAutomaticDetailCutout
        ? Math.max(areaWidth / drawSourceWidth, areaHeight / drawSourceHeight)
        : Math.min(areaWidth / drawSourceWidth, areaHeight / drawSourceHeight);
      const automaticDetailScale = usesAutomaticDetailCutout
        ? automaticInteriorDetail ? 0.9 : 0.82
        : 1;
      const detailRatio = contentWidth / Math.max(1, contentHeight);
      const vipDetailOffset = detailRatio <= 0.78
        ? -0.105
        : detailRatio <= 1.05 ? -0.11 : detailRatio <= 1.45 ? -0.12 : -0.13;
      const drawWidth = drawSourceWidth * fitScale * draft.zoom * automaticDetailScale;
      const drawHeight = drawSourceHeight * fitScale * draft.zoom * automaticDetailScale;
      let drawX = areaX + (areaWidth - drawWidth) / 2 + draft.offset_x * areaWidth;
      const multiAngleHandleLift = compositePrimary
        ? liveHandleVisualLift(livePreviewProductCutout(compositePrimaryUrl || sourceUrl, compositePrimary))
        : 0;
      const multiAngleRowShift = multiAngleHandleLift >= 0.55
        ? (sourceIndex < 2 ? 1 : -1) * Math.round(13 * multiAngleHandleLift * output.height / 750)
        : 0;
      const handleAware = (platform === "vip" && ["2.jpg", "3.jpg", "30.png"].includes(slot.file_name))
        || (platform === "jd" && slot.file_name === "2.jpg");
      const tallHandleDropAware = (platform === "vip" && ["2.jpg", "3.jpg", "30.png"].includes(slot.file_name))
        || (platform === "jd" && slot.file_name === "2.jpg");
      const tallHandleDropRatio = platform === "vip" && slot.file_name === "2.jpg" ? 0.14 : 0.12;
      const bodyCentered = (handleAware || (platform === "vip" && slot.file_name === "401.jpg"))
        && productLayer
        && !hasManualCrop;
      let drawY = areaY + (areaHeight - drawHeight) / 2 + draft.offset_y * areaHeight
        + multiAngleRowShift
        + (usesAutomaticDetailCutout && !automaticInteriorDetail ? vipDetailOffset * areaHeight : 0)
        - (platform === "vip" && ["2.jpg", "3.jpg", "30.png"].includes(slot.file_name) && !hasManualCrop ? 0.03 * areaHeight : 0)
        + (tallHandleDropAware && productLayer && !hasManualCrop
          ? tallHandleDropRatio * liveHandleVisualLift(productLayer) * areaHeight
          : 0);
      if (bodyCentered) {
        const body = liveInfoMeasurementBounds(productLayer);
        const cropLeft = sourceX * drawSourceScaleX;
        const cropTop = sourceY * drawSourceScaleY;
        const cropWidth = sourceWidth * drawSourceScaleX;
        const cropHeight = sourceHeight * drawSourceScaleY;
        drawX += (cropLeft + cropWidth / 2 - (body.left + body.right) / 2) * drawWidth / Math.max(1, cropWidth);
        drawY += (cropTop + cropHeight / 2 - (body.top + body.bottom) / 2) * drawHeight / Math.max(1, cropHeight);
      }
      const editorArea = slotEditorSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
      const safeLeft = editorArea.x * output.width;
      const safeTop = editorArea.y * output.height;
      const safeRight = (editorArea.x + editorArea.width) * output.width;
      const safeBottom = (editorArea.y + editorArea.height) * output.height;
      const allowFreeMovement = hasManualLayout && (
        (platform === "vip" && slot.file_name === "401.jpg")
        || (platform === "jd" && slot.file_name === "3.jpg")
      );
      if (!allowFreeMovement) {
        drawX = clampLayerOrigin(drawX, drawWidth, safeLeft, safeRight);
        drawY = clampLayerOrigin(drawY, drawHeight, safeTop, safeBottom);
      }
      if (platform === "jd" && slot.file_name === "2.jpg" && !hasManualLayout) {
        const body = productLayer ? liveInfoMeasurementBounds(productLayer) : null;
        const isTallHandleBag = productLayer && body
          ? (body.right - body.left) / Math.max(1, body.bottom - body.top) <= 1.15
            && liveHandleVisualLift(productLayer) >= 0.55
          : false;
        const baseSafeTop = output.width === 800 && output.height === 800 ? 162 : 175;
        const tallHandleSafeTop = output.width === 800 && output.height === 800 ? 180 : 185;
        const minimumTop = isTallHandleBag ? tallHandleSafeTop : baseSafeTop;
        const maximumBottom = output.width === 800 && output.height === 800 ? 740 : 930;
        drawY = drawHeight <= maximumBottom - minimumTop
          ? Math.max(minimumTop, Math.min(drawY, maximumBottom - drawHeight))
          : Math.max(drawY, minimumTop);
      }

      context.fillStyle = "#fff";
      if (platform === "vip" && slot.file_name === "401.jpg") {
        context.fillRect(280 * output.width / 750, 240 * output.height / 665, 440 * output.width / 750, 310 * output.height / 665);
      }
      context.fillRect(areaX, areaY, areaWidth, areaHeight);
      context.save();
      context.beginPath();
      context.rect(safeLeft, safeTop, safeRight - safeLeft, safeBottom - safeTop);
      context.clip();
      context.drawImage(
        drawSource,
        drawSourceX,
        drawSourceY,
        drawSourceWidth,
        drawSourceHeight,
        drawX,
        drawY,
        drawWidth,
        drawHeight
      );
      context.restore();

      if (platform === "vip" && slot.file_name === "401.jpg") {
        const scaleX = output.width / 750;
        const scaleY = output.height / 665;
        const lineColor = "#777";
        const storedBaseBody = storedProductRulerBase(draft);
        const baseBody = storedBaseBody || liveInfoProductBody(sourceUrl, image, draft);
        const ruler = infoRulerGeometry(baseBody);
        const productRulerCenter = {
          x: areaX + areaWidth / 2,
          y: areaY + areaHeight / 2
        };
        const widthRuler = infoWidthRulerGeometry(baseBody, productRulerCenter, draft, output);
        const lengthRuler = transformProductRulerSegment(
          { x: ruler.left, y: ruler.horizontalY },
          { x: ruler.right, y: ruler.horizontalY },
          productRulerCenter,
          draft,
          draft.length_ruler_scale || 1,
          draft.length_ruler_offset_x || 0,
          draft.length_ruler_offset_y || 0,
          output
        );
        const heightRuler = transformProductRulerSegment(
          { x: ruler.verticalX, y: ruler.top },
          { x: ruler.verticalX, y: ruler.bottom },
          productRulerCenter,
          draft,
          draft.height_ruler_scale || 1,
          draft.height_ruler_offset_x || 0,
          draft.height_ruler_offset_y || 0,
          output
        );
        const lengthValue = Number.parseFloat(productInfo.product_length || "");
        const widthValue = Number.parseFloat(productInfo.product_width || "");
        const heightValue = Number.parseFloat(productInfo.product_height || "");
        const dimensionLabel = (value: number) => Number.isFinite(value) ? `${Math.round(value)}mm` : "--mm";
        context.save();
        context.strokeStyle = lineColor;
        context.fillStyle = "#555";
        context.lineWidth = 2 * Math.min(scaleX, scaleY);
        context.font = `${Math.max(12, Math.round(19 * Math.min(scaleX, scaleY)))}px sans-serif`;
        context.textAlign = "center";
        context.beginPath();
        context.moveTo(lengthRuler.start.x, lengthRuler.start.y);
        context.lineTo(lengthRuler.end.x, lengthRuler.end.y);
        context.moveTo(lengthRuler.start.x, lengthRuler.start.y - 9 * scaleY);
        context.lineTo(lengthRuler.start.x, lengthRuler.start.y + 9 * scaleY);
        context.moveTo(lengthRuler.end.x, lengthRuler.end.y - 9 * scaleY);
        context.lineTo(lengthRuler.end.x, lengthRuler.end.y + 9 * scaleY);
        context.moveTo(heightRuler.start.x, heightRuler.start.y);
        context.lineTo(heightRuler.end.x, heightRuler.end.y);
        context.moveTo(heightRuler.start.x - 9 * scaleX, heightRuler.start.y);
        context.lineTo(heightRuler.start.x + 9 * scaleX, heightRuler.start.y);
        context.moveTo(heightRuler.end.x - 9 * scaleX, heightRuler.end.y);
        context.lineTo(heightRuler.end.x + 9 * scaleX, heightRuler.end.y);
        widthRuler.segments.forEach(([start, end]) => {
          context.moveTo(start.x * scaleX, start.y * scaleY);
          context.lineTo(end.x * scaleX, end.y * scaleY);
        });
        context.stroke();
        context.fillText(dimensionLabel(lengthValue), (lengthRuler.start.x + lengthRuler.end.x) / 2, lengthRuler.start.y + 36 * scaleY);
        context.save();
        context.translate(heightRuler.start.x - 31 * scaleX, (heightRuler.start.y + heightRuler.end.y) / 2 - 7 * scaleY);
        context.rotate(-Math.PI / 2);
        context.fillText(dimensionLabel(heightValue), 0, 0);
        context.restore();
        context.save();
        context.translate(widthRuler.text.x * scaleX, widthRuler.text.y * scaleY);
        context.rotate(-26 * Math.PI / 180);
        context.fillText(dimensionLabel(widthValue), 0, 0);
        context.restore();
        context.restore();
      }

      if (platform === "jd" && logoReference?.complete && logoReference.naturalWidth) {
        const logoX = output.width === 750 && output.height === 1000 ? 56 : 32;
        const logoY = output.width === 750 && output.height === 1000 ? 45 : 38;
        context.drawImage(logoReference, logoX, logoY, 190, 60);
      }
      drawAdjustmentGuide(context, output, slot, platform, sourceIndex, targetFolder);
    };
    image.addEventListener("load", draw);
    if (compositePrimary) compositePrimary.addEventListener("load", draw);
    if (template) template.addEventListener("load", draw);
    if (phoneReference) phoneReference.addEventListener("load", draw);
    if (logoReference) logoReference.addEventListener("load", draw);
    draw();
    return () => {
      image.removeEventListener("load", draw);
      if (compositePrimary) compositePrimary.removeEventListener("load", draw);
      if (template) template.removeEventListener("load", draw);
      if (phoneReference) phoneReference.removeEventListener("load", draw);
      if (logoReference) logoReference.removeEventListener("load", draw);
    };
  }, [
    draft,
    platform,
    slot.file_name,
    slot.size,
    sourceIndex,
    sourceUrl,
    compositePrimaryUrl,
    targetFolder,
    templateUrl,
    logoColor,
    productInfo.product_length,
    productInfo.product_width,
    productInfo.product_height
  ]);

  return <canvas ref={canvasRef} aria-label={`${slot.file_name} 前端即时预览`} />;
}

function slotPreviewSignature(
  slot: Slot,
  productInfo: Record<string, string>,
  platform: OrganizerPlatform,
  targetFolder: PreviewFolder = "800"
) {
  const jdSizeInfo = {
    product_length: productInfo.product_length || "",
    product_height: productInfo.product_height || ""
  };
  return JSON.stringify({
    platform,
    targetFolder,
    slot,
    productInfo: slot.file_name === "401.jpg"
      ? productInfo
      : platform === "jd" && slot.file_name === "5.jpg" ? jdSizeInfo : undefined
  });
}

function jdComparisonDimensionsReady(productInfo: Record<string, string>) {
  const length = Number.parseFloat(productInfo.product_length || "");
  const height = Number.parseFloat(productInfo.product_height || "");
  return Number.isFinite(length) && length > 0 && Number.isFinite(height) && height > 0;
}

function UploadPasteControl({ disabled = false, onUpload }: {
  disabled?: boolean;
  onUpload: (kind: "product" | "model" | "tag", files: File[], skipped?: number) => void;
}) {
  const [target, setTarget] = useState<"product" | "model" | "tag">("product");
  const [pasteHint, setPasteHint] = useState("");
  const pasteButtonRef = useRef<HTMLButtonElement | null>(null);

  function namedClipboardFile(blob: Blob, index: number) {
    const extension = blob.type === "image/jpeg"
      ? "jpg"
      : blob.type === "image/webp"
        ? "webp"
        : "png";
    return new File([blob], `粘贴图片-${Date.now()}-${index + 1}.${extension}`, { type: blob.type });
  }

  function acceptFiles(files: File[]) {
    const supported = files.filter((file) => SUPPORTED_IMAGE_NAME.test(file.name));
    const selected = target === "tag" ? supported.slice(0, 1) : supported;
    if (!selected.length) {
      setPasteHint("剪贴板中没有图片");
      return;
    }
    setPasteHint("");
    onUpload(target, selected, files.length - selected.length);
  }

  function handlePaste(event: ClipboardEvent<HTMLElement>) {
    if (disabled) return;
    const files = Array.from(event.clipboardData.items).flatMap((item) => {
      if (item.kind !== "file" || !item.type.startsWith("image/")) return [];
      const file = item.getAsFile();
      return file ? [file] : [];
    });
    if (!files.length) {
      setPasteHint("剪贴板中没有图片");
      return;
    }
    event.preventDefault();
    acceptFiles(files.map((file, index) => (
      SUPPORTED_IMAGE_NAME.test(file.name) ? file : namedClipboardFile(file, index)
    )));
  }

  async function pasteFromClipboard() {
    if (disabled) return;
    pasteButtonRef.current?.focus();
    if (!navigator.clipboard?.read) {
      setPasteHint("请按 Ctrl+V");
      return;
    }
    try {
      const items = await navigator.clipboard.read();
      const blobs = await Promise.all(items.flatMap((item) => {
        const imageType = item.types.find((type) => type.startsWith("image/"));
        return imageType ? [item.getType(imageType)] : [];
      }));
      acceptFiles(blobs.map(namedClipboardFile));
    } catch {
      setPasteHint("请按 Ctrl+V");
      pasteButtonRef.current?.focus();
    }
  }

  return (
    <div className="organizer-paste-control" onPaste={handlePaste}>
      <label>
        <span>粘贴到</span>
        <select
          value={target}
          disabled={disabled}
          onChange={(event) => {
            setTarget(event.target.value as "product" | "model" | "tag");
            setPasteHint("");
          }}
        >
          <option value="product">商品原图</option>
          <option value="model">模特图</option>
          <option value="tag">吊牌图</option>
        </select>
      </label>
      <button
        ref={pasteButtonRef}
        type="button"
        disabled={disabled}
        onClick={() => void pasteFromClipboard()}
      >
        <ClipboardPaste size={17} />{pasteHint || "粘贴图片"}
      </button>
      {pasteHint === "请按 Ctrl+V" && <small>目标：{target === "product" ? "商品原图" : target === "model" ? "模特图" : "吊牌图"}</small>}
    </div>
  );
}

function UploadSection({ title, hint, items, multiple = true, disabled = false, onUpload, onDelete, onPreview }: {
  title: string;
  hint: string;
  items: UploadItem[];
  multiple?: boolean;
  disabled?: boolean;
  onUpload: (files: FileList | File[] | null, skipped?: number) => void;
  onDelete: (item: UploadItem) => void;
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
      <div className="organizer-upload-heading">
        <div><strong>{title}</strong><span>{hint}</span></div>
        <small>{items.length ? `${items.length} 张` : "尚未上传"}</small>
      </div>
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
        <div className="organizer-thumb-item" key={item.image_id} title={item.file_name}>
          <button className="organizer-thumb-preview" type="button" onClick={() => onPreview(item.original_url || item.preview_url)} aria-label={`预览 ${item.file_name}`}>
            <img src={item.preview_url} alt={item.file_name} />
          </button>
          <button
            className="organizer-thumb-delete"
            type="button"
            disabled={disabled}
            onClick={() => onDelete(item)}
            aria-label={`删除 ${item.file_name}`}
            title={`删除 ${item.file_name}`}
          >
            <X size={13} />
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
  compositePrimaryUrl,
  displaySourceUrl,
  initialPreview,
  productInfo,
  platform,
  targetFolder,
  initialMoveTarget = "product",
  onClose,
  onSave
}: {
  sessionId: string;
  slot: Slot;
  sourceIndex: number;
  sourceUrl: string;
  compositePrimaryUrl?: string;
  displaySourceUrl?: string;
  initialPreview?: string;
  productInfo: Record<string, string>;
  platform: OrganizerPlatform;
  targetFolder: PreviewFolder;
  initialMoveTarget?: "product" | "phone";
  onClose: () => void;
  onSave: (
    adjustment: ImageAdjustment,
    logoColor: LogoColor,
    previewUrl?: string,
    syncJdFolders?: boolean
  ) => void;
}) {
  const initial = normalizeAdjustment(slot.adjustments?.[sourceIndex]);
  const supportsLogoColor = platform === "jd" && /^[1-5]\.jpg$/.test(slot.file_name);
  const [draft, setDraft] = useState<ImageAdjustment>(initial);
  const [logoColor, setLogoColor] = useState<LogoColor>(slot.logo_color === "white" ? "white" : "black");
  const [renderedPreview, setRenderedPreview] = useState(initialPreview || "");
  const [previewSynced, setPreviewSynced] = useState(Boolean(initialPreview));
  const [holdExactPreview, setHoldExactPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);
  const [cropMode, setCropMode] = useState(false);
  const isPhoneComparison = platform === "jd" && slot.file_name === "5.jpg";
  const supportsJdFolderSync = platform === "jd" && previewFoldersForSlot(slot, platform).length > 1;
  const [syncJdFolders, setSyncJdFolders] = useState(
    supportsJdFolderSync && !isPhoneComparison
  );
  const isPhoneObjectEditor = isPhoneComparison && initialMoveTarget === "phone";
  const isInfoPage = platform === "vip" && slot.file_name === "401.jpg";
  const [moveTarget, setMoveTarget] = useState<AdjustmentTarget>(initialMoveTarget);
  const [infoMoveTarget, setInfoMoveTarget] = useState<InfoMoveTarget>(
    initial.product_show_ruler === false ? "product" : "product_rulers"
  );
  const [cropSelection, setCropSelection] = useState<CropSelection | null>(null);
  const sourceStageRef = useRef<HTMLDivElement>(null);
  const resultStageRef = useRef<HTMLDivElement>(null);
  const sourceImageRef = useRef<HTMLImageElement>(null);
  const cropStartRef = useRef<{ x: number; y: number } | null>(null);
  const cropSelectionRef = useRef<CropSelection | null>(null);
  const moveStartRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number; target: AdjustmentTarget } | null>(null);
  const pendingMoveRef = useRef<ImageAdjustment | null>(null);
  const moveFrameRef = useRef<number | null>(null);
  const draftRef = useRef<ImageAdjustment>(initial);
  const logoColorRef = useRef<LogoColor>(slot.logo_color === "white" ? "white" : "black");
  const renderedPreviewRef = useRef(initialPreview || "");
  const draftVersionRef = useRef(0);
  const syncedVersionRef = useRef(initialPreview ? 0 : -1);
  const previewRequestRef = useRef(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const previewTimerRef = useRef<number | null>(null);
  const moveTargetRef = useRef<AdjustmentTarget>("product");
  const linkedProductRulersRef = useRef(false);

  const activeMoveTarget: AdjustmentTarget = isInfoPage
    ? (infoMoveTarget === "product_rulers" ? "product" : infoMoveTarget)
    : moveTarget;
  moveTargetRef.current = activeMoveTarget;
  linkedProductRulersRef.current = activeMoveTarget === "product" && (
    isInfoPage
      ? infoMoveTarget === "product_rulers"
      : isPhoneComparison && draft.product_show_ruler !== false
  );

  function updateTargetScale(current: ImageAdjustment, target: AdjustmentTarget, scale: number) {
    return target === "product" && linkedProductRulersRef.current
      ? withLinkedProductScale(current, scale)
      : withTargetScale(current, target, scale);
  }

  function updateTargetOffset(current: ImageAdjustment, target: AdjustmentTarget, x: number, y: number) {
    if (target !== "product" || !linkedProductRulersRef.current) {
      return withTargetOffset(current, target, x, y);
    }
    return withLinkedProductOffset(
      current,
      x,
      y
    );
  }

  function withPhoneRulerLinkPreservingPosition(
    current: ImageAdjustment,
    linked: boolean
  ): ImageAdjustment {
    const currentlyLinked = current.phone_show_ruler !== false;
    if (currentlyLinked === linked) return current;
    const productImage = livePreviewImage(sourceUrl);
    const phoneReference = livePreviewImage("/organizer-assets/iphone_reference.png");
    if (!productImage.complete || !productImage.naturalWidth) {
      return { ...current, phone_show_ruler: linked };
    }

    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const layer = liveJdProductLayer(sourceUrl, productImage, current);
    const { geometry, baseGeometry } = jdComparisonProductGeometry(
      output,
      layer,
      current,
      productInfo
    );
    const phoneLayout = jdComparisonPhoneLayout(
      output,
      geometry,
      baseGeometry,
      current,
      phoneReference
    );
    const rawSegment = (useLinkedPhone: boolean) => {
      const box = useLinkedPhone ? phoneLayout.phone : phoneLayout.basePhone;
      const x = Math.min(
        geometry.safe.right - 12,
        box.left + box.width + phoneLayout.phoneRulerGap
      );
      return {
        start: { x, y: box.top },
        end: { x, y: box.top + box.height }
      };
    };
    const previousBase = rawSegment(currentlyLinked);
    const desired = transformCanvasRulerSegment(
      previousBase.start,
      previousBase.end,
      current.phone_ruler_scale || 1,
      current.phone_ruler_offset_x || 0,
      current.phone_ruler_offset_y || 0,
      output
    );
    const nextBase = rawSegment(linked);
    const nextBaseLength = Math.max(1, nextBase.end.y - nextBase.start.y);
    const desiredLength = Math.max(1, desired.end.y - desired.start.y);
    const nextBaseCenter = {
      x: (nextBase.start.x + nextBase.end.x) / 2,
      y: (nextBase.start.y + nextBase.end.y) / 2
    };
    const desiredCenter = {
      x: (desired.start.x + desired.end.x) / 2,
      y: (desired.start.y + desired.end.y) / 2
    };
    return {
      ...current,
      phone_show_ruler: linked,
      phone_ruler_scale: desiredLength / nextBaseLength,
      phone_ruler_offset_x: (desiredCenter.x - nextBaseCenter.x) / (output.width * 0.18),
      phone_ruler_offset_y: (desiredCenter.y - nextBaseCenter.y) / (output.height * 0.18)
    };
  }

  function productRulerBodyForDraft(nextDraft: ImageAdjustment): PixelBounds | null {
    if ((!isInfoPage && !isPhoneComparison) || !sourceImageRef.current?.naturalWidth) return null;
    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const layer = liveJdProductLayer(sourceUrl, sourceImageRef.current, nextDraft);
    if (isPhoneComparison) {
      const { geometry } = jdComparisonProductGeometry(output, layer, nextDraft, productInfo);
      return geometry.body;
    }
    return liveInfoProductBody(sourceUrl, sourceImageRef.current, nextDraft);
  }

  function withSyncedProductRulerBody(nextDraft: ImageAdjustment): ImageAdjustment {
    if (isPhoneObjectEditor) return nextDraft;
    const current = draftRef.current;
    const currentBody = storedProductRulerBase(current);
    const cropUnchanged = current.crop_x === nextDraft.crop_x
      && current.crop_y === nextDraft.crop_y
      && current.crop_width === nextDraft.crop_width
      && current.crop_height === nextDraft.crop_height;
    const hasLegacyGroupTransform = Math.abs((current.product_ruler_group_scale || 1) - 1) > 0.0001
      || Math.abs(current.product_ruler_group_offset_x || 0) > 0.0001
      || Math.abs(current.product_ruler_group_offset_y || 0) > 0.0001;
    if (currentBody && cropUnchanged && !hasLegacyGroupTransform) {
      const currentProductBody = productRulerBodyForDraft(current);
      const nextProductBody = productRulerBodyForDraft(nextDraft);
      if (currentProductBody && nextProductBody) {
        const currentWidth = Math.max(1, currentProductBody.right - currentProductBody.left);
        const currentHeight = Math.max(1, currentProductBody.bottom - currentProductBody.top);
        const scaleX = (nextProductBody.right - nextProductBody.left) / currentWidth;
        const scaleY = (nextProductBody.bottom - nextProductBody.top) / currentHeight;
        const currentCenterX = (currentProductBody.left + currentProductBody.right) / 2;
        const nextCenterX = (nextProductBody.left + nextProductBody.right) / 2;
        const transformX = (value: number) => nextCenterX + (value - currentCenterX) * scaleX;
        const transformY = (value: number) => nextProductBody.bottom
          + (value - currentProductBody.bottom) * scaleY;
        return {
          ...nextDraft,
          product_ruler_base_left: transformX(currentBody.left),
          product_ruler_base_top: transformY(currentBody.top),
          product_ruler_base_right: transformX(currentBody.right),
          product_ruler_base_bottom: transformY(currentBody.bottom),
          product_ruler_group_scale: 1,
          product_ruler_group_offset_x: 0,
          product_ruler_group_offset_y: 0
        };
      }
    }
    const body = productRulerBodyForDraft(nextDraft);
    return body ? {
      ...nextDraft,
      product_ruler_base_left: body.left,
      product_ruler_base_top: body.top,
      product_ruler_base_right: body.right,
      product_ruler_base_bottom: body.bottom,
      product_ruler_group_scale: 1,
      product_ruler_group_offset_x: 0,
      product_ruler_group_offset_y: 0
    } : nextDraft;
  }

  function slotWithDraft(nextDraft: ImageAdjustment) {
    const adjustments = [...(slot.adjustments || [])];
    while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
    adjustments[sourceIndex] = nextDraft;
    return { ...slot, adjustments, logo_color: logoColorRef.current };
  }

  function cancelStalePreview() {
    if (previewTimerRef.current !== null) {
      window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = null;
    }
    if (!previewAbortRef.current) return;
    previewAbortRef.current.abort();
    previewAbortRef.current = null;
    previewRequestRef.current += 1;
    setBusy(false);
  }

  function scheduleExactPreview(nextDraft: ImageAdjustment, version: number) {
    if (previewTimerRef.current !== null) window.clearTimeout(previewTimerRef.current);
    previewTimerRef.current = window.setTimeout(() => {
      previewTimerRef.current = null;
      void refreshPreview(nextDraft, version);
    }, 320);
  }

  function applyDraft(
    nextDraft: ImageAdjustment,
    syncProductRulerBody = linkedProductRulersRef.current
  ) {
    const preparedDraft = syncProductRulerBody
      ? withSyncedProductRulerBody(nextDraft)
      : nextDraft;
    cancelStalePreview();
    setHoldExactPreview(false);
    draftVersionRef.current += 1;
    draftRef.current = preparedDraft;
    setDraft(preparedDraft);
    setPreviewSynced(false);
    scheduleExactPreview(preparedDraft, draftVersionRef.current);
  }

  function changeLogoColor(nextColor: LogoColor) {
    if (logoColorRef.current === nextColor) return;
    cancelStalePreview();
    logoColorRef.current = nextColor;
    setLogoColor(nextColor);
    draftVersionRef.current += 1;
    const nextVersion = draftVersionRef.current;
    setHoldExactPreview(Boolean(renderedPreviewRef.current));
    setPreviewSynced(false);
    void refreshPreview(draftRef.current, nextVersion).finally(() => {
      if (draftVersionRef.current === nextVersion) setHoldExactPreview(false);
    });
  }

  function changePhoneAlignment(nextAlignment: "center" | "bottom") {
    if (draftRef.current.phone_alignment === nextAlignment) return;
    cancelStalePreview();
    const nextDraft = { ...draftRef.current, phone_alignment: nextAlignment };
    draftVersionRef.current += 1;
    const nextVersion = draftVersionRef.current;
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    setHoldExactPreview(Boolean(renderedPreviewRef.current));
    setPreviewSynced(false);
    void refreshPreview(nextDraft, nextVersion).finally(() => {
      if (draftVersionRef.current === nextVersion) setHoldExactPreview(false);
    });
  }

  async function refreshPreview(
    nextDraft: ImageAdjustment = draftRef.current,
    version = draftVersionRef.current
  ): Promise<string | undefined> {
    if (previewTimerRef.current !== null) {
      window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = null;
    }
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
      if (previewTimerRef.current !== null) window.clearTimeout(previewTimerRef.current);
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
      const target = moveTargetRef.current;
      const maximum = target === "product" ? 4 : target === "phone" ? 1.8 : 2;
      const nextScale = Math.max(0.5, Math.min(maximum, Math.round((targetScale(current, target) + delta) * 100) / 100));
      applyDraft(updateTargetScale(current, target, nextScale));
    };
    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [isPhoneComparison]);

  function displayedImageRect() {
    const stage = sourceStageRef.current;
    const image = sourceImageRef.current;
    if (!stage || !image?.naturalWidth || !image.naturalHeight) return null;
    const stageBounds = stage.getBoundingClientRect();
    const imageBounds = image.getBoundingClientRect();
    const scale = Math.min(imageBounds.width / image.naturalWidth, imageBounds.height / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    return {
      left: imageBounds.left - stageBounds.left + (imageBounds.width - width) / 2,
      top: imageBounds.top - stageBounds.top + (imageBounds.height - height) / 2,
      width,
      height
    };
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

  function updateCropSelection(clientX: number, clientY: number) {
    const start = cropStartRef.current;
    const point = sourcePoint(clientX, clientY);
    if (!start || !point) return;
    const nextSelection = cropSelectionForTemplate(start, point, point.imageRect, cropAspectRatio());
    cropSelectionRef.current = nextSelection;
    setCropSelection(nextSelection);
  }

  function cropAspectRatio() {
    const isProductCrop = platform === "jd"
      ? ["2.jpg", "5.jpg", "透明.png"].includes(slot.file_name)
      : ["2.jpg", "3.jpg", "30.png", "401.jpg", "606.jpg", "801.jpg"].includes(slot.file_name);
    if (isProductCrop) return null;
    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const area = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
    return (area.width * output.width) / Math.max(1, area.height * output.height);
  }

  function cropAdjustment(selection: CropSelection, imageRect: CropSelection) {
    const nextDraft: ImageAdjustment = {
      ...draftRef.current,
      crop_x: (selection.left - imageRect.left) / imageRect.width,
      crop_y: (selection.top - imageRect.top) / imageRect.height,
      crop_width: selection.width / imageRect.width,
      crop_height: selection.height / imageRect.height,
      zoom: 1,
      offset_x: 0,
      offset_y: 0,
      product_ruler_base_left: undefined,
      product_ruler_base_top: undefined,
      product_ruler_base_right: undefined,
      product_ruler_base_bottom: undefined
    };
    return linkedProductRulersRef.current ? {
      ...nextDraft,
      product_ruler_group_scale: 1,
      product_ruler_group_offset_x: 0,
      product_ruler_group_offset_y: 0
    } : nextDraft;
  }

  function finishCrop() {
    const selection = cropSelectionRef.current;
    const imageRect = displayedImageRect();
    cropStartRef.current = null;
    if (!selection || !imageRect || selection.width < 8 || selection.height < 8) return;
    applyDraft(cropAdjustment(selection, imageRect));
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
      const previousSelection = {
        left: imageRect.left + current.crop_x * imageRect.width,
        top: imageRect.top + current.crop_y * imageRect.height,
        width: current.crop_width * imageRect.width,
        height: current.crop_height * imageRect.height
      };
      const selection = fitCropSelectionToTemplate(previousSelection, imageRect, cropAspectRatio());
      cropSelectionRef.current = selection;
      setCropSelection(selection);
      applyDraft(cropAdjustment(selection, imageRect));
    });
  }

  function changeZoom(delta: number) {
    const current = draftRef.current;
    const maximum = activeMoveTarget === "product" ? 4 : activeMoveTarget === "phone" ? 1.8 : 2;
    const nextScale = Math.max(0.5, Math.min(maximum, Math.round((targetScale(current, activeMoveTarget) + delta) * 100) / 100));
    applyDraft(updateTargetScale(current, activeMoveTarget, nextScale));
  }

  function reset() {
    if (!isPhoneComparison) {
      logoColorRef.current = "black";
      setLogoColor("black");
    }
    if (activeMoveTarget !== "product") {
      let next = withTargetScale(draftRef.current, activeMoveTarget, targetScale(DEFAULT_ADJUSTMENT, activeMoveTarget));
      next = withTargetOffset(next, activeMoveTarget, 0, 0);
      if (activeMoveTarget === "phone") {
        const keepPhoneRulerLinked = isPhoneObjectEditor && draftRef.current.phone_show_ruler !== false;
        next = {
          ...next,
          phone_alignment: "bottom",
          phone_show_ruler: keepPhoneRulerLinked,
          ...(keepPhoneRulerLinked ? {
            phone_ruler_scale: DEFAULT_ADJUSTMENT.phone_ruler_scale,
            phone_ruler_offset_x: DEFAULT_ADJUSTMENT.phone_ruler_offset_x,
            phone_ruler_offset_y: DEFAULT_ADJUSTMENT.phone_ruler_offset_y
          } : {})
        };
      }
      applyDraft(next);
    } else if (isPhoneComparison || isInfoPage) {
      const resetProduct = {
        ...draftRef.current,
        zoom: DEFAULT_ADJUSTMENT.zoom,
        offset_x: DEFAULT_ADJUSTMENT.offset_x,
        offset_y: DEFAULT_ADJUSTMENT.offset_y,
        crop_x: DEFAULT_ADJUSTMENT.crop_x,
        crop_y: DEFAULT_ADJUSTMENT.crop_y,
        crop_width: DEFAULT_ADJUSTMENT.crop_width,
        crop_height: DEFAULT_ADJUSTMENT.crop_height,
        product_ruler_base_left: undefined,
        product_ruler_base_top: undefined,
        product_ruler_base_right: undefined,
        product_ruler_base_bottom: undefined,
        product_show_ruler: isInfoPage
          ? infoMoveTarget === "product_rulers"
          : draftRef.current.product_show_ruler
      };
      const automaticDraft = linkedProductRulersRef.current ? {
        ...resetProduct,
        product_ruler_group_scale: 1,
        product_ruler_group_offset_x: 0,
        product_ruler_group_offset_y: 0,
        length_ruler_scale: 1,
        length_ruler_offset_x: 0,
        length_ruler_offset_y: 0,
        height_ruler_scale: 1,
        height_ruler_offset_x: 0,
        height_ruler_offset_y: 0
      } : resetProduct;
      // A reset must discard the stored manual ruler body. Re-syncing here would
      // rebuild it from the pre-reset draft and make "恢复自动" retain stale state.
      applyDraft(automaticDraft, false);
    } else {
      applyDraft({ ...DEFAULT_ADJUSTMENT });
    }
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
    flushPendingMove();
    const currentDraft = draftRef.current;
    let previewUrl = renderedPreviewRef.current;
    if (previewTimerRef.current !== null) {
      window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = null;
    }
    if (syncedVersionRef.current !== draftVersionRef.current) {
      previewUrl = await refreshPreview(currentDraft, draftVersionRef.current) || "";
    }
    if (previewUrl) onSave(currentDraft, logoColorRef.current, previewUrl, syncJdFolders);
  }

  function requestClose() {
    flushPendingMove();
    const changed = draftVersionRef.current > 0
      || logoColorRef.current !== (slot.logo_color === "white" ? "white" : "black");
    if (changed) {
      setShowCloseConfirm(true);
      return;
    }
    onClose();
  }

  return (
    <div className="slot-adjustment-modal" role="dialog" aria-modal="true" aria-label={`调整 ${slot.file_name}`} onMouseDown={(event) => {
      if (event.target === event.currentTarget) requestClose();
    }}>
      <section className="slot-adjustment-dialog">
        <header>
          <div>
            <strong>{slot.file_name} · {slot.title}</strong>
            <span>{slot.file_name === "606.jpg" ? `正在调整来源 ${sourceIndex + 1}` : isPhoneComparison ? `正在调整${moveTarget === "phone" ? (draft.phone_show_ruler !== false ? "手机和高标线" : "手机") : moveTarget === "phone_ruler" ? "高标线" : moveTarget === "length_ruler" ? "商品长标线" : moveTarget === "height_ruler" ? "商品高标线" : "商品图"}` : isInfoPage ? `正在调整${infoMoveTarget === "width_ruler" ? "宽标线" : infoMoveTarget === "length_ruler" ? "长标线" : infoMoveTarget === "height_ruler" ? "高标线" : infoMoveTarget === "product" ? "商品图" : "全部"}` : "当前输出位置独立调整"}</span>
          </div>
          <button type="button" className="icon-button" onClick={requestClose} title="关闭"><X size={21} /></button>
        </header>

        {showCloseConfirm && <div className="slot-close-confirm" role="alertdialog" aria-label="未保存调整提示">
          <div><strong>调整尚未保存</strong><span>保存会生成最终预览并退出；右上角 × 会放弃本次调整。</span></div>
          {supportsJdFolderSync && <button
            type="button"
            className={syncJdFolders ? "active-tool" : ""}
            aria-pressed={syncJdFolders}
            onClick={() => setSyncJdFolders((current) => !current)}
          >同步 800/750</button>}
          <button type="button" className="primary" disabled={busy} onClick={() => void saveAdjustment()}><Save size={17} />保存并退出</button>
          <button type="button" className="icon-button" onClick={onClose} aria-label="不保存并退出" title="不保存并退出"><X size={20} /></button>
        </div>}

        <div className="slot-adjustment-workspace">
          <div className="slot-adjustment-source">
            <div className="slot-adjustment-heading">
              <strong>{isPhoneObjectEditor ? "手机参照图" : "原始图片"}</strong>
              <span>{isPhoneObjectEditor ? "在右侧预览中调整手机或高标线" : cropMode ? "拖动框选保留区域" : "点击“裁剪”后框选区域"}</span>
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
                updateCropSelection(event.clientX, event.clientY);
              }}
              onPointerUp={(event) => {
                updateCropSelection(event.clientX, event.clientY);
                finishCrop();
              }}
              onPointerCancel={finishCrop}
            >
              <img ref={sourceImageRef} src={displaySourceUrl || sourceUrl} alt={isPhoneObjectEditor ? "手机参照图" : "原始素材"} draggable={false} />
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
                const currentOffset = targetOffset(draftRef.current, activeMoveTarget);
                moveStartRef.current = {
                  x: event.clientX,
                  y: event.clientY,
                  offsetX: currentOffset.x,
                  offsetY: currentOffset.y,
                  target: activeMoveTarget
                };
              }}
              onPointerMove={(event) => {
                const start = moveStartRef.current;
                if (!start) return;
                const bounds = event.currentTarget.getBoundingClientRect();
                const output = slotCanvasSize(slot.size, platform, targetFolder);
                const basis = adjustmentOffsetBasis(slot, platform, sourceIndex, targetFolder, start.target);
                const dragSensitivity = platform === "jd"
                  && slot.file_name === "2.jpg"
                  && start.target === "product"
                  ? 0.55
                  : 1;
                const canvasDeltaX = (event.clientX - start.x) * output.width / Math.max(1, bounds.width) * dragSensitivity;
                const canvasDeltaY = (event.clientY - start.y) * output.height / Math.max(1, bounds.height) * dragSensitivity;
                const nextOffsetX = Math.max(-1.5, Math.min(1.5, start.offsetX + canvasDeltaX / basis.x));
                const nextOffsetY = Math.max(-1.5, Math.min(1.5, start.offsetY + canvasDeltaY / basis.y));
                pendingMoveRef.current = updateTargetOffset(draftRef.current, start.target, nextOffsetX, nextOffsetY);
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
                compositePrimaryUrl={compositePrimaryUrl}
                templateUrl={renderedPreview || initialPreview}
                slot={slot}
                draft={draft}
                platform={platform}
                sourceIndex={sourceIndex}
                targetFolder={targetFolder}
                productInfo={productInfo}
                logoColor={logoColor}
              />
              {renderedPreview && <img
                className={`slot-exact-preview${previewSynced || holdExactPreview ? " is-visible" : ""}`}
                src={renderedPreview}
                alt={`${slot.file_name} 精确成品预览`}
                draggable={false}
              />}
              <SlotSafeAreaOverlay
                slot={slot}
                platform={platform}
                sourceIndex={sourceIndex}
                targetFolder={targetFolder}
              />
              <span className="slot-preview-loading">
                {busy ? <LoaderCircle className="spin" size={17} /> : <Move size={16} />}
                {busy ? "正在生成精确模板" : previewSynced ? "精确成品预览" : "前端即时预览"}
              </span>
            </div>
          </div>
        </div>

        <div className="slot-adjustment-controls">
          {(isPhoneComparison || isInfoPage) && <div className="slot-phone-controls" role="group" aria-label={isInfoPage ? "产品信息图调整" : "手机对比调整"}>
            <span>调整对象</span>
            {isPhoneObjectEditor ? <>
              <button type="button" className={moveTarget === "phone" && draft.phone_show_ruler !== false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone");
                setCropMode(false);
                if (draftRef.current.phone_show_ruler === false) {
                  applyDraft(withPhoneRulerLinkPreservingPosition(draftRef.current, true));
                }
              }}>全部</button>
              <button type="button" className={moveTarget === "phone" && draft.phone_show_ruler === false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone");
                setCropMode(false);
                if (draftRef.current.phone_show_ruler !== false) {
                  applyDraft(withPhoneRulerLinkPreservingPosition(draftRef.current, false));
                }
              }}>手机</button>
              <button type="button" className={moveTarget === "phone_ruler" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone_ruler");
                setCropMode(false);
                if (draftRef.current.phone_show_ruler !== false) {
                  applyDraft(withPhoneRulerLinkPreservingPosition(draftRef.current, false));
                }
              }}>高标线</button>
              <span>对齐</span>
              <button type="button" className={draft.phone_alignment === "center" ? "active-tool" : ""} onClick={() => changePhoneAlignment("center")}>中心同高</button>
              <button type="button" className={(draft.phone_alignment || "bottom") === "bottom" ? "active-tool" : ""} onClick={() => changePhoneAlignment("bottom")}>底部齐平</button>
            </> : isInfoPage ? <>
              <button type="button" className={infoMoveTarget === "product" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("product");
                linkedProductRulersRef.current = false;
                if (draftRef.current.product_show_ruler !== false) {
                  applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
                }
              }}>仅商品图</button>
              <button type="button" className={infoMoveTarget === "product_rulers" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("product_rulers");
                linkedProductRulersRef.current = true;
                applyDraft({ ...draftRef.current, product_show_ruler: true }, true);
              }}>全部</button>
              <button type="button" className={infoMoveTarget === "length_ruler" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("length_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
              }}>长标线</button>
              <button type="button" className={infoMoveTarget === "height_ruler" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("height_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
              }}>高标线</button>
              <button type="button" className={infoMoveTarget === "width_ruler" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("width_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
              }}>宽标线</button>
            </> : <>
              <button type="button" className={moveTarget === "product" && draft.product_show_ruler === false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("product");
                linkedProductRulersRef.current = false;
                if (draftRef.current.product_show_ruler !== false) {
                  applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
                }
              }}>商品图</button>
              <button type="button" className={moveTarget === "product" && draft.product_show_ruler !== false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("product");
                linkedProductRulersRef.current = true;
                applyDraft({ ...draftRef.current, product_show_ruler: true }, true);
              }}>商品图和长高标线</button>
              <button type="button" className={moveTarget === "length_ruler" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("length_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
                if (draftRef.current.product_show_ruler !== false) applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
              }}>商品长标线</button>
              <button type="button" className={moveTarget === "height_ruler" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("height_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
                if (draftRef.current.product_show_ruler !== false) applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
              }}>商品高标线</button>
            </>}
          </div>}
          {activeMoveTarget === "product" && <button type="button" className={cropMode ? "active-tool" : ""} onClick={toggleCropMode}><Crop size={18} />裁剪</button>}
          <button type="button" onClick={() => changeZoom(-0.05)}><ZoomOut size={18} />缩小</button>
          <span className="slot-zoom-value">{Math.round(targetScale(draft, activeMoveTarget) * 100)}%</span>
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
          <span className="slot-drag-hint"><Move size={16} />位置 {
            Math.round(targetOffset(draft, activeMoveTarget).x * 100)
          } / {
            Math.round(targetOffset(draft, activeMoveTarget).y * 100)
          }</span>
          {!previewSynced && <span className="slot-preview-pending">保存时会自动生成精确预览</span>}
          {supportsJdFolderSync && <button
            type="button"
            className={syncJdFolders ? "active-tool" : ""}
            aria-pressed={syncJdFolders}
            title={syncJdFolders ? "本次调整会同时保存到 800 和 750" : `本次调整只保存到 ${targetFolder}`}
            onClick={() => setSyncJdFolders((current) => !current)}
          >同步 800/750</button>}
          <button type="button" className="primary" disabled={busy} onClick={() => void saveAdjustment()}><Save size={18} />{busy ? "正在保存" : "保存并退出"}</button>
        </div>
        {error && <div className="alert warning">{error}</div>}
      </section>
    </div>
  );
}

type VipOrganizerProps = {
  active: boolean;
  initialProductFile?: File | null;
  onInitialProductFileConsumed?: () => void;
};

export default function VipOrganizer({ active, initialProductFile, onInitialProductFileConsumed }: VipOrganizerProps) {
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
  const slotsRef = useRef<Slot[]>([]);
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
  const [previewRetryVersion, setPreviewRetryVersion] = useState(0);
  const [platformSwitching, setPlatformSwitching] = useState(false);
  const [platformRegenerating, setPlatformRegenerating] = useState(false);
  const [adjustmentEditor, setAdjustmentEditor] = useState<{
    fileName: string;
    sourceIndex: number;
    targetFolder: PreviewFolder;
    targetObject: "product" | "phone";
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

  useEffect(() => {
    if (active) void api.prewarmHeavyTask("organizer").catch(() => undefined);
  }, [active]);

  useEffect(() => {
    if (!initialProductFile) return;
    onInitialProductFileConsumed?.();
    void upload("product", [initialProductFile]);
  }, [initialProductFile]);

  useEffect(() => {
    slotsRef.current = slots;
  }, [slots]);

  useEffect(() => {
    if (!preview) return;
    const closePreview = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreview(null);
    };
    window.addEventListener("keydown", closePreview);
    return () => window.removeEventListener("keydown", closePreview);
  }, [preview]);

  function organizerProductInfo() {
    const dimensions = [info.product_length, info.product_width, info.product_height]
      .map((value) => value.trim())
      .filter(Boolean)
      .join(" × ");
    return { ...info, dimensions: dimensions ? `${dimensions} mm` : "" };
  }

  useEffect(() => {
    if (platformSwitching || platformRegenerating) return;
    if (!sessionId || !slots.length) {
      previewAbortRef.current?.abort();
      setSlotPreviews({});
      return;
    }
    const productInfo = organizerProductInfo();
    const jdSizeReady = jdComparisonDimensionsReady(productInfo);
    const previewTargets = slots.flatMap((slot) => {
      if (!slot.image_ids[0] || (slot.file_name === "606.jpg" && slot.image_ids.length < 4)) return [];
      if (platform === "jd" && slot.file_name === "5.jpg" && !jdSizeReady) return [];
      if (platform === "vip" && slot.file_name === "401.jpg" && !jdSizeReady) return [];
      return previewFoldersForSlot(slot, platform).map((targetFolder) => ({
        slot,
        targetFolder,
        key: slotPreviewKey(platform, slot.file_name, targetFolder)
      }));
    });
    const signatures = Object.fromEntries(previewTargets.map((target) => [
      target.key,
      slotPreviewSignature(
        slotForPreviewFolder(target.slot, platform, target.targetFolder),
        productInfo,
        platform,
        target.targetFolder
      )
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
              slots: slotsForPreviewFolder(slots, platform, targetFolder),
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
              slots: [slotForPreviewFolder(target.slot, platform, target.targetFolder)],
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
  }, [sessionId, slots, info, platform, platformSwitching, platformRegenerating, previewRetryVersion]);

  useEffect(() => {
    if (
      platform !== "vip"
      || platformSwitching
      || platformRegenerating
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
        const backgroundSlots = result.slots.filter((slot: Slot) => (
          slot.file_name !== "5.jpg" || jdComparisonDimensionsReady(productInfo)
        ));
        const folderResults = await Promise.allSettled((["800", "750"] as PreviewFolder[]).map(async (targetFolder) => {
          const previews = await api.previewVipOrganizer({
            session_id: sessionId,
            slots: backgroundSlots,
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
        const signatures = Object.fromEntries(backgroundSlots.flatMap((slot: Slot) =>
          previewFoldersForSlot(slot, "jd").map((targetFolder) => [
            slotPreviewKey("jd", slot.file_name, targetFolder),
            slotPreviewSignature(
              slotForPreviewFolder(slot, "jd", targetFolder),
              productInfo,
              "jd",
              targetFolder
            )
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
  }, [platform, platformSwitching, platformRegenerating, previewBusy, sessionId, slots, slotPreviews, info]);

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
    clearLivePreviewCaches();
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
    slotsRef.current = [];
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

  async function deleteUploadedAsset(kind: "product" | "model" | "tag", item: UploadItem) {
    const currentSession = sessionIdRef.current || sessionId;
    if (!currentSession) return;
    setBusy(true);
    setMessage(`正在删除 ${item.file_name} 并刷新后续成品……`);
    try {
      await api.deleteVipOrganizerAsset(currentSession, item.image_id);

      const nextProducts = kind === "product"
        ? productsRef.current.filter((entry) => entry.image_id !== item.image_id)
        : productsRef.current;
      const nextModels = kind === "model"
        ? modelsRef.current.filter((entry) => entry.image_id !== item.image_id)
        : modelsRef.current;
      const nextTags = kind === "tag"
        ? tagsRef.current.filter((entry) => entry.image_id !== item.image_id)
        : tagsRef.current;
      productsRef.current = nextProducts;
      modelsRef.current = nextModels;
      tagsRef.current = nextTags;
      setProducts(nextProducts);
      setModels(nextModels);
      setTags(nextTags);

      const nextRoles = { ...assetRolesRef.current };
      const nextAssetTags = { ...assetTagsRef.current };
      delete nextRoles[item.image_id];
      delete nextAssetTags[item.image_id];
      assetRolesRef.current = nextRoles;
      assetTagsRef.current = nextAssetTags;
      setAssetRoles(nextRoles);
      setAssetTags(nextAssetTags);
      setManualAssetIds((current) => {
        const next = new Set(current);
        next.delete(item.image_id);
        return next;
      });
      setApiRoleNotes((current) => {
        const next = { ...current };
        delete next[item.image_id];
        return next;
      });
      setAssets((current) => ({
        product: (current.product || []).filter((asset: any) => (asset.id ?? asset.image_id) !== item.image_id),
        model: (current.model || []).filter((asset: any) => (asset.id ?? asset.image_id) !== item.image_id),
        tag: (current.tag || []).filter((asset: any) => (asset.id ?? asset.image_id) !== item.image_id)
      }));

      const previousSlots = slotsRef.current;
      const nextSlotState = previousSlots.map((slot) => {
        const keptIndexes = slot.image_ids
          .map((imageId, index) => ({ imageId, index }))
          .filter(({ imageId }) => imageId !== item.image_id);
        if (keptIndexes.length === slot.image_ids.length) return slot;
        return {
          ...slot,
          image_ids: keptIndexes.map(({ imageId }) => imageId),
          adjustments: slot.adjustments
            ? keptIndexes.map(({ index }) => slot.adjustments?.[index] || { ...DEFAULT_ADJUSTMENT })
            : undefined
        };
      });
      slotsRef.current = nextSlotState;
      setSlots(nextSlotState);
      setAdjustmentEditor((current) => {
        if (!current) return current;
        const editedSlot = previousSlots.find((slot) => slot.file_name === current.fileName);
        return editedSlot?.image_ids[current.sourceIndex] === item.image_id ? null : current;
      });
      setPreview((current) => (
        current === item.preview_url || current === item.original_url ? null : current
      ));

      previewAbortRef.current?.abort();
      previewRequestRef.current += 1;
      clearLivePreviewCaches();
      setSlotPreviews({});
      slotPreviewSignaturesRef.current = {};
      platformWorkspaceRef.current = {};
      jdBackgroundPreparedRef.current = false;

      if (!nextProducts.length) {
        slotsRef.current = [];
        setSlots([]);
        setAssets({ product: [], model: [], tag: [] });
        setMessage("商品原图已删除，请重新上传后再自动整理。");
        return;
      }
      await analyze(
        nextRoles,
        platform,
        nextAssetTags,
        { products: nextProducts, models: nextModels, tags: nextTags },
        undefined,
        false,
        true
      );
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function analyze(
    rolesOverride?: Record<number, string>,
    platformOverride: OrganizerPlatform = platform,
    tagsOverride?: Record<number, string[]>,
    collections?: { products: UploadItem[]; models: UploadItem[]; tags: UploadItem[] },
    incrementalKind?: "tag",
    manageBusy = true,
    replaceSlots = false
  ) {
    const productItems = collections?.products || productsRef.current;
    const modelItems = collections?.models || modelsRef.current;
    const tagItems = collections?.tags || tagsRef.current;
    if (!productItems.length) return setMessage("请先上传商品原图");
    if (manageBusy) setBusy(true);
    setMessage("");
    try {
      void api.prewarmHeavyTask("organizer").catch(() => undefined);
      const result = await api.analyzeVipOrganizer({
        session_id: sessionIdRef.current || sessionId,
        product_image_ids: productItems.map((item) => item.image_id),
        model_image_ids: modelItems.map((item) => item.image_id),
        tag_image_ids: tagItems.map((item) => item.image_id),
        asset_roles: rolesOverride || assetRolesRef.current,
        asset_tags: tagsOverride || assetTagsRef.current,
        platform: platformOverride
      });
      const currentSlots = slotsRef.current;
      let nextSlots: Slot[];
      if (replaceSlots) {
        nextSlots = result.slots as Slot[];
      } else {
        const merged = mergeAnalyzedSlots(currentSlots, result.slots as Slot[]);
        if (incrementalKind !== "tag") {
          nextSlots = merged;
        } else {
          const mergedByName = new Map(merged.map((slot) => [slot.file_name, slot]));
          nextSlots = currentSlots.map((slot) => slot.kind === "tag" ? mergedByName.get(slot.file_name) || slot : slot);
        }
      }
      const previousByName = new Map(currentSlots.map((slot) => [slot.file_name, slotPreviewSignature(slot, organizerProductInfo(), platformOverride)]));
      const changedNames = nextSlots
        .filter((slot) => previousByName.get(slot.file_name) !== slotPreviewSignature(slot, organizerProductInfo(), platformOverride))
        .map((slot) => slot.file_name);
      if (changedNames.length) {
        const changedKeys = changedNames.flatMap((fileName) => {
          const slot = nextSlots.find((item) => item.file_name === fileName);
          return slot ? previewFoldersForSlot(slot, platformOverride).map((folder) => slotPreviewKey(platformOverride, fileName, folder)) : [];
        });
        changedKeys.forEach((key) => delete slotPreviewSignaturesRef.current[key]);
        const workspace = platformWorkspaceRef.current[platformOverride];
        if (workspace) {
          const signatures = { ...workspace.signatures };
          changedKeys.forEach((key) => delete signatures[key]);
          platformWorkspaceRef.current[platformOverride] = { ...workspace, slots: nextSlots, signatures };
        }
      }
      slotsRef.current = nextSlots;
      setSlots(nextSlots);
      if (platformOverride === "vip" && incrementalKind !== "tag" && collections) {
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
      const nextSlots = mergeAnalyzedSlots(slotsRef.current, result.slots as Slot[]);
      const changedNames = nextSlots
        .filter((slot) => {
          const current = slotsRef.current.find((item) => item.file_name === slot.file_name);
          return !current || slotPreviewSignature(current, organizerProductInfo(), platform) !== slotPreviewSignature(slot, organizerProductInfo(), platform);
        })
        .map((slot) => slot.file_name);
      changedNames.forEach((fileName) => {
        const changedSlot = nextSlots.find((slot) => slot.file_name === fileName);
        previewFoldersForSlot(changedSlot!, platform).forEach((folder) => {
          delete slotPreviewSignaturesRef.current[slotPreviewKey(platform, fileName, folder)];
        });
      });
      slotsRef.current = nextSlots;
      setSlots(nextSlots);
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
    setSlots((current) => {
      const nextSlots = current.map((slot) => {
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
          folder_adjustments: undefined,
          confidence: 100,
          reason: linkedModelSlot ? `${linkedNames.join("与")}已同步使用同一张模特图` : "已由设计师人工确认",
        };
      });
      slotsRef.current = nextSlots;
      return nextSlots;
    });
  }

  function openAdjustmentEditor(
    fileName: string,
    sourceIndex = 0,
    targetFolder: PreviewFolder = "800",
    targetObject: "product" | "phone" = "product"
  ) {
    const slot = slots.find((item) => item.file_name === fileName);
    if (!slot?.image_ids[sourceIndex]) {
      setMessage("当前输出位置还没有可调整的来源图片");
      return;
    }
    setAdjustmentEditor({ fileName, sourceIndex, targetFolder, targetObject });
  }

  function saveSlotAdjustment(
    fileName: string,
    sourceIndex: number,
    targetFolder: PreviewFolder,
    adjustment: ImageAdjustment,
    logoColor: LogoColor,
    previewUrl?: string,
    syncJdFolders = false
  ) {
    const currentSlot = slotsRef.current.find((slot) => slot.file_name === fileName);
    if (!currentSlot) return;
    const currentFolderSlot = slotForPreviewFolder(currentSlot, platform, targetFolder);
    const adjustments = [...(currentFolderSlot.adjustments || [])];
    while (adjustments.length <= sourceIndex) adjustments.push({ ...DEFAULT_ADJUSTMENT });
    adjustments[sourceIndex] = normalizeAdjustment(adjustment);
    const normalizedAdjustments = adjustments.map((item) => normalizeAdjustment(item));
    const foldersToUpdate = platform === "jd" && syncJdFolders
      ? previewFoldersForSlot(currentSlot, platform)
      : [targetFolder];
    const folderAdjustments = { ...(currentSlot.folder_adjustments || {}) };
    foldersToUpdate.forEach((folder) => {
      folderAdjustments[folder] = normalizedAdjustments.map((item) => ({ ...item }));
    });
    const updatedSlot: Slot = {
      ...currentSlot,
      adjustments: targetFolder === "800" || syncJdFolders
        ? normalizedAdjustments
        : currentSlot.adjustments,
      folder_adjustments: platform === "jd" ? folderAdjustments : currentSlot.folder_adjustments,
      logo_color: logoColor
    };
    setSlots((current) => {
      const nextSlots = current.map((slot) => slot.file_name === fileName ? updatedSlot : slot);
      slotsRef.current = nextSlots;
      return nextSlots;
    });
    if (previewUrl) {
      const previewKey = slotPreviewKey(platform, fileName, targetFolder);
      slotPreviewSignaturesRef.current[previewKey] = slotPreviewSignature(
        slotForPreviewFolder(updatedSlot, platform, targetFolder),
        organizerProductInfo(),
        platform,
        targetFolder
      );
      setSlotPreviews((current) => ({ ...current, [previewKey]: previewUrl }));
    }
    setAdjustmentEditor(null);
    setMessage("");
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
    const productInfo = organizerProductInfo();
    if (platform === "jd" && !jdComparisonDimensionsReady(productInfo)) {
      setMessage("请先填写商品长和高，再下载京东套图");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const result = await api.exportVipOrganizer({
        session_id: sessionId,
        slots,
        product_info: productInfo,
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
    if (reanalyzeTimerRef.current !== null) {
      window.clearTimeout(reanalyzeTimerRef.current);
      reanalyzeTimerRef.current = null;
    }
    const scrollTop = window.scrollY;
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    platformWorkspaceRef.current[platform] = {
      slots: slotsRef.current,
      previews: slotPreviews,
      signatures: { ...slotPreviewSignaturesRef.current }
    };
    setAdjustmentEditor(null);
    const cached = platformWorkspaceRef.current[nextPlatform];
    if (cached) {
      slotsRef.current = cached.slots;
      setSlots(cached.slots);
      setSlotPreviews({ ...cached.previews });
      slotPreviewSignaturesRef.current = { ...cached.signatures };
      setPlatform(nextPlatform);
      setMessage(`已切换到${nextPlatform === "jd" ? "京东" : "唯品会"}，已恢复该平台预览；缺失项会单独补充。`);
      window.requestAnimationFrame(() => window.scrollTo({ top: scrollTop, left: 0, behavior: "auto" }));
      return;
    }
    setPlatformSwitching(true);
    setSlotPreviews({});
    slotPreviewSignaturesRef.current = {};
    setPlatform(nextPlatform);
    setMessage(`正在准备${nextPlatform === "jd" ? "京东" : "唯品会"}预览……`);
    try {
      if (productsRef.current.length) {
        await analyze(undefined, nextPlatform, assetTagsRef.current, undefined, undefined, true, true);
      }
    } finally {
      setPlatformSwitching(false);
      window.requestAnimationFrame(() => window.scrollTo({ top: scrollTop, left: 0, behavior: "auto" }));
    }
  }

  async function regenerateCurrentPlatform() {
    if (!productsRef.current.length) {
      setMessage("请先上传商品原图");
      return;
    }
    if (reanalyzeTimerRef.current !== null) {
      window.clearTimeout(reanalyzeTimerRef.current);
      reanalyzeTimerRef.current = null;
    }
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    setPlatformRegenerating(true);
    setAdjustmentEditor(null);
    setSlotPreviews({});
    slotPreviewSignaturesRef.current = {};
    delete platformWorkspaceRef.current[platform];
    setMessage(`正在清空并重新生成${platform === "jd" ? "京东" : "唯品会"}套图……`);
    try {
      await analyze(
        assetRolesRef.current,
        platform,
        assetTagsRef.current,
        undefined,
        undefined,
        true,
        true
      );
    } finally {
      setPlatformRegenerating(false);
    }
  }

  const activeEditorSlot = adjustmentEditor
    ? (() => {
      const slot = slots.find((item) => item.file_name === adjustmentEditor.fileName);
      return slot
        ? slotForPreviewFolder(slot, platform, adjustmentEditor.targetFolder)
        : undefined;
    })()
    : undefined;
  const activeEditorAsset = activeEditorSlot && adjustmentEditor
    ? selectedAsset(activeEditorSlot.image_ids[adjustmentEditor.sourceIndex])
    : undefined;
  const activeEditorPrimaryAsset = activeEditorSlot?.file_name === "606.jpg"
    ? selectedAsset(activeEditorSlot.image_ids[0])
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
      <header className="page-header organizer-page-header">
        <h1>自动化整理</h1>
      </header>

      <section className="panel organizer-source-panel">
        <div className="organizer-step-header">
          <div className="organizer-step-heading">
            <h2>1. 上传素材</h2>
            <p>先上传商品原图；模特图和吊牌图可按实际需要补充。</p>
          </div>
          <div className="button-row organizer-step-actions">
            {sessionId && <button disabled={busy} onClick={startNewSession}><RefreshCw size={18} />开始新一轮</button>}
            <button className="primary" disabled={busy || !products.length} onClick={() => analyze()}>
              {busy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}自动整理初稿
            </button>
          </div>
        </div>
        <UploadPasteControl
          disabled={busy}
          onUpload={(kind, files, skipped) => upload(kind, files, skipped)}
        />
        <div className="organizer-upload-columns">
          <UploadSection title="商品原图" hint="支持多选" items={products} disabled={busy} onUpload={(files) => upload("product", files)} onDelete={(item) => void deleteUploadedAsset("product", item)} onPreview={setPreview} />
          <UploadSection title="模特图" hint="支持多选" items={models} disabled={busy} onUpload={(files) => upload("model", files)} onDelete={(item) => void deleteUploadedAsset("model", item)} onPreview={setPreview} />
          <UploadSection title="吊牌图" hint="可选" items={tags} multiple={false} disabled={busy} onUpload={(files) => upload("tag", files)} onDelete={(item) => void deleteUploadedAsset("tag", item)} onPreview={setPreview} />
        </div>
      </section>

      {slots.length > 0 && <>
        <section className="panel organizer-analysis-panel">
          <div className="organizer-step-header organizer-analysis-header">
            <div className="organizer-step-heading">
              <h2>2. 素材分析</h2>
              <p>核对自动分类和细节标签，确认后可按最新结果重新整理。</p>
            </div>
            <div className="organizer-analysis-toolbar organizer-step-actions">
              <label className="organizer-api-select">
                <span>图文分析 API</span>
                <select value={analysisConfigId} onChange={(event) => setAnalysisConfigId(Number(event.target.value) || "")}>
                  {!analysisConfigs.length && <option value="">暂无图文分析 API</option>}
                  {analysisConfigs.map((item) => <option value={item.id} key={item.id}>{item.config_name}{item.is_default ? "（默认）" : ""}</option>)}
                </select>
              </label>
              <button disabled={busy || !analysisConfigId} onClick={analyzeWithApi}><RefreshCw size={18} />API 分析</button>
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
          <div className="organizer-step-header organizer-step-header-simple">
            <div className="organizer-step-heading">
              <h2>3. 商品信息</h2>
              <p>尺寸统一填写毫米（mm），用于产品信息图和尺寸对比图。</p>
            </div>
          </div>
          <div className="organizer-info-grid">
            <label>商品名称<input value={info.product_name} onChange={(event) => setInfo({ ...info, product_name: event.target.value })} /></label>
            <label>长（mm）<input inputMode="decimal" placeholder="例如：200" value={info.product_length} onChange={(event) => setInfo({ ...info, product_length: event.target.value })} /></label>
            <label>宽（mm）<input inputMode="decimal" placeholder="例如：80" value={info.product_width} onChange={(event) => setInfo({ ...info, product_width: event.target.value })} /></label>
            <label>高（mm）<input inputMode="decimal" placeholder="例如：140" value={info.product_height} onChange={(event) => setInfo({ ...info, product_height: event.target.value })} /></label>
            <label>主要材质<input value={info.main_material} onChange={(event) => setInfo({ ...info, main_material: event.target.value })} /></label>
            <label>里料材质<input value={info.lining_material} onChange={(event) => setInfo({ ...info, lining_material: event.target.value })} /></label>
            <label>包型背法<input placeholder="例如：单肩/斜挎" value={info.wearing_method} onChange={(event) => setInfo({ ...info, wearing_method: event.target.value })} /></label>
            <label className="wide">免责声明<textarea rows={2} value={info.disclaimer} onChange={(event) => setInfo({ ...info, disclaimer: event.target.value })} /></label>
          </div>
        </section>

        <section className="panel organizer-slots-panel">
          <div className="organizer-platform-switcher" aria-label="输出平台">
            <div><strong>输出平台</strong></div>
            <div className="organizer-platform-actions">
              <div className="organizer-platform-tabs" role="tablist" aria-label="选择输出平台">
                {ORGANIZER_PLATFORMS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={item.id === platform}
                    className={item.id === platform ? "active" : ""}
                    disabled={busy || platformSwitching || platformRegenerating}
                    onClick={() => changePlatform(item.id)}
                  >
                    <span>{item.label}</span>
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="organizer-platform-refresh"
                disabled={busy || platformSwitching || platformRegenerating || !products.length}
                title="清空当前平台的手动选图和调整，按最新标签重新自动生成"
                onClick={() => void regenerateCurrentPlatform()}
              >
                {platformRegenerating ? <LoaderCircle className="spin" size={17} /> : <RefreshCw size={17} />}
                {platformRegenerating ? "正在重新生成" : "刷新当前平台"}
              </button>
            </div>
          </div>
          <div className="organizer-step-header">
            <div className="organizer-step-heading">
              <h2>4. 检查{platform === "jd" ? "京东 7 个" : "15 个"}输出位置</h2>
              <p>逐项确认成品、来源图片和调整结果后再导出。</p>
            </div>
            {(previewBusy || platformSwitching || platformRegenerating) && <span className="organizer-preview-status organizer-step-actions"><LoaderCircle className="spin" size={16} />{platformSwitching ? "正在切换输出平台" : platformRegenerating ? "正在重新生成当前平台" : "正在更新成品预览"}</span>}
          </div>
          <div className="organizer-preview-groups">
            {previewGroups.map((group) => <section className="organizer-preview-group" key={group.folder}>
              {group.label && <header className="organizer-preview-group-header">
                <div><strong>{group.label}</strong><span>{group.description}</span></div>
                <small>{group.slots.length} 张</small>
              </header>}
              <div className="organizer-slot-grid">
                {group.slots.map((slot) => {
                  const count = slot.file_name === "606.jpg" ? 4 : 1;
                  const isLockedInfoFront = platform === "vip" && slot.file_name === "401.jpg";
                  const editableSource = !isLockedInfoFront && slot.kind !== "generated";
                  const previewKey = slotPreviewKey(platform, slot.file_name, group.folder);
                  const dimensionsReady = jdComparisonDimensionsReady(organizerProductInfo());
                  const outputReady = !((platform === "jd" && slot.file_name === "5.jpg")
                    || (platform === "vip" && slot.file_name === "401.jpg")) || dimensionsReady;
                  const renderedPreview = outputReady ? slotPreviews[previewKey] : undefined;
                  const outputSize = slotCanvasSize(slot.size, platform, group.folder);
                  return <article className={`organizer-slot${slot.file_name === "606.jpg" ? " is-composite" : ""}`} key={previewKey}>
                    <div
                      className="organizer-slot-preview"
                      style={{ aspectRatio: `${outputSize.width} / ${outputSize.height}` }}
                    >
                      {renderedPreview
                        ? <>
                          <button className="organizer-slot-edit-preview" type="button" onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder)} aria-label={`调整 ${slot.file_name} 最终成品`}><img src={renderedPreview} alt={`${slot.file_name} 最终成品`} onError={() => {
                              setSlotPreviews((current) => {
                                const next = { ...current };
                                delete next[previewKey];
                                return next;
                              });
                              delete slotPreviewSignaturesRef.current[previewKey];
                              setPreviewRetryVersion((current) => current + 1);
                            }} /></button>
                          <button
                            className="organizer-slot-view-preview"
                            type="button"
                            title={`查看 ${slot.file_name} 大图`}
                            aria-label={`查看 ${slot.file_name} 放大图片`}
                            onClick={() => setPreview(renderedPreview)}
                          >
                            <Eye size={18} />
                          </button>
                        </>
                        : <div className="generated-placeholder"><FileImage size={30} /><span>{!outputReady ? "请先填写商品长和高" : previewBusy ? "正在套用模板" : "缺少素材"}</span></div>}
                    </div>
                    <div className="organizer-slot-body">
                      <div className="organizer-slot-title">
                        <span className="organizer-slot-title-text"><strong>{slot.file_name}</strong><span title={slot.title}>{slotDisplayTitle(platform, slot.file_name, slot.title)}</span></span>
                        <small>{outputSize.width}×{outputSize.height}</small>
                      </div>
                      {count === 1 && platform === "jd" && slot.file_name === "5.jpg" ? <div className="organizer-object-adjustments" role="group" aria-label="尺寸对比图调整对象">
                        <button
                          type="button"
                          className="organizer-adjust-output"
                          disabled={!slot.image_ids[0] || !outputReady}
                          onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder, "product")}
                        ><Crop size={16} />调整商品图</button>
                        <button
                          type="button"
                          className="organizer-adjust-output"
                          disabled={!slot.image_ids[0] || !outputReady}
                          onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder, "phone")}
                        ><Smartphone size={16} />调整手机</button>
                      </div> : count === 1 && <button
                        type="button"
                        className="organizer-adjust-output"
                        disabled={!slot.image_ids[0] || !outputReady}
                        onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder)}
                      ><Crop size={16} />调整成品</button>}
                      {isLockedInfoFront && <label>来源图片
                        <span className="organizer-source-picker">
                          <select value={slot.image_ids[0] || ""} disabled aria-label="401固定优先使用透明正面图">
                            <option value={slot.image_ids[0] || ""}>{selectedAsset(slot.image_ids[0]) ? assetOptionLabel(selectedAsset(slot.image_ids[0]), "product") : "透明正面图"}</option>
                          </select>
                        </span>
                      </label>}
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
            <button className="primary" disabled={busy || previewBusy} onClick={exportZip}>{busy ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />}下载 ZIP</button>
          </div>
        </section>
      </>}

      {message && <div className="alert warning organizer-status-message" role="status">{message}</div>}
      {preview && <div className="image-modal" role="dialog" aria-modal="true" aria-label="放大图片预览" onClick={() => setPreview(null)}>
        <button className="image-modal-close" type="button" onClick={() => setPreview(null)} aria-label="关闭预览"><X size={22} /></button>
        <img src={preview} alt="图片预览" onClick={(event) => event.stopPropagation()} />
      </div>}
      {adjustmentEditor && activeEditorSlot && activeEditorAsset && <SlotAdjustmentEditor
        key={`${platform}:${adjustmentEditor.targetFolder}:${activeEditorSlot.file_name}:${adjustmentEditor.sourceIndex}:${adjustmentEditor.targetObject}:${activeEditorAsset.id}`}
        sessionId={sessionId}
        slot={activeEditorSlot}
        sourceIndex={adjustmentEditor.sourceIndex}
        sourceUrl={activeEditorAsset.original_url || activeEditorAsset.preview_url}
        compositePrimaryUrl={activeEditorPrimaryAsset?.original_url || activeEditorPrimaryAsset?.preview_url}
        displaySourceUrl={adjustmentEditor.targetObject === "phone" ? "/organizer-assets/iphone_reference.png" : undefined}
        initialPreview={slotPreviews[slotPreviewKey(platform, activeEditorSlot.file_name, adjustmentEditor.targetFolder)]}
        productInfo={organizerProductInfo()}
        platform={platform}
        targetFolder={adjustmentEditor.targetFolder}
        initialMoveTarget={adjustmentEditor.targetObject}
        onClose={() => setAdjustmentEditor(null)}
        onSave={(adjustment, logoColor, previewUrl, syncJdFolders) => saveSlotAdjustment(
          activeEditorSlot.file_name,
          adjustmentEditor.sourceIndex,
          adjustmentEditor.targetFolder,
          adjustment,
          logoColor,
          previewUrl,
          syncJdFolders
        )}
      />}
    </section>
  );
}
