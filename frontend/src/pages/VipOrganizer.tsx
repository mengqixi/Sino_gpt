import { Crop, Download, Eye, FileImage, LoaderCircle, Move, RefreshCw, RotateCcw, Save, Smartphone, UploadCloud, X, ZoomIn, ZoomOut } from "lucide-react";
import type { DragEvent, PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
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
  folder_logo_colors?: Partial<Record<PreviewFolder, LogoColor>>;
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
  phone_label_scale?: number;
  phone_label_offset_x?: number;
  phone_label_offset_y?: number;
  phone_label_linked?: boolean;
  phone_alignment?: "center" | "bottom";
  product_show_ruler?: boolean;
  phone_show_ruler?: boolean;
  product_ruler_gap_scale?: number;
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
type OrganizerLayerInfo = {
  url: string;
  width: number;
  height: number;
  measurement_bbox: [number, number, number, number];
  product_body_bbox: [number, number, number, number];
  handle_lift: number;
};
type AdjustmentTarget = "product" | "phone" | "phone_label" | "length_ruler" | "height_ruler" | "width_ruler" | "phone_ruler";
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
const JD_SLOT_FILES = ["0-无logo.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg", "透明.png"];
const JD_MEASURE_COLOR = "#707070";
const JD_PHONE_SCALE_MAX = 4;
const JD_DECORATION_SCALE_MIN = 0.125;
const JD_DECORATION_SCALE_MAX = 4;
const JD_DECORATION_OFFSET_MAX = 8;
const ORGANIZER_CANVAS_FONT = '"OrganizerNotoSans"';
// Keep persisted preview signatures aligned with backend PREVIEW_RENDER_VERSION
// and ORGANIZER_LAYER_RENDER_VERSION. Bump this token whenever either renderer
// changes so a refresh cannot revive an older exact preview from sessionStorage.
const ORGANIZER_RENDER_STATE_VERSION = "34:1";
const ORGANIZER_SESSION_SNAPSHOT_VERSION = 2;

let organizerCanvasFontsReady: Promise<unknown> | null = null;
let organizerPreviewGeneration = Date.now() * 1000;

function nextOrganizerPreviewGeneration() {
  organizerPreviewGeneration = Math.max(
    organizerPreviewGeneration + 1,
    Date.now() * 1000
  );
  return organizerPreviewGeneration;
}

function ensureOrganizerCanvasFonts() {
  if (!organizerCanvasFontsReady) {
    organizerCanvasFontsReady = Promise.all([
      document.fonts.load(`400 19px ${ORGANIZER_CANVAS_FONT}`),
      document.fonts.load(`500 12px ${ORGANIZER_CANVAS_FONT}`),
      document.fonts.load(`700 32px ${ORGANIZER_CANVAS_FONT}`)
    ]);
  }
  return organizerCanvasFontsReady;
}

function jdMeasureFont(output: { width: number; height: number }) {
  return `400 ${Math.max(14, Math.round(Math.min(output.width, output.height) * 0.022))}px ${ORGANIZER_CANVAS_FONT}`;
}

function jdPhoneLabelFontSize(
  output: { width: number; height: number },
  phoneHeight: number,
  labelScale = 1
) {
  const regularSize = Math.max(12, Math.round(Math.min(output.width, output.height) * 0.017));
  const adaptiveSize = Math.max(10, Math.min(regularSize, Math.round(phoneHeight * 0.085)));
  return Math.max(8, Math.round(adaptiveSize * labelScale));
}

function jdPhoneLabelFont(
  output: { width: number; height: number },
  phoneHeight: number,
  labelScale = 1
) {
  const scaledSize = jdPhoneLabelFontSize(output, phoneHeight, labelScale);
  // Small text loses noticeably more coverage than the larger ruler labels
  // after browser/image antialiasing. Medium keeps the same #707070 ink while
  // matching their perceived darkness at the final rendered size.
  return `500 ${scaledSize}px ${ORGANIZER_CANVAS_FONT}`;
}

function jdPhoneLabelGap(output: { width: number; height: number }, phoneHeight: number) {
  const referenceHeight = Math.min(output.width, output.height) * 0.22;
  const phoneScale = Math.max(0.65, Math.min(1.5, phoneHeight / referenceHeight));
  return Math.max(6, Math.round(Math.min(output.width, output.height) * 0.015 * phoneScale));
}

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
  phone_label_scale: 1,
  phone_label_offset_x: 0,
  phone_label_offset_y: 0,
  phone_label_linked: true,
  phone_alignment: "bottom",
  product_show_ruler: true,
  phone_show_ruler: true,
  product_ruler_gap_scale: 1,
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
const JD_PHONE_ASPECT_RATIO = 553 / 710;
const VIP_INFO_PRODUCT_SCALE = 1;
const VIP_INFO_HANDLE_SCALE = 0.08;
const VIP_INFO_HANDLE_LIFT_Y = 0.04;
const VIP_INFO_NO_HANDLE_DROP_Y = 0.028;
const VIP_INFO_WIDTH_EDGE_SAFE_RIGHT = 714;
const VIP_INFO_WIDTH_RULER_ALLOWANCE = 84;
const VIP_INFO_WIDTH_EDGE_RANGE = 36;
const VIP_INFO_WIDTH_EDGE_MAX_SHRINK = 0.08;
const VIP_INFO_WIDTH_EDGE_MAX_SHIFT_X = 16;
const VIP_INFO_TEXT_X = 53;
const VIP_INFO_HEIGHT_RULER_SHIFT_Y = 5;
const VIP_INFO_RULER_COLOR = "#8a8a8a";
const IMAGE_PREVIEW_ZOOM_MIN = 0.5;
const IMAGE_PREVIEW_ZOOM_MAX = 5;
const IMAGE_PREVIEW_ZOOM_STEP = 0.25;

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

function productThickness(productInfo: Record<string, string>) {
  return productInfo.product_thickness || productInfo.product_width || "";
}

function positiveDimensionValue(value: string | undefined) {
  const normalized = String(value || "").trim();
  if (!/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(normalized)) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function dimensionMmLabel(value: string | undefined, fallback = "--mm") {
  const parsed = positiveDimensionValue(value);
  if (parsed === null) return fallback;
  // Keep browser labels on the same positive-number rounding contract as the
  // exact renderer: ties round away from zero (Decimal ROUND_HALF_UP).
  const rounded = Math.floor((parsed + Number.EPSILON) * 10 + 0.5) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}mm`;
}

function vipInfoProductScale(handleLift = 0) {
  return VIP_INFO_PRODUCT_SCALE * (1 + VIP_INFO_HANDLE_SCALE * Math.max(0, Math.min(1, handleLift)));
}

function vipInfoProductLiftY(handleLift = 0) {
  return VIP_INFO_HANDLE_LIFT_Y * Math.max(0, Math.min(1, handleLift));
}

function vipInfoAutoLayout(
  layerWidth: number,
  layerHeight: number,
  body: PixelBounds,
  handleLift = 0
) {
  const areaWidth = VIP_INFO_PRODUCT_BOX.right - VIP_INFO_PRODUCT_BOX.left;
  const areaHeight = VIP_INFO_PRODUCT_BOX.bottom - VIP_INFO_PRODUCT_BOX.top;
  const boundedHandleLift = Math.max(0, Math.min(1, handleLift));
  const baseScale = Math.min(
    areaWidth / Math.max(1, layerWidth),
    areaHeight / Math.max(1, layerHeight)
  ) * vipInfoProductScale(boundedHandleLift);
  const baseX = VIP_INFO_PRODUCT_BOX.left + (areaWidth - layerWidth * baseScale) / 2;
  const projectedWidthRulerRight = baseX + body.right * baseScale + VIP_INFO_WIDTH_RULER_ALLOWANCE;
  const edgePressure = Math.max(0, Math.min(
    1,
    (projectedWidthRulerRight - VIP_INFO_WIDTH_EDGE_SAFE_RIGHT) / VIP_INFO_WIDTH_EDGE_RANGE
  ));
  const noHandleWeight = 1 - Math.min(1, boundedHandleLift / 0.35);
  return {
    scale: 1 - VIP_INFO_WIDTH_EDGE_MAX_SHRINK * edgePressure,
    shiftX: -VIP_INFO_WIDTH_EDGE_MAX_SHIFT_X * edgePressure,
    dropY: VIP_INFO_NO_HANDLE_DROP_Y * noHandleWeight
  };
}

function targetScale(draft: ImageAdjustment, target: AdjustmentTarget) {
  if (target === "phone") return draft.phone_scale || 1;
  if (target === "phone_label") return draft.phone_label_scale || 1;
  if (target === "length_ruler") return draft.length_ruler_scale || 1;
  if (target === "height_ruler") return draft.height_ruler_scale || 1;
  if (target === "width_ruler") return draft.width_ruler_scale || 1;
  if (target === "phone_ruler") return draft.phone_ruler_scale || 1;
  return draft.zoom;
}

function targetScaleLimits(target: AdjustmentTarget) {
  if (target === "product" || target === "phone") return { minimum: 0.5, maximum: JD_PHONE_SCALE_MAX };
  if (target === "phone_ruler" || target === "phone_label") {
    return { minimum: JD_DECORATION_SCALE_MIN, maximum: JD_DECORATION_SCALE_MAX };
  }
  return { minimum: 0.5, maximum: 2 };
}

function targetOffsetLimit(target: AdjustmentTarget) {
  if (target === "phone_ruler" || target === "phone_label") return JD_DECORATION_OFFSET_MAX;
  return 1.5;
}

function targetOffset(draft: ImageAdjustment, target: AdjustmentTarget) {
  if (target === "phone") return { x: draft.phone_offset_x || 0, y: draft.phone_offset_y || 0 };
  if (target === "phone_label") return { x: draft.phone_label_offset_x || 0, y: draft.phone_label_offset_y || 0 };
  if (target === "length_ruler") return { x: draft.length_ruler_offset_x || 0, y: draft.length_ruler_offset_y || 0 };
  if (target === "height_ruler") return { x: draft.height_ruler_offset_x || 0, y: draft.height_ruler_offset_y || 0 };
  if (target === "width_ruler") return { x: draft.width_ruler_offset_x || 0, y: draft.width_ruler_offset_y || 0 };
  if (target === "phone_ruler") return { x: draft.phone_ruler_offset_x || 0, y: draft.phone_ruler_offset_y || 0 };
  return { x: draft.offset_x, y: draft.offset_y };
}

function withTargetScale(draft: ImageAdjustment, target: AdjustmentTarget, scale: number): ImageAdjustment {
  if (target === "phone") return { ...draft, phone_scale: scale };
  if (target === "phone_label") return { ...draft, phone_label_scale: scale };
  if (target === "length_ruler") return { ...draft, length_ruler_scale: scale };
  if (target === "height_ruler") return { ...draft, height_ruler_scale: scale };
  if (target === "width_ruler") return { ...draft, width_ruler_scale: scale };
  if (target === "phone_ruler") return { ...draft, phone_ruler_scale: scale };
  return { ...draft, zoom: scale };
}

function withTargetOffset(draft: ImageAdjustment, target: AdjustmentTarget, x: number, y: number): ImageAdjustment {
  if (target === "phone") return { ...draft, phone_offset_x: x, phone_offset_y: y };
  if (target === "phone_label") return { ...draft, phone_label_offset_x: x, phone_label_offset_y: y };
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

function modelDragOffsetWithBoundaryResistance(start: number, delta: number) {
  const softBoundary = 0.12;
  const resistance = 0.35;
  const projected = start + delta;
  if (Math.abs(projected) <= softBoundary || Math.abs(projected) <= Math.abs(start)) {
    return projected;
  }
  const direction = Math.sign(projected) || 1;
  if (Math.abs(start) >= softBoundary && Math.sign(start) === direction) {
    return start + delta * resistance;
  }
  const boundary = direction * softBoundary;
  return boundary + (projected - boundary) * resistance;
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

    // A designer-selected source is authoritative, including JD 5.jpg. An
    // automatic re-analysis must not silently replace that image.
    const preserveManualSources = isManuallyConfirmedSlot(previous);
    const imageIds = preserveManualSources ? previous.image_ids : nextSlot.image_ids;
    const sourceUnchanged = imageIds.length === previous.image_ids.length
      && imageIds.every((imageId, index) => previous.image_ids[index] === imageId);
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
      // Folder adjustments are tied to the source image. Reusing them after an
      // automatic source change applies the old crop/position to a new photo.
      folder_adjustments: sourceUnchanged ? previous.folder_adjustments : undefined,
      folder_logo_colors: previous.folder_logo_colors,
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
  const {
    folder_adjustments: folderAdjustments,
    folder_logo_colors: folderLogoColors,
    ...baseSlot
  } = slot;
  return {
    ...baseSlot,
    adjustments: folderAdjustments?.[targetFolder] || slot.adjustments,
    logo_color: folderLogoColors?.[targetFolder] || slot.logo_color
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

function adjustmentForSyncedFolder(
  adjustment: ImageAdjustment,
  targetAdjustment: ImageAdjustment | undefined,
  slot: Slot,
  platform: OrganizerPlatform,
  sourceFolder: PreviewFolder,
  targetFolder: PreviewFolder
) {
  if (platform !== "jd" || sourceFolder === targetFolder) return { ...adjustment };
  if (slot.file_name !== "5.jpg") return { ...adjustment };

  // JD 800 and 750 use independent comparison-layout algorithms. Their
  // product bodies are not related by a simple canvas-width/height ratio, so
  // copying or scaling an absolute ruler baseline makes the target folder
  // jump. Linked rulers rebuild from the target folder's own rendered body;
  // detached rulers retain only a baseline that was already saved for that
  // target folder.
  const detachedTarget = adjustment.product_show_ruler === false
    ? targetAdjustment
    : undefined;
  return {
    ...adjustment,
    product_ruler_base_left: detachedTarget?.product_ruler_base_left,
    product_ruler_base_top: detachedTarget?.product_ruler_base_top,
    product_ruler_base_right: detachedTarget?.product_ruler_base_right,
    product_ruler_base_bottom: detachedTarget?.product_ruler_base_bottom
  };
}

function slotUsesOrganizerLayer(slot: Slot, platform: OrganizerPlatform) {
  return platform === "jd"
    ? ["2.jpg", "4.jpg", "5.jpg", "透明.png"].includes(slot.file_name)
    : ["2.jpg", "3.jpg", "15.jpg", "30.png", "401.jpg", "604.jpg", "605.jpg", "606.jpg"].includes(slot.file_name);
}

function slotUsesAutoHandleLayout(slot: Slot, platform: OrganizerPlatform) {
  return platform === "jd"
    ? ["2.jpg", "透明.png"].includes(slot.file_name)
    : ["2.jpg", "3.jpg", "30.png"].includes(slot.file_name);
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
  if (platform === "vip" && slot.file_name === "606.jpg") {
    const areas = [
      { x: 30 / 750, y: 135 / 750, width: 345 / 750, height: 285 / 750 },
      { x: 375 / 750, y: 135 / 750, width: 345 / 750, height: 285 / 750 },
      { x: 30 / 750, y: 420 / 750, width: 345 / 750, height: 300 / 750 },
      { x: 375 / 750, y: 420 / 750, width: 345 / 750, height: 300 / 750 }
    ];
    return areas[sourceIndex] || areas[0];
  }
  if (slot.file_name.endsWith(".png")) return { x: 0.04, y: 0.04, width: 0.92, height: 0.92 };
  const template = slotPreviewLayout(slot, platform, sourceIndex, targetFolder);
  if (template.x <= 0.04 && template.y <= 0.04 && template.x + template.width >= 0.96 && template.y + template.height >= 0.96) {
    return template;
  }
  const padding = ["604.jpg", "605.jpg"].includes(slot.file_name) ? 0.14 : 0.055;
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

function autoHandleBaselineOrigin(
  platform: OrganizerPlatform,
  fileName: string,
  output: { width: number; height: number },
  position: { x: number; y: number },
  size: { width: number; height: number },
  clip: { left: number; top: number; right: number; bottom: number }
) {
  const x = clampLayerOrigin(position.x, size.width, clip.left, clip.right);
  let y = clampLayerOrigin(position.y, size.height, clip.top, clip.bottom);
  if (platform === "jd" && fileName === "2.jpg") {
    const minimumTop = output.width === 800 && output.height === 800 ? 162 : 175;
    const maximumBottom = output.width === 800 && output.height === 800 ? 740 : 930;
    const latestY = maximumBottom - size.height;
    y = latestY >= minimumTop
      ? Math.max(minimumTop, Math.min(y, latestY))
      : Math.max(y, minimumTop);
  }
  return { x, y };
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
const livePreviewRawCutoutCache = new Map<string, HTMLCanvasElement>();
const livePreviewCutoutCache = new Map<string, HTMLCanvasElement>();
const livePreparedProductCache = new Map<string, HTMLCanvasElement>();
const livePreviewLightBorderCache = new Map<string, boolean>();
let liveHandleLiftCache = new WeakMap<HTMLCanvasElement, number>();
type PixelBounds = { left: number; top: number; right: number; bottom: number };
type LiveProductLayer = { canvas: HTMLCanvasElement; body: PixelBounds; handleLift?: number };
const liveJdProductLayerCache = new Map<string, LiveProductLayer>();

function clearLivePreviewCaches() {
  livePreviewImageCache.clear();
  livePreviewBoundsCache.clear();
  livePreviewRawCutoutCache.clear();
  livePreviewCutoutCache.clear();
  livePreparedProductCache.clear();
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

function waitForLivePreviewImage(image: HTMLImageElement, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      image.removeEventListener("load", loaded);
      image.removeEventListener("error", failed);
      signal.removeEventListener("abort", aborted);
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const decoded = () => {
      void image.decode().catch(() => undefined).then(() => {
        if (signal.aborted) aborted();
        else if (image.naturalWidth > 0) finish();
        else finish(new Error("精确商品图层加载失败"));
      });
    };
    const loaded = () => decoded();
    const failed = () => finish(new Error("精确商品图层加载失败"));
    const aborted = () => finish(new DOMException("Prepared layer request aborted", "AbortError"));
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) {
      aborted();
      return;
    }
    if (image.complete) {
      if (image.naturalWidth > 0) decoded();
      else failed();
      return;
    }
    image.addEventListener("load", loaded, { once: true });
    image.addEventListener("error", failed, { once: true });
  });
}

function preloadExactPreview(url: string, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    let settled = false;
    const cleanup = () => {
      image.onload = null;
      image.onerror = null;
      signal.removeEventListener("abort", abort);
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const abort = () => finish(new DOMException("Preview request aborted", "AbortError"));
    image.onload = () => {
      void image.decode().catch(() => undefined).then(() => {
        if (signal.aborted) abort();
        else finish();
      });
    };
    image.onerror = () => finish(new Error("精确预览图片加载失败"));
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    image.src = url;
  });
}

function preparedProductCutout(url: string, image: HTMLImageElement) {
  const cached = livePreparedProductCache.get(url);
  if (cached) return cached;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, image.naturalWidth);
  canvas.height = Math.max(1, image.naturalHeight);
  canvas.getContext("2d")?.drawImage(image, 0, 0);
  livePreparedProductCache.set(url, canvas);
  return canvas;
}

function livePreviewRawProductCutout(url: string, image: HTMLImageElement) {
  const cached = livePreviewRawCutoutCache.get(url);
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
  livePreviewRawCutoutCache.set(url, canvas);
  return canvas;
}

function livePreviewProductCutout(url: string, image: HTMLImageElement) {
  const cached = livePreviewCutoutCache.get(url);
  if (cached) return cached;
  const raw = livePreviewRawProductCutout(url, image);
  const bounds = liveInfoMeasurementBounds(raw);
  const objectWidth = Math.max(1, bounds.right - bounds.left);
  const objectHeight = Math.max(1, bounds.bottom - bounds.top);
  const padding = Math.max(3, Math.round(Math.max(objectWidth, objectHeight) * 0.015));
  const left = Math.max(0, bounds.left - padding);
  const top = Math.max(0, bounds.top - padding);
  const right = Math.min(raw.width, bounds.right + padding);
  const bottom = Math.min(raw.height, bounds.bottom + padding);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, right - left);
  canvas.height = Math.max(1, bottom - top);
  canvas.getContext("2d")?.drawImage(
    raw,
    left,
    top,
    canvas.width,
    canvas.height,
    0,
    0,
    canvas.width,
    canvas.height
  );
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

function infoRulerGeometry(body: PixelBounds, gapScale = 1) {
  const rulerGap = 34 * gapScale;
  const left = Math.round(body.left + 4);
  const right = Math.round(body.right - 4);
  // The detected alpha boundary includes anti-aliased edge/shadow pixels.
  // Keep the long-standing calibrated insets so the rulers follow the visible
  // leather body rather than the faint outer fringe.
  const bottom = Math.round(body.bottom - 9);
  const top = Math.round(body.top - 5);
  return {
    left,
    right,
    top,
    bottom,
    verticalX: left - rulerGap,
    horizontalY: bottom + rulerGap
  };
}

function drawVipInfoStaticPreview(
  context: CanvasRenderingContext2D,
  productInfo: Record<string, string>
) {
  context.save();
  context.fillStyle = "#101010";
  context.font = `700 32px ${ORGANIZER_CANVAS_FONT}`;
  context.textAlign = "left";
  context.textBaseline = "top";
  context.fillText("\u4ea7\u54c1\u4fe1\u606f", 290, 40);

  const rows = [
    ["\u6750\u8d28", productInfo.main_material || "\u5f85\u586b\u5199"],
    ["\u91cc\u6599", productInfo.lining_material || "\u5f85\u586b\u5199"],
    ["\u80cc\u6cd5", productInfo.wearing_method || "\u5f85\u586b\u5199"]
  ];
  rows.forEach(([label, value], index) => {
    const y = 216 + index * 96;
    context.fillStyle = "#111111";
    context.font = `700 20px ${ORGANIZER_CANVAS_FONT}`;
    context.fillText(label, VIP_INFO_TEXT_X, y);
    context.fillStyle = "#555555";
    context.font = `400 19px ${ORGANIZER_CANVAS_FONT}`;
    context.fillText(value.slice(0, 18), VIP_INFO_TEXT_X, y + 34);
  });
  context.restore();
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
  const rulerGap = 34 * (draft.product_ruler_gap_scale || 1);
  const anchorDirection = Math.hypot(22, 18);
  const start = {
    x: baseBody.right + 22 / anchorDirection * rulerGap,
    y: baseBody.bottom + 18 / anchorDirection * rulerGap
  };
  const end = { x: start.x + 51, y: start.y - 27 };
  const deltaX = end.x - start.x;
  const deltaY = end.y - start.y;
  const length = Math.max(1, Math.hypot(deltaX, deltaY));
  const perpendicular = { x: -deltaY / length * 9, y: deltaX / length * 9 };
  const rawSegments = [
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
  const groupedSegments = rawSegments.map(([segmentStart, segmentEnd]) => {
    const grouped = transformCanvasRulerSegment(
      segmentStart,
      segmentEnd,
      draft.product_ruler_group_scale || 1,
      draft.product_ruler_group_offset_x || 0,
      draft.product_ruler_group_offset_y || 0,
      output,
      productCenter
    );
    return [grouped.start, grouped.end] as const;
  });
  const groupedMain = groupedSegments[0];
  const widthOrigin = {
    x: (groupedMain[0].x + groupedMain[1].x) / 2,
    y: (groupedMain[0].y + groupedMain[1].y) / 2
  };
  const segments = groupedSegments.map(([segmentStart, segmentEnd]) => {
    const transformed = transformCanvasRulerSegment(
      segmentStart,
      segmentEnd,
      draft.width_ruler_scale || 1,
      draft.width_ruler_offset_x || 0,
      draft.width_ruler_offset_y || 0,
      output,
      widthOrigin
    );
    return [transformed.start, transformed.end] as const;
  });
  const transformedMain = segments[0];
  const transformedDeltaX = transformedMain[1].x - transformedMain[0].x;
  const transformedDeltaY = transformedMain[1].y - transformedMain[0].y;
  const transformedLength = Math.max(1, Math.hypot(transformedDeltaX, transformedDeltaY));
  const labelNormal = {
    x: -transformedDeltaY / transformedLength,
    y: transformedDeltaX / transformedLength
  };
  const text = {
    x: (transformedMain[0].x + transformedMain[1].x) / 2 + labelNormal.x * 26,
    y: (transformedMain[0].y + transformedMain[1].y) / 2 + labelNormal.y * 26
  };
  return {
    segments,
    text
  };
}

function liveJdProductLayer(url: string, image: HTMLImageElement, draft: ImageAdjustment): LiveProductLayer {
  const cropKey = [draft.crop_x, draft.crop_y, draft.crop_width, draft.crop_height]
    .map((value) => value.toFixed(5))
    .join(":");
  const key = `${url}|${cropKey}`;
  const cached = liveJdProductLayerCache.get(key);
  if (cached) return cached;
  // Crop coordinates are stored against the uploaded image, so manual crops
  // must start from the full-size cutout rather than the already trimmed layer.
  const cutout = livePreviewRawProductCutout(url, image);
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
  const layer = {
    canvas,
    body: liveInfoMeasurementBounds(canvas),
    handleLift: liveHandleVisualLift(canvas)
  };
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

function organizerLayerBounds(bounds: OrganizerLayerInfo["measurement_bbox"] | undefined): PixelBounds | null {
  if (!bounds) return null;
  const [left, top, right, bottom] = bounds;
  return right > left && bottom > top ? { left, top, right, bottom } : null;
}

function preparedOrganizerProductLayer(
  info: OrganizerLayerInfo,
  image: HTMLImageElement
): LiveProductLayer {
  const canvas = preparedProductCutout(info.url, image);
  return {
    canvas,
    handleLift: info.handle_lift,
    body: organizerLayerBounds(info.product_body_bbox)
      || organizerLayerBounds(info.measurement_bbox)
      || { left: 0, top: 0, right: canvas.width, bottom: canvas.height }
  };
}

function organizerProductGeometryLayer(info: OrganizerLayerInfo): LiveProductLayer {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, info.width);
  canvas.height = Math.max(1, info.height);
  return {
    canvas,
    handleLift: info.handle_lift,
    body: organizerLayerBounds(info.product_body_bbox)
      || organizerLayerBounds(info.measurement_bbox)
      || { left: 0, top: 0, right: canvas.width, bottom: canvas.height }
  };
}

function positionedInfoProductBody(
  layerWidth: number,
  layerHeight: number,
  layerBounds: PixelBounds,
  draft: ImageAdjustment,
  handleLift = 0
): PixelBounds {
  const areaX = VIP_INFO_PRODUCT_BOX.left;
  const areaY = VIP_INFO_PRODUCT_BOX.top;
  const areaWidth = VIP_INFO_PRODUCT_BOX.right - VIP_INFO_PRODUCT_BOX.left;
  const areaHeight = VIP_INFO_PRODUCT_BOX.bottom - VIP_INFO_PRODUCT_BOX.top;
  const fitScale = Math.min(areaWidth / layerWidth, areaHeight / layerHeight);
  const automaticLayout = vipInfoAutoLayout(layerWidth, layerHeight, layerBounds, handleLift);
  const scale = fitScale * draft.zoom * vipInfoProductScale(handleLift) * automaticLayout.scale;
  const drawWidth = layerWidth * scale;
  const drawHeight = layerHeight * scale;
  let drawX = areaX + (areaWidth - drawWidth) / 2 + draft.offset_x * areaWidth + automaticLayout.shiftX;
  let drawY = areaY + (areaHeight - drawHeight) / 2 + draft.offset_y * areaHeight
    - vipInfoProductLiftY(handleLift) * areaHeight
    + automaticLayout.dropY * areaHeight;
  return {
    left: drawX + layerBounds.left * scale,
    top: drawY + layerBounds.top * scale,
    right: drawX + layerBounds.right * scale,
    bottom: drawY + layerBounds.bottom * scale
  };
}

function liveInfoProductBody(
  sourceUrl: string,
  image: HTMLImageElement,
  draft: ImageAdjustment,
  preparedLayer?: HTMLCanvasElement,
  preparedBody?: PixelBounds
): PixelBounds {
  const hasManualCrop = draft.crop_x > 0.0001
    || draft.crop_y > 0.0001
    || draft.crop_width < 0.9999
    || draft.crop_height < 0.9999;
  const productLayer = preparedLayer || livePreviewProductCutout(sourceUrl, image);
  const croppedProductLayer = !preparedLayer && hasManualCrop ? liveJdProductLayer(sourceUrl, image, draft) : null;
  const drawSource = croppedProductLayer?.canvas || productLayer;
  const drawSourceWidth = drawSource.width;
  const drawSourceHeight = drawSource.height;
  const layerBounds = preparedBody || croppedProductLayer?.body || liveInfoMeasurementBounds(drawSource);
  if (layerBounds.right <= layerBounds.left || layerBounds.bottom <= layerBounds.top) {
    return positionedInfoProductBody(drawSourceWidth, drawSourceHeight, {
      left: 0,
      top: 0,
      right: drawSourceWidth,
      bottom: drawSourceHeight
    }, draft, liveHandleVisualLift(drawSource));
  }
  return positionedInfoProductBody(
    drawSourceWidth,
    drawSourceHeight,
    layerBounds,
    draft,
    liveHandleVisualLift(drawSource)
  );
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
  const lengthMm = positiveDimensionValue(productInfo.product_length) || 200;
  const providedHeight = positiveDimensionValue(productInfo.product_height);
  const heightMm = providedHeight !== null
    ? providedHeight
    : Math.max(60, lengthMm * bodyHeight / bodyWidth);
  const profile = jdProductShapeProfile(bodyWidth, bodyHeight, lengthMm / Math.max(1, heightMm));
  const preferredBodyWidth = output.width * profile.preferredWidth * Math.max(0.82, Math.min(1.08, lengthMm / 205));
  const safe = { left: output.width * 0.04, top: output.height * 0.04, right: output.width * 0.96, bottom: output.height * 0.96 };
  const objectGap = Math.max(16, output.width * 0.025);
  const phoneDefaultShiftX = output.height > output.width ? 8 : 12;
  const productRulerGap = Math.max(28, output.width * 0.045);
  const productLeftAllowance = productRulerGap + Math.max(24, output.width * 0.03);
  const phoneRulerGap = Math.max(22, output.width * 0.035);
  const phoneLabelClearance = Math.max(40, output.width * 0.05);
  const groupLeftBound = safe.left + productLeftAllowance;
  const groupRightBound = safe.right - phoneRulerGap - phoneLabelClearance;
  const groupAvailableWidth = Math.max(1, groupRightBound - groupLeftBound);
  let baseScale = Math.min(
    output.width * profile.maxWidth / bodyWidth,
    output.height * profile.maxHeight / bodyHeight,
    output.width * 0.46 / layer.canvas.width,
    output.height * 0.60 / layer.canvas.height,
    preferredBodyWidth / bodyWidth
  );
  for (let pass = 0; pass < 2; pass += 1) {
    const fittedPhoneHeight = Math.max(
      output.height * 0.095,
      Math.min(output.height * 0.46, bodyHeight * baseScale * (163 / heightMm))
    );
    const groupWidth = bodyWidth * baseScale + objectGap + phoneDefaultShiftX
      + fittedPhoneHeight * JD_PHONE_ASPECT_RATIO;
    baseScale *= Math.min(1, groupAvailableWidth / Math.max(1, groupWidth));
  }
  const scale = baseScale * draft.zoom;
  const width = layer.canvas.width * scale;
  const height = layer.canvas.height * scale;
  const scaledBody = {
    left: layer.body.left * scale,
    top: layer.body.top * scale,
    right: layer.body.right * scale,
    bottom: layer.body.bottom * scale
  };
  const basePhoneHeight = Math.max(
    output.height * 0.095,
    Math.min(output.height * 0.46, bodyHeight * baseScale * (163 / heightMm))
  );
  const baseGroupWidth = bodyWidth * baseScale + objectGap + basePhoneHeight * JD_PHONE_ASPECT_RATIO;
  const centeredGroupLeft = groupLeftBound + Math.max(0, groupAvailableWidth - baseGroupWidth) / 2;
  const baseGroupLeft = Math.max(
    groupLeftBound,
    Math.min(centeredGroupLeft, groupRightBound - baseGroupWidth - phoneDefaultShiftX)
  );
  const desiredBodyCenterX = baseGroupLeft + bodyWidth * baseScale / 2 + draft.offset_x * output.width * 0.18;
  let x = desiredBodyCenterX - (scaledBody.left + scaledBody.right) / 2;
  let y = output.height * (output.height > output.width ? 0.70 : 0.73) + draft.offset_y * output.height * 0.18 - scaledBody.bottom;
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
        && (layer.handleLift ?? liveHandleVisualLift(layer.canvas)) >= 0.55;
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
    automaticBodyCenterX: baseGroupLeft + bodyWidth * baseScale / 2,
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
  const baselineShiftX = (baseGeometry.body.left + baseGeometry.body.right) / 2 - baseGeometry.automaticBodyCenterX;
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
  const basePhoneHeight = Math.max(
    output.height * 0.095,
    Math.min(output.height * 0.46, geometry.baseBodyHeight * (163 / geometry.heightMm))
  );
  const phoneHeightForScale = (scale: number) => basePhoneHeight * scale;
  const phoneWidthForHeight = (height: number) => phoneReference?.naturalWidth && phoneReference.naturalHeight
    ? height * phoneReference.naturalWidth / phoneReference.naturalHeight
    : height * 0.78;
  const basePhoneWidth = phoneWidthForHeight(basePhoneHeight);
  const objectGap = Math.max(16, output.width * 0.025);
  const minimumPhoneLeft = baseGeometry.body.right + objectGap;
  const maximumPhoneLeft = geometry.safe.right - basePhoneWidth - phoneRightAllowance;
  const availableExtraGap = Math.max(0, maximumPhoneLeft - minimumPhoneLeft);
  const portraitOutput = output.height > output.width;
  const minimumExtraGap = portraitOutput ? 8 : 12;
  const maximumExtraGap = portraitOutput ? 28 : 48;
  const spareRoomShare = portraitOutput ? 0.4 : 0.55;
  const preferredExtraGap = Math.max(
    minimumExtraGap,
    Math.min(maximumExtraGap, availableExtraGap * spareRoomShare)
  );
  const adaptiveExtraGap = Math.min(availableExtraGap, preferredExtraGap);
  let basePhoneLeft = minimumPhoneLeft + adaptiveExtraGap;
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
  const shortestSide = Math.min(output.width, output.height);
  const cap = Math.max(8, Math.round(shortestSide * 0.014));
  context.save();
  context.strokeStyle = JD_MEASURE_COLOR;
  context.fillStyle = JD_MEASURE_COLOR;
  context.lineWidth = Math.max(2, Math.round(shortestSide / 400));
  context.font = jdMeasureFont(output);
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
    const textMetrics = context.measureText(label);
    const textHeight = Math.max(
      1,
      textMetrics.actualBoundingBoxAscent + textMetrics.actualBoundingBoxDescent
    );
    const textX = labelOnRight
      ? start.x + cap + 9
      : start.x - cap - textHeight - 16;
    const textTop = (start.y + end.y) / 2 - 30;
    context.save();
    // Pillow pastes its 90° text layer at (textX, midpoint - 30). Draw the
    // browser text into that same rotated bounding box so live/exact previews
    // keep identical cap, label-side and baseline geometry.
    context.translate(textX, textTop + textMetrics.width);
    context.rotate(-Math.PI / 2);
    context.textAlign = "left";
    context.textBaseline = "top";
    context.fillText(label, 0, 0);
    context.restore();
  } else {
    context.textBaseline = "top";
    context.fillText(label, (start.x + end.x) / 2, start.y + cap + 7);
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
  productInfo: Record<string, string>,
  preparedLayer?: LiveProductLayer
) {
  const layer = preparedLayer || liveJdProductLayer(sourceUrl, image, draft);
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
  const lengthLabel = dimensionMmLabel(productInfo.product_length, "200mm");
  const heightLabel = dimensionMmLabel(productInfo.product_height, `${Math.round(geometry.heightMm)}mm`);
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
  const phoneLabelBox = draft.phone_label_linked === false ? phoneLayout.basePhone : phone;
  context.save();
  context.fillStyle = JD_MEASURE_COLOR;
  context.font = jdPhoneLabelFont(output, phoneLabelBox.height, draft.phone_label_scale || 1);
  context.textAlign = "center";
  context.textBaseline = "top";
  context.fillText(
    "iPhone 17 Pro Max",
    phoneLabelBox.left + phoneLabelBox.width / 2 + (draft.phone_label_offset_x || 0) * output.width * 0.18,
    phoneLabelBox.top + phoneLabelBox.height + jdPhoneLabelGap(output, phoneLabelBox.height)
      + (draft.phone_label_offset_y || 0) * output.height * 0.18
  );
  context.restore();
}

function LiveSlotPreview({ sourceUrl, sourceImageId, compositePrimaryUrl, compositePrimaryImageId, templateUrl, slot, draft, platform, sourceIndex, targetFolder, productInfo, logoColor, onLayerInfoChange }: {
  sourceUrl: string;
  sourceImageId: number;
  compositePrimaryUrl?: string;
  compositePrimaryImageId?: number;
  templateUrl?: string;
  slot: Slot;
  draft: ImageAdjustment;
  platform: OrganizerPlatform;
  sourceIndex: number;
  targetFolder: PreviewFolder;
  productInfo: Record<string, string>;
  logoColor: LogoColor;
  onLayerInfoChange?: (info: OrganizerLayerInfo | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [layerInfo, setLayerInfo] = useState<OrganizerLayerInfo | null>(null);
  const [primaryLayerInfo, setPrimaryLayerInfo] = useState<OrganizerLayerInfo | null>(null);
  const cropKey = `${draft.crop_x.toFixed(6)}:${draft.crop_y.toFixed(6)}:${draft.crop_width.toFixed(6)}:${draft.crop_height.toFixed(6)}`;

  useEffect(() => {
    if (!slotUsesOrganizerLayer(slot, platform)) {
      setLayerInfo(null);
      onLayerInfoChange?.(null);
      return;
    }
    setLayerInfo(null);
    onLayerInfoChange?.(null);
    const controller = new AbortController();
    void api.getVipOrganizerLayerInfo(sourceImageId, {
      crop_x: draft.crop_x,
      crop_y: draft.crop_y,
      crop_width: draft.crop_width,
      crop_height: draft.crop_height
    }, controller.signal).then(async (info) => {
      if (controller.signal.aborted) return;
      setLayerInfo(info);
      // Keep the editor locked until the exact prepared layer itself has
      // decoded. Publishing geometry as soon as the JSON arrived allowed a
      // zoom click to race the browser cutout -> prepared-cutout switch.
      await waitForLivePreviewImage(livePreviewImage(info.url), controller.signal);
      if (controller.signal.aborted) return;
      onLayerInfoChange?.(info);
    }).catch((requestError: any) => {
      if (requestError?.name !== "AbortError") {
        setLayerInfo(null);
        onLayerInfoChange?.(null);
      }
    });
    return () => controller.abort();
  }, [sourceImageId, cropKey, platform, slot.file_name, onLayerInfoChange]);

  useEffect(() => {
    if (platform !== "vip" || slot.file_name !== "606.jpg" || !compositePrimaryImageId) {
      setPrimaryLayerInfo(null);
      return;
    }
    setPrimaryLayerInfo(null);
    const controller = new AbortController();
    void api.getVipOrganizerLayerInfo(compositePrimaryImageId, {
      crop_x: 0,
      crop_y: 0,
      crop_width: 1,
      crop_height: 1
    }, controller.signal).then(setPrimaryLayerInfo).catch((requestError: any) => {
      if (requestError?.name !== "AbortError") setPrimaryLayerInfo(null);
    });
    return () => controller.abort();
  }, [compositePrimaryImageId, platform, slot.file_name]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let disposed = false;
    const context = canvas.getContext("2d");
    if (!context) return;
    const output = slotCanvasSize(slot.size, platform, targetFolder);
    canvas.width = output.width;
    canvas.height = output.height;
    const image = livePreviewImage(sourceUrl);
    const compositePrimary = platform === "vip" && slot.file_name === "606.jpg"
      ? livePreviewImage(compositePrimaryUrl || sourceUrl)
      : null;
    const phoneReference = platform === "jd" && slot.file_name === "5.jpg"
      ? livePreviewImage("/organizer-assets/iphone_reference.png")
      : null;
    const logoReference = platform === "jd" && /^[1-5]\.jpg$/.test(slot.file_name)
      ? livePreviewImage(`/organizer-assets/elle_logo_${logoColor}.png`)
      : null;
    const preparedUrl = layerInfo?.url || "";
    const preparedProduct = preparedUrl ? livePreviewImage(preparedUrl) : null;
    const draw = () => {
      if (!image.complete || !image.naturalWidth) return;
      if (compositePrimary && (!compositePrimary.complete || !compositePrimary.naturalWidth)) return;
      context.clearRect(0, 0, output.width, output.height);
      context.fillStyle = slot.file_name === "5.jpg" && platform === "jd" ? "#f3f3f3" : "#fff";
      context.fillRect(0, 0, output.width, output.height);
      if (platform === "vip" && slot.file_name === "401.jpg") {
        drawVipInfoStaticPreview(context, productInfo);
        if (!layerInfo || !preparedProduct?.complete || !preparedProduct.naturalWidth) {
          drawAdjustmentGuide(context, output, slot, platform, sourceIndex, targetFolder);
          return;
        }
      }
      if (platform === "vip" && ["604.jpg", "605.jpg"].includes(slot.file_name)) {
        context.fillStyle = "#fff";
        context.fillRect(0, output.height * 0.18, output.width, output.height * 0.82);
      }
      if (platform === "jd" && slot.file_name === "5.jpg") {
        if (!layerInfo || !preparedProduct?.complete || !preparedProduct.naturalWidth) {
          drawAdjustmentGuide(context, output, slot, platform, sourceIndex, targetFolder);
          return;
        }
        drawJdComparisonPreview(
          context,
          output,
          image,
          sourceUrl,
          phoneReference,
          logoReference,
          draft,
          productInfo,
          preparedOrganizerProductLayer(layerInfo, preparedProduct)
        );
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
      if (usesProductCutout && preparedProduct && (!preparedProduct.complete || !preparedProduct.naturalWidth)) return;
      // The backend applies crop, zoom and offsets to the complete source image
      // for model, detail and tag slots. Cropping the browser preview to its
      // detected non-white content changes the coordinate basis and makes the
      // layer jump when the exact preview arrives. Product slots use their
      // prepared cutout below; every other slot must retain full-image bounds.
      const bounds = {
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
      const productLayer = usesProductCutout
        ? preparedProduct
          ? preparedProductCutout(preparedUrl, preparedProduct)
          : livePreviewProductCutout(sourceUrl, image)
        : null;
      // The backend removes whitespace again after a manual product crop. Use
      // the same cropped, tightly bounded layer in the live preview so crop,
      // zoom and drag do not jump when the exact preview is generated.
      const croppedProductLayer = usesProductCutout && hasManualCrop && !preparedProduct
        ? liveJdProductLayer(sourceUrl, image, draft)
        : null;
      const drawSource = croppedProductLayer?.canvas || productLayer || image;
      const drawSourceX = usesProductCutout ? 0 : sourceX;
      const drawSourceY = usesProductCutout ? 0 : sourceY;
      const drawSourceWidth = usesProductCutout ? drawSource.width : sourceWidth;
      const drawSourceHeight = usesProductCutout ? drawSource.height : sourceHeight;
      const fitScale = area.mode === "cover" && !hasManualCrop && !usesAutomaticDetailCutout
        ? Math.max(areaWidth / drawSourceWidth, areaHeight / drawSourceHeight)
        : Math.min(areaWidth / drawSourceWidth, areaHeight / drawSourceHeight);
      const automaticDetailScale = usesAutomaticDetailCutout
        ? automaticInteriorDetail ? 0.9 : 0.82
        : 1;
      const detailRatio = drawSourceWidth / Math.max(1, drawSourceHeight);
      const vipDetailOffset = detailRatio <= 0.78
        ? -0.105
        : detailRatio <= 1.05 ? -0.11 : detailRatio <= 1.45 ? -0.12 : -0.13;
      const infoHandleLift = platform === "vip" && slot.file_name === "401.jpg"
        ? layerInfo?.handle_lift ?? (productLayer ? liveHandleVisualLift(productLayer) : 0)
        : 0;
      const infoLayoutBody = platform === "vip" && slot.file_name === "401.jpg" && productLayer
        ? organizerLayerBounds(layerInfo?.product_body_bbox) || liveInfoMeasurementBounds(productLayer)
        : null;
      const infoAutomaticLayout = infoLayoutBody
        ? vipInfoAutoLayout(drawSourceWidth, drawSourceHeight, infoLayoutBody, infoHandleLift)
        : { scale: 1, shiftX: 0, dropY: 0 };
      const infoProductScale = platform === "vip" && slot.file_name === "401.jpg"
        ? vipInfoProductScale(infoHandleLift) * infoAutomaticLayout.scale
        : 1;
      const infoProductLiftY = platform === "vip" && slot.file_name === "401.jpg"
        ? vipInfoProductLiftY(infoHandleLift)
        : 0;
      const autoHandleLayout = slotUsesAutoHandleLayout(slot, platform);
      const automaticHandleLift = autoHandleLayout && productLayer && !hasManualCrop
        ? layerInfo?.handle_lift ?? liveHandleVisualLift(productLayer)
        : 0;
      const drawWidth = drawSourceWidth * fitScale * draft.zoom * automaticDetailScale * infoProductScale;
      const drawHeight = drawSourceHeight * fitScale * draft.zoom * automaticDetailScale * infoProductScale;
      const productBody = autoHandleLayout && productLayer && !hasManualCrop
        ? organizerLayerBounds(
          platform === "vip" && slot.file_name === "401.jpg"
            ? layerInfo?.product_body_bbox
            : layerInfo?.measurement_bbox
        ) || liveInfoMeasurementBounds(productLayer)
        : null;
      const productScale = fitScale * draft.zoom * automaticDetailScale * infoProductScale;
      const bodyCenterOffsetX = productBody && productLayer
        ? (productLayer.width / 2 - (productBody.left + productBody.right) / 2) * productScale
        : 0;
      const bodyCenterOffsetY = productBody && productLayer
        ? (productLayer.height / 2 - (productBody.top + productBody.bottom) / 2) * productScale
        : 0;
      let drawX = areaX + (areaWidth - drawWidth) / 2 + draft.offset_x * areaWidth
        + bodyCenterOffsetX
        + infoAutomaticLayout.shiftX;
      const multiAngleHandleLift = compositePrimary
        ? primaryLayerInfo?.handle_lift
          ?? liveHandleVisualLift(livePreviewProductCutout(compositePrimaryUrl || sourceUrl, compositePrimary))
        : 0;
      const multiAngleRowShift = multiAngleHandleLift >= 0.55
        ? (sourceIndex < 2 ? 1 : -1) * Math.round(13 * multiAngleHandleLift * output.height / 750)
        : 0;
      const tallHandleDropAware = autoHandleLayout;
      const productAutoLift = autoHandleLayout ? 0.03 : 0;
      const tallHandleDropRatio = platform === "vip" && slot.file_name === "2.jpg" ? 0.14 : 0.12;
      const automaticVerticalShift = multiAngleRowShift
        - infoProductLiftY * areaHeight
        + infoAutomaticLayout.dropY * areaHeight
        + (usesAutomaticDetailCutout && !automaticInteriorDetail ? (vipDetailOffset + 0.02) * areaHeight : 0)
        - (!hasManualCrop ? productAutoLift * areaHeight : 0)
        + (tallHandleDropAware && productLayer && !hasManualCrop
          ? tallHandleDropRatio * automaticHandleLift * areaHeight
          : 0);
      let drawY = areaY + (areaHeight - drawHeight) / 2 + draft.offset_y * areaHeight
        + bodyCenterOffsetY
        + automaticVerticalShift;
      const editorArea = slotEditorSafeAreaLayout(slot, platform, sourceIndex, targetFolder);
      // Match Pillow's integer safe-area edges so the live and exact layers
      // do not differ by a one-pixel fringe when a product touches a border.
      const safeLeft = Math.round(editorArea.x * output.width);
      const safeTop = Math.round(editorArea.y * output.height);
      const safeRight = Math.round((editorArea.x + editorArea.width) * output.width);
      const safeBottom = Math.round((editorArea.y + editorArea.height) * output.height);
      const usesStableAutoHandleAnchor = autoHandleLayout && Boolean(productLayer && productBody) && !hasManualCrop;
      if (usesStableAutoHandleAnchor && productLayer && productBody) {
        const baseProductScale = fitScale * automaticDetailScale * infoProductScale;
        const baseDrawWidth = drawSourceWidth * baseProductScale;
        const baseDrawHeight = drawSourceHeight * baseProductScale;
        const bodyCenterX = (productBody.left + productBody.right) / 2;
        const bodyCenterY = (productBody.top + productBody.bottom) / 2;
        const baseline = autoHandleBaselineOrigin(
          platform,
          slot.file_name,
          output,
          {
            x: areaX + (areaWidth - baseDrawWidth) / 2
              + (productLayer.width / 2 - bodyCenterX) * baseProductScale
              + infoAutomaticLayout.shiftX,
            y: areaY + (areaHeight - baseDrawHeight) / 2
              + (productLayer.height / 2 - bodyCenterY) * baseProductScale
              + automaticVerticalShift
          },
          { width: baseDrawWidth, height: baseDrawHeight },
          { left: safeLeft, top: safeTop, right: safeRight, bottom: safeBottom }
        );
        const bodyAnchorX = baseline.x + bodyCenterX * baseProductScale;
        const bodyAnchorY = baseline.y + productBody.bottom * baseProductScale;
        drawX = bodyAnchorX - bodyCenterX * productScale + draft.offset_x * areaWidth;
        drawY = bodyAnchorY - productBody.bottom * productScale + draft.offset_y * areaHeight;
      }
      // Keep automatic placement constrained, but do not re-clamp a layer
      // after the designer explicitly moves, crops or zooms it. Re-clamping
      // made dragging asymmetric and caused the zoom anchor to jump from one
      // edge to the other. This applies equally to VIP and JD manual edits.
      const allowFreeMovement = hasManualLayout;
      if (!allowFreeMovement && !usesStableAutoHandleAnchor) {
        drawX = clampLayerOrigin(drawX, drawWidth, safeLeft, safeRight);
      }
      if (!allowFreeMovement && !usesStableAutoHandleAnchor) {
        drawY = clampLayerOrigin(drawY, drawHeight, safeTop, safeBottom);
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
        const lineColor = VIP_INFO_RULER_COLOR;
        const storedBaseBody = storedProductRulerBase(draft);
        const baseBody = storedBaseBody || liveInfoProductBody(
          sourceUrl,
          image,
          draft,
          productLayer || undefined,
          organizerLayerBounds(layerInfo?.product_body_bbox) || undefined
        );
        const ruler = infoRulerGeometry(baseBody, draft.product_ruler_gap_scale || 1);
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
          { x: ruler.verticalX, y: ruler.top + VIP_INFO_HEIGHT_RULER_SHIFT_Y },
          { x: ruler.verticalX, y: ruler.bottom + VIP_INFO_HEIGHT_RULER_SHIFT_Y },
          productRulerCenter,
          draft,
          draft.height_ruler_scale || 1,
          draft.height_ruler_offset_x || 0,
          draft.height_ruler_offset_y || 0,
          output
        );
        const lengthLabel = dimensionMmLabel(productInfo.product_length);
        const thicknessLabel = dimensionMmLabel(productThickness(productInfo));
        const heightLabel = dimensionMmLabel(productInfo.product_height);
        context.save();
        context.strokeStyle = lineColor;
        context.fillStyle = "#555";
        context.lineWidth = 2 * Math.min(scaleX, scaleY);
        context.font = `400 ${Math.max(12, Math.round(19 * Math.min(scaleX, scaleY)))}px ${ORGANIZER_CANVAS_FONT}`;
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
        context.fillText(lengthLabel, (lengthRuler.start.x + lengthRuler.end.x) / 2, lengthRuler.start.y + 36 * scaleY);
        context.font = `400 ${Math.max(12, Math.round(18 * Math.min(scaleX, scaleY)))}px ${ORGANIZER_CANVAS_FONT}`;
        context.save();
        context.translate(heightRuler.start.x - 31 * scaleX, (heightRuler.start.y + heightRuler.end.y) / 2 - 7 * scaleY);
        context.rotate(-Math.PI / 2);
        context.fillText(heightLabel, 0, 0);
        context.restore();
        context.save();
        context.translate(widthRuler.text.x * scaleX, widthRuler.text.y * scaleY);
        context.rotate(-26 * Math.PI / 180);
        context.textBaseline = "middle";
        context.fillText(thicknessLabel, 0, 0);
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
    if (phoneReference) phoneReference.addEventListener("load", draw);
    if (logoReference) logoReference.addEventListener("load", draw);
    if (preparedProduct) preparedProduct.addEventListener("load", draw);
    draw();
    void ensureOrganizerCanvasFonts().then(() => {
      if (!disposed) draw();
    });
    return () => {
      disposed = true;
      image.removeEventListener("load", draw);
      if (compositePrimary) compositePrimary.removeEventListener("load", draw);
      if (phoneReference) phoneReference.removeEventListener("load", draw);
      if (logoReference) logoReference.removeEventListener("load", draw);
      if (preparedProduct) preparedProduct.removeEventListener("load", draw);
    };
  }, [
    draft,
    platform,
    slot.file_name,
    slot.size,
    sourceIndex,
    sourceUrl,
    sourceImageId,
    compositePrimaryUrl,
    compositePrimaryImageId,
    layerInfo,
    primaryLayerInfo,
    targetFolder,
    templateUrl,
    logoColor,
    productInfo.product_length,
    productInfo.product_thickness,
    productInfo.product_width,
    productInfo.product_height,
    productInfo.main_material,
    productInfo.lining_material,
    productInfo.wearing_method
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
    renderVersion: ORGANIZER_RENDER_STATE_VERSION,
    platform,
    targetFolder,
    slot,
    productInfo: slot.file_name === "401.jpg"
      ? productInfo
      : platform === "jd" && slot.file_name === "5.jpg" ? jdSizeInfo : undefined
  });
}

function jdComparisonDimensionsReady(productInfo: Record<string, string>) {
  return positiveDimensionValue(productInfo.product_length) !== null
    && positiveDimensionValue(productInfo.product_height) !== null;
}

function vipInfoDimensionsReady(productInfo: Record<string, string>) {
  return jdComparisonDimensionsReady(productInfo)
    && positiveDimensionValue(productThickness(productInfo)) !== null;
}

function UploadSection({ title, hint, items, multiple = true, disabled = false, deleteDisabled = false, onUpload, onDelete, onPreview }: {
  title: string;
  hint: string;
  items: UploadItem[];
  multiple?: boolean;
  disabled?: boolean;
  deleteDisabled?: boolean;
  onUpload: (files: FileList | File[] | null, skipped?: number) => void;
  onDelete: (item: UploadItem) => void;
  onPreview: (url: string) => void;
}) {
  const [dragging, setDragging] = useState(false);

  function acceptFiles(files: FileList | File[]) {
    const incoming = Array.from(files);
    const supported = incoming.filter((file) => SUPPORTED_IMAGE_NAME.test(file.name));
    const selected = multiple ? supported : supported.slice(0, 1);
    if (!selected.length) return;
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
            disabled={disabled || deleteDisabled}
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
  sourceImageId,
  sourceUrl,
  compositePrimaryUrl,
  compositePrimaryImageId,
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
  sourceImageId: number;
  sourceUrl: string;
  compositePrimaryUrl?: string;
  compositePrimaryImageId?: number;
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
  const isInfoPage = platform === "vip" && slot.file_name === "401.jpg";
  const storedInitial = normalizeAdjustment(slot.adjustments?.[sourceIndex]);
  // A saved 401 ruler baseline can intentionally differ from the product body
  // after the product was moved independently. Preserve it when reopening the
  // editor; clearing it here made the rulers snap back to the product.
  const initial = storedInitial;
  // Regenerate the exact 401 preview while keeping its saved adjustment state.
  const usableInitialPreview = isInfoPage ? "" : (initialPreview || "");
  const supportsLogoColor = platform === "jd" && /^[1-5]\.jpg$/.test(slot.file_name);
  const [draft, setDraft] = useState<ImageAdjustment>(initial);
  const [editorLayerInfo, setEditorLayerInfo] = useState<OrganizerLayerInfo | null>(null);
  const [logoColor, setLogoColor] = useState<LogoColor>(slot.logo_color === "white" ? "white" : "black");
  const [renderedPreview, setRenderedPreview] = useState(usableInitialPreview);
  const [loadedExactPreview, setLoadedExactPreview] = useState("");
  const [previewSynced, setPreviewSynced] = useState(Boolean(usableInitialPreview));
  const [holdExactPreview, setHoldExactPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);
  const [cropMode, setCropMode] = useState(false);
  const isPhoneComparison = platform === "jd" && slot.file_name === "5.jpg";
  const requiresPreparedGeometry = isInfoPage
    || isPhoneComparison
    || slotUsesAutoHandleLayout(slot, platform);
  const supportsJdFolderSync = platform === "jd" && previewFoldersForSlot(slot, platform).length > 1;
  const [syncJdFolders, setSyncJdFolders] = useState(false);
  const isPhoneObjectEditor = isPhoneComparison && initialMoveTarget === "phone";
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
  const renderedPreviewRef = useRef(usableInitialPreview);
  const draftVersionRef = useRef(0);
  const syncedVersionRef = useRef(usableInitialPreview ? 0 : -1);
  const previewRequestRef = useRef(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const activePreviewGenerationRef = useRef<number | null>(null);
  const previewTimerRef = useRef<number | null>(null);
  const moveTargetRef = useRef<AdjustmentTarget>("product");
  const linkedProductRulersRef = useRef(false);
  const defaultPhoneLinkAppliedRef = useRef(false);
  const editorGeometryReady = !requiresPreparedGeometry || Boolean(editorLayerInfo);

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
    if (!editorLayerInfo) return current;
    const currentlyLinked = current.phone_show_ruler !== false;
    if (currentlyLinked === linked) return current;
    const productImage = livePreviewImage(sourceUrl);
    const phoneReference = livePreviewImage("/organizer-assets/iphone_reference.png");
    if (!productImage.complete || !productImage.naturalWidth) {
      return { ...current, phone_show_ruler: linked };
    }

    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const layer = organizerProductGeometryLayer(editorLayerInfo);
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
      phone_ruler_scale: Math.max(
        JD_DECORATION_SCALE_MIN,
        Math.min(JD_DECORATION_SCALE_MAX, desiredLength / nextBaseLength)
      ),
      phone_ruler_offset_x: Math.max(
        -JD_DECORATION_OFFSET_MAX,
        Math.min(JD_DECORATION_OFFSET_MAX, (desiredCenter.x - nextBaseCenter.x) / (output.width * 0.18))
      ),
      phone_ruler_offset_y: Math.max(
        -JD_DECORATION_OFFSET_MAX,
        Math.min(JD_DECORATION_OFFSET_MAX, (desiredCenter.y - nextBaseCenter.y) / (output.height * 0.18))
      )
    };
  }

  function withPhoneLabelLinkPreservingPosition(
    current: ImageAdjustment,
    linked: boolean
  ): ImageAdjustment {
    if (!editorLayerInfo) return current;
    const currentlyLinked = current.phone_label_linked !== false;
    if (currentlyLinked === linked) return current;
    const productImage = livePreviewImage(sourceUrl);
    const phoneReference = livePreviewImage("/organizer-assets/iphone_reference.png");
    if (!productImage.complete || !productImage.naturalWidth) {
      return { ...current, phone_label_linked: linked };
    }

    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const layer = organizerProductGeometryLayer(editorLayerInfo);
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
    const anchor = (useLinkedPhone: boolean) => useLinkedPhone
      ? phoneLayout.phone
      : phoneLayout.basePhone;
    const previousBox = anchor(currentlyLinked);
    const nextBox = anchor(linked);
    const desiredX = previousBox.left + previousBox.width / 2
      + (current.phone_label_offset_x || 0) * output.width * 0.18;
    const desiredY = previousBox.top + previousBox.height
      + jdPhoneLabelGap(output, previousBox.height)
      + (current.phone_label_offset_y || 0) * output.height * 0.18;
    const desiredFontSize = jdPhoneLabelFontSize(
      output,
      previousBox.height,
      current.phone_label_scale || 1
    );
    const nextBaseFontSize = jdPhoneLabelFontSize(output, nextBox.height, 1);
    const nextBaseX = nextBox.left + nextBox.width / 2;
    const nextBaseY = nextBox.top + nextBox.height + jdPhoneLabelGap(output, nextBox.height);
    return {
      ...current,
      phone_label_linked: linked,
      phone_label_scale: Math.max(
        JD_DECORATION_SCALE_MIN,
        Math.min(JD_DECORATION_SCALE_MAX, desiredFontSize / Math.max(1, nextBaseFontSize))
      ),
      phone_label_offset_x: Math.max(
        -JD_DECORATION_OFFSET_MAX,
        Math.min(JD_DECORATION_OFFSET_MAX, (desiredX - nextBaseX) / (output.width * 0.18))
      ),
      phone_label_offset_y: Math.max(
        -JD_DECORATION_OFFSET_MAX,
        Math.min(JD_DECORATION_OFFSET_MAX, (desiredY - nextBaseY) / (output.height * 0.18))
      )
    };
  }

  useEffect(() => {
    if (!isPhoneObjectEditor || defaultPhoneLinkAppliedRef.current) return;
    const productImage = livePreviewImage(sourceUrl);
    const phoneReference = livePreviewImage("/organizer-assets/iphone_reference.png");
    const applyDefaultPhoneLink = () => {
      if (defaultPhoneLinkAppliedRef.current) return;
      // JD 5 uses the prepared organizer layer for its exact geometry. Do not
      // establish the initial "全部" link against the browser cutout before
      // that layer arrives, otherwise the first exact frame can move the
      // ruler/label by a few pixels.
      if (!editorLayerInfo) return;
      if (!productImage.complete || !productImage.naturalWidth) return;
      if (!phoneReference.complete || !phoneReference.naturalWidth) return;
      defaultPhoneLinkAppliedRef.current = true;
      const current = draftRef.current;
      let next = withPhoneRulerLinkPreservingPosition(current, true);
      next = withPhoneLabelLinkPreservingPosition(next, true);
      if (next !== current) applyDraft(next, false, false);
    };
    productImage.addEventListener("load", applyDefaultPhoneLink);
    phoneReference.addEventListener("load", applyDefaultPhoneLink);
    applyDefaultPhoneLink();
    return () => {
      productImage.removeEventListener("load", applyDefaultPhoneLink);
      phoneReference.removeEventListener("load", applyDefaultPhoneLink);
    };
  }, [editorLayerInfo, isPhoneObjectEditor, sourceUrl]);

  function productRulerBodyForDraft(nextDraft: ImageAdjustment): PixelBounds | null {
    if (!isInfoPage && !isPhoneComparison) return null;
    if (isInfoPage) {
      const body = organizerLayerBounds(editorLayerInfo?.product_body_bbox);
      if (body && editorLayerInfo) {
        return positionedInfoProductBody(
          editorLayerInfo.width,
          editorLayerInfo.height,
          body,
          nextDraft,
          editorLayerInfo.handle_lift
        );
      }
      return null;
    }
    if (!editorLayerInfo) return null;
    const output = slotCanvasSize(slot.size, platform, targetFolder);
    const layer = organizerProductGeometryLayer(editorLayerInfo);
    if (isPhoneComparison) {
      const { geometry } = jdComparisonProductGeometry(output, layer, nextDraft, productInfo);
      return geometry.body;
    }
    return null;
  }

  function withInfoRulerBaseline(current: ImageAdjustment): ImageAdjustment {
    if (storedProductRulerBase(current)) return current;
    const body = productRulerBodyForDraft(current);
    return body ? {
      ...current,
      product_ruler_base_left: body.left,
      product_ruler_base_top: body.top,
      product_ruler_base_right: body.right,
      product_ruler_base_bottom: body.bottom,
      product_ruler_group_scale: current.product_ruler_group_scale || 1,
      product_ruler_group_offset_x: current.product_ruler_group_offset_x || 0,
      product_ruler_group_offset_y: current.product_ruler_group_offset_y || 0
    } : current;
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
          ...(isInfoPage ? {
            product_ruler_gap_scale: (current.product_ruler_gap_scale || 1)
              * nextDraft.zoom / Math.max(0.0001, current.zoom)
          } : {}),
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
      ...(isInfoPage ? { product_ruler_gap_scale: nextDraft.zoom } : {}),
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

  function supersedeActiveServerPreview() {
    if (activePreviewGenerationRef.current === null) return;
    activePreviewGenerationRef.current = null;
    void api.cancelVipOrganizerSlotPreview({
      session_id: sessionId,
      file_name: slot.file_name,
      platform,
      target_folder: targetFolder,
      preview_generation: nextOrganizerPreviewGeneration()
    }).catch(() => undefined);
  }

  function cancelStalePreview() {
    if (previewTimerRef.current !== null) {
      window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = null;
    }
    if (!previewAbortRef.current) return;
    supersedeActiveServerPreview();
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
    syncProductRulerBody = linkedProductRulersRef.current,
    requestExactPreview = true
  ) {
    if (!editorGeometryReady) return;
    const preparedDraft = syncProductRulerBody
      ? withSyncedProductRulerBody(nextDraft)
      : nextDraft;
    cancelStalePreview();
    setHoldExactPreview(false);
    draftVersionRef.current += 1;
    draftRef.current = preparedDraft;
    setDraft(preparedDraft);
    setPreviewSynced(false);
    if (requestExactPreview) {
      scheduleExactPreview(preparedDraft, draftVersionRef.current);
    }
  }

  function setProductRulerLinkMode(linked: boolean) {
    if (!editorGeometryReady) return;
    const previous = draftRef.current;
    const current = withInfoRulerBaseline(previous);
    if (current === previous && (current.product_show_ruler !== false) === linked) return;
    const next = { ...current, product_show_ruler: linked };
    draftRef.current = next;
    setDraft(next);
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
    if (!editorGeometryReady) return;
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
    version = draftVersionRef.current,
    retrySuperseded = true
  ): Promise<string | undefined> {
    if (previewTimerRef.current !== null) {
      window.clearTimeout(previewTimerRef.current);
      previewTimerRef.current = null;
    }
    const requestId = ++previewRequestRef.current;
    previewAbortRef.current?.abort();
    const controller = new AbortController();
    previewAbortRef.current = controller;
    const previewGeneration = nextOrganizerPreviewGeneration();
    activePreviewGenerationRef.current = previewGeneration;
    setBusy(true);
    setError("");
    try {
      const result = await api.previewVipOrganizerSlot({
        session_id: sessionId,
        slots: [slotWithDraft(nextDraft)],
        product_info: productInfo,
        file_name: slot.file_name,
        platform,
        target_folder: targetFolder,
        preview_generation: previewGeneration
      }, controller.signal);
      if (result?.superseded) {
        if (
          retrySuperseded
          && requestId === previewRequestRef.current
          && version === draftVersionRef.current
        ) {
          return refreshPreview(nextDraft, version, false);
        }
        return undefined;
      }
      await preloadExactPreview(result.preview_url, controller.signal);
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
        if (activePreviewGenerationRef.current === previewGeneration) {
          activePreviewGenerationRef.current = null;
        }
        setBusy(false);
      }
    }
  }

  useEffect(() => {
    if (!usableInitialPreview) void refreshPreview(initial, 0);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
      if (previewTimerRef.current !== null) window.clearTimeout(previewTimerRef.current);
      supersedeActiveServerPreview();
      previewAbortRef.current?.abort();
      if (moveFrameRef.current !== null) window.cancelAnimationFrame(moveFrameRef.current);
    };
  }, []);

  useEffect(() => {
    const stage = resultStageRef.current;
    if (!stage) return;
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (!editorGeometryReady) return;
      const delta = event.deltaY < 0 ? 0.02 : -0.02;
      const current = draftRef.current;
      const target = moveTargetRef.current;
      const limits = targetScaleLimits(target);
      const nextScale = Math.max(
        limits.minimum,
        Math.min(limits.maximum, Math.round((targetScale(current, target) + delta) * 100) / 100)
      );
      applyDraft(updateTargetScale(current, target, nextScale));
    };
    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [editorGeometryReady, isPhoneComparison]);

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
    const current = linkedProductRulersRef.current
      ? draftRef.current
      : withInfoRulerBaseline(draftRef.current);
    const nextDraft: ImageAdjustment = {
      ...current,
      crop_x: (selection.left - imageRect.left) / imageRect.width,
      crop_y: (selection.top - imageRect.top) / imageRect.height,
      crop_width: selection.width / imageRect.width,
      crop_height: selection.height / imageRect.height,
      zoom: 1,
      offset_x: 0,
      offset_y: 0
    };
    return linkedProductRulersRef.current ? {
      ...nextDraft,
      product_ruler_base_left: undefined,
      product_ruler_base_top: undefined,
      product_ruler_base_right: undefined,
      product_ruler_base_bottom: undefined,
      product_ruler_gap_scale: 1,
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
    // Linked rulers rebuild from the prepared post-crop product body; detached
    // rulers keep their saved baseline exactly where the designer left it.
    applyDraft(cropAdjustment(selection, imageRect), false);
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
      applyDraft(cropAdjustment(selection, imageRect), false);
    });
  }

  function changeZoom(delta: number) {
    const current = draftRef.current;
    const limits = targetScaleLimits(activeMoveTarget);
    const nextScale = Math.max(
      limits.minimum,
      Math.min(limits.maximum, Math.round((targetScale(current, activeMoveTarget) + delta) * 100) / 100)
    );
    applyDraft(updateTargetScale(current, activeMoveTarget, nextScale));
  }

  function reset() {
    if (!isPhoneComparison) {
      logoColorRef.current = "black";
      setLogoColor("black");
    }
    const resetProduct = (current: ImageAdjustment) => ({
        ...current,
        zoom: DEFAULT_ADJUSTMENT.zoom,
        offset_x: DEFAULT_ADJUSTMENT.offset_x,
        offset_y: DEFAULT_ADJUSTMENT.offset_y,
        crop_x: DEFAULT_ADJUSTMENT.crop_x,
        crop_y: DEFAULT_ADJUSTMENT.crop_y,
        crop_width: DEFAULT_ADJUSTMENT.crop_width,
        crop_height: DEFAULT_ADJUSTMENT.crop_height
    });
    const clearProductRulerBaseline = (current: ImageAdjustment) => ({
        ...current,
        product_ruler_base_left: undefined,
        product_ruler_base_top: undefined,
        product_ruler_base_right: undefined,
        product_ruler_base_bottom: undefined
    });
    const resetProductRulers = (current: ImageAdjustment) => ({
        ...current,
        product_ruler_group_scale: 1,
        product_ruler_group_offset_x: 0,
        product_ruler_group_offset_y: 0,
        product_ruler_gap_scale: 1,
        length_ruler_scale: 1,
        length_ruler_offset_x: 0,
        length_ruler_offset_y: 0,
        height_ruler_scale: 1,
        height_ruler_offset_x: 0,
        height_ruler_offset_y: 0,
        width_ruler_scale: 1,
        width_ruler_offset_x: 0,
        width_ruler_offset_y: 0
    });
    let next = draftRef.current;

    if (isInfoPage) {
      if (infoMoveTarget === "product_rulers") {
        next = resetProductRulers({
          ...clearProductRulerBaseline(resetProduct(next)),
          product_show_ruler: true
        });
      } else if (infoMoveTarget === "product") {
        next = {
          ...resetProduct(withInfoRulerBaseline(next)),
          product_show_ruler: false
        };
      } else {
        next = withTargetScale(next, activeMoveTarget, targetScale(DEFAULT_ADJUSTMENT, activeMoveTarget));
        next = withTargetOffset(next, activeMoveTarget, 0, 0);
      }
      applyDraft(next, false);
    } else if (isPhoneComparison) {
      if (activeMoveTarget === "product") {
        const productState = linkedProductRulersRef.current ? next : withInfoRulerBaseline(next);
        next = {
          ...resetProduct(productState),
          product_show_ruler: next.product_show_ruler
        };
        if (linkedProductRulersRef.current) {
          next = resetProductRulers(clearProductRulerBaseline(next));
        }
      } else if (activeMoveTarget === "phone") {
        const phoneRulerLinked = next.phone_show_ruler !== false;
        const phoneLabelLinked = next.phone_label_linked !== false;
        next = {
          ...next,
          phone_scale: DEFAULT_ADJUSTMENT.phone_scale,
          phone_offset_x: DEFAULT_ADJUSTMENT.phone_offset_x,
          phone_offset_y: DEFAULT_ADJUSTMENT.phone_offset_y,
          phone_alignment: "bottom",
          ...(phoneRulerLinked ? {
            phone_ruler_scale: DEFAULT_ADJUSTMENT.phone_ruler_scale,
            phone_ruler_offset_x: DEFAULT_ADJUSTMENT.phone_ruler_offset_x,
            phone_ruler_offset_y: DEFAULT_ADJUSTMENT.phone_ruler_offset_y
          } : {}),
          ...(phoneLabelLinked ? {
            phone_label_scale: DEFAULT_ADJUSTMENT.phone_label_scale,
            phone_label_offset_x: DEFAULT_ADJUSTMENT.phone_label_offset_x,
            phone_label_offset_y: DEFAULT_ADJUSTMENT.phone_label_offset_y
          } : {})
        };
      } else {
        next = withTargetScale(next, activeMoveTarget, targetScale(DEFAULT_ADJUSTMENT, activeMoveTarget));
        next = withTargetOffset(next, activeMoveTarget, 0, 0);
      }
      applyDraft(next, false);
    } else if (activeMoveTarget !== "product") {
      next = withTargetScale(next, activeMoveTarget, targetScale(DEFAULT_ADJUSTMENT, activeMoveTarget));
      next = withTargetOffset(next, activeMoveTarget, 0, 0);
      applyDraft(next, false);
    } else {
      applyDraft({ ...DEFAULT_ADJUSTMENT });
    }
    cropSelectionRef.current = null;
    setCropSelection(null);
    setCropMode(false);
  }

  function flushPendingMove(requestExactPreview = false) {
    if (moveFrameRef.current !== null) {
      window.cancelAnimationFrame(moveFrameRef.current);
      moveFrameRef.current = null;
    }
    const nextDraft = pendingMoveRef.current;
    pendingMoveRef.current = null;
    if (nextDraft) applyDraft(nextDraft, linkedProductRulersRef.current, requestExactPreview);
  }

  function finishMove() {
    if (!editorGeometryReady) return;
    flushPendingMove(false);
    moveStartRef.current = null;
    scheduleExactPreview(draftRef.current, draftVersionRef.current);
  }

  function saveAdjustment() {
    if (!editorGeometryReady) {
      setError("正在读取商品精确几何，请稍后再保存");
      return;
    }
    // Saving the adjustment must not wait behind an obsolete exact render.
    // The parent preview effect regenerates the latest slot after the editor
    // closes; a fully synced URL can still be reused without another request.
    flushPendingMove(false);
    const currentDraft = draftRef.current;
    const previewUrl = syncedVersionRef.current === draftVersionRef.current
      ? renderedPreviewRef.current
      : undefined;
    cancelStalePreview();
    onSave(currentDraft, logoColorRef.current, previewUrl, syncJdFolders);
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
            <span>{slot.file_name === "606.jpg" ? `正在调整来源 ${sourceIndex + 1}` : isPhoneComparison ? `正在调整${moveTarget === "phone" ? (draft.phone_show_ruler !== false ? "手机和高标线" : "手机") : moveTarget === "phone_label" ? "iPhone文字" : moveTarget === "phone_ruler" ? "高标线" : moveTarget === "length_ruler" ? "商品长标线" : moveTarget === "height_ruler" ? "商品高标线" : "商品图"}` : isInfoPage ? `正在调整${infoMoveTarget === "width_ruler" ? "厚标线" : infoMoveTarget === "length_ruler" ? "长标线" : infoMoveTarget === "height_ruler" ? "高标线" : infoMoveTarget === "product" ? "商品图" : "全部"}` : "当前输出位置独立调整"}</span>
          </div>
          <button type="button" className="icon-button" onClick={requestClose} title="关闭"><X size={21} /></button>
        </header>

        {showCloseConfirm && <div className="slot-close-confirm" role="alertdialog" aria-label="未保存调整提示">
          <div><strong>调整尚未保存</strong><span>保存会生成最终预览并退出；右上角 × 会放弃本次调整</span></div>
          <div className="slot-save-actions">
            {supportsJdFolderSync && <label className="slot-sync-toggle">
              <input
                type="checkbox"
                checked={syncJdFolders}
                onChange={(event) => setSyncJdFolders(event.target.checked)}
              />
              <span>同步 800/750</span>
            </label>}
            <button type="button" className="primary" disabled={!editorGeometryReady} onClick={saveAdjustment}><Save size={17} />保存并退出</button>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="不保存并退出" title="不保存并退出"><X size={20} /></button>
        </div>}

        <div className="slot-adjustment-workspace">
          <div className="slot-adjustment-source">
            <div className="slot-adjustment-heading">
              <strong>{isPhoneObjectEditor ? "手机参照图" : "原始图片"}</strong>
              <span>{isPhoneObjectEditor ? "在右侧预览中调整手机、iPhone文字或高标线" : cropMode ? "拖动框选保留区域" : "点击“裁剪”后框选区域"}</span>
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
              className={`slot-result-stage${busy || !editorGeometryReady ? " is-loading" : ""}`}
              onPointerDown={(event) => {
                if (!editorGeometryReady) return;
                cancelStalePreview();
                setHoldExactPreview(false);
                setPreviewSynced(false);
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
                const dragSensitivity = start.target === "product" && slot.kind === "model"
                  ? 0.6
                  : platform === "jd" && slot.file_name === "2.jpg" && start.target === "product"
                    ? 0.65
                    : 1;
                const canvasDeltaX = (event.clientX - start.x) * output.width / Math.max(1, bounds.width) * dragSensitivity;
                const canvasDeltaY = (event.clientY - start.y) * output.height / Math.max(1, bounds.height) * dragSensitivity;
                const rawOffsetX = start.offsetX + canvasDeltaX / basis.x;
                const rawOffsetY = start.offsetY + canvasDeltaY / basis.y;
                const offsetLimit = targetOffsetLimit(start.target);
                const nextOffsetX = Math.max(-offsetLimit, Math.min(offsetLimit,
                  slot.kind === "model"
                    ? modelDragOffsetWithBoundaryResistance(start.offsetX, rawOffsetX - start.offsetX)
                    : rawOffsetX
                ));
                const nextOffsetY = Math.max(-offsetLimit, Math.min(offsetLimit,
                  slot.kind === "model"
                    ? modelDragOffsetWithBoundaryResistance(start.offsetY, rawOffsetY - start.offsetY)
                    : rawOffsetY
                ));
                pendingMoveRef.current = updateTargetOffset(draftRef.current, start.target, nextOffsetX, nextOffsetY);
                if (moveFrameRef.current === null) {
                  moveFrameRef.current = window.requestAnimationFrame(() => {
                    moveFrameRef.current = null;
                    const nextDraft = pendingMoveRef.current;
                    pendingMoveRef.current = null;
                    if (nextDraft) applyDraft(nextDraft, linkedProductRulersRef.current, false);
                  });
                }
              }}
              onPointerUp={finishMove}
              onPointerCancel={finishMove}
            >
              <LiveSlotPreview
                sourceUrl={sourceUrl}
                sourceImageId={sourceImageId}
                compositePrimaryUrl={compositePrimaryUrl}
                compositePrimaryImageId={compositePrimaryImageId}
                templateUrl={renderedPreview || usableInitialPreview}
                slot={slot}
                draft={draft}
                platform={platform}
                sourceIndex={sourceIndex}
                targetFolder={targetFolder}
                productInfo={productInfo}
                logoColor={logoColor}
                onLayerInfoChange={setEditorLayerInfo}
              />
              {renderedPreview && <img
                key={renderedPreview}
                className={`slot-exact-preview${(previewSynced || holdExactPreview) && loadedExactPreview === renderedPreview ? " is-visible" : ""}`}
                src={renderedPreview}
                alt={`${slot.file_name} 精确成品预览`}
                draggable={false}
                onLoad={() => setLoadedExactPreview(renderedPreview)}
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
          {(isPhoneComparison || isInfoPage) && <div
            className="slot-phone-controls"
            role="group"
            aria-disabled={!editorGeometryReady}
            aria-label={isInfoPage ? "产品信息图调整" : "手机对比调整"}
            onClickCapture={(event) => {
              if (editorGeometryReady) return;
              event.preventDefault();
              event.stopPropagation();
            }}
          >
            <span>调整对象</span>
            {isPhoneObjectEditor ? <>
              <button type="button" className={moveTarget === "phone" && draft.phone_show_ruler !== false && draft.phone_label_linked !== false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone");
                setCropMode(false);
                if (draftRef.current.phone_show_ruler === false || draftRef.current.phone_label_linked === false) {
                  let next = withPhoneRulerLinkPreservingPosition(draftRef.current, true);
                  next = withPhoneLabelLinkPreservingPosition(next, true);
                  applyDraft(next);
                }
              }}>全部</button>
              <button type="button" className={moveTarget === "phone" && draft.phone_show_ruler === false && draft.phone_label_linked === false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone");
                setCropMode(false);
                if (draftRef.current.phone_show_ruler !== false || draftRef.current.phone_label_linked !== false) {
                  let next = withPhoneRulerLinkPreservingPosition(draftRef.current, false);
                  next = withPhoneLabelLinkPreservingPosition(next, false);
                  applyDraft(next);
                }
              }}>手机</button>
              <button type="button" className={moveTarget === "phone_label" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("phone_label");
                setCropMode(false);
                if (draftRef.current.phone_label_linked !== false) {
                  applyDraft(withPhoneLabelLinkPreservingPosition(draftRef.current, false));
                }
              }}>iPhone文字</button>
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
              <button type="button" className={infoMoveTarget === "product_rulers" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("product_rulers");
                linkedProductRulersRef.current = true;
                // Keep the independently positioned ruler baseline intact.
                // Subsequent linked moves transform it by the same product
                // delta instead of snapping it back onto the product body.
                setProductRulerLinkMode(true);
              }}>全部</button>
              <button type="button" className={infoMoveTarget === "product" ? "active-tool" : ""} onClick={() => {
                setInfoMoveTarget("product");
                linkedProductRulersRef.current = false;
                setProductRulerLinkMode(false);
              }}>商品图</button>
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
              }}>厚标线</button>
            </> : <>
              <button type="button" className={moveTarget === "product" && draft.product_show_ruler !== false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("product");
                linkedProductRulersRef.current = true;
                applyDraft({ ...draftRef.current, product_show_ruler: true }, true);
              }}>全部</button>
              <button type="button" className={moveTarget === "product" && draft.product_show_ruler === false ? "active-tool" : ""} onClick={() => {
                setMoveTarget("product");
                linkedProductRulersRef.current = false;
                if (draftRef.current.product_show_ruler !== false) {
                  applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
                }
              }}>商品图</button>
              <button type="button" className={moveTarget === "length_ruler" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("length_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
                if (draftRef.current.product_show_ruler !== false) applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
              }}>长标线</button>
              <button type="button" className={moveTarget === "height_ruler" ? "active-tool" : ""} onClick={() => {
                setMoveTarget("height_ruler");
                linkedProductRulersRef.current = false;
                setCropMode(false);
                if (draftRef.current.product_show_ruler !== false) applyDraft({ ...draftRef.current, product_show_ruler: false }, true);
              }}>高标线</button>
            </>}
          </div>}
          {activeMoveTarget === "product" && <button type="button" disabled={!editorGeometryReady} className={cropMode ? "active-tool" : ""} onClick={toggleCropMode}><Crop size={18} />裁剪</button>}
          <button type="button" disabled={!editorGeometryReady} onClick={() => changeZoom(-0.05)}><ZoomOut size={18} />缩小</button>
          <span className="slot-zoom-value">{Math.round(targetScale(draft, activeMoveTarget) * 100)}%</span>
          <button type="button" disabled={!editorGeometryReady} onClick={() => changeZoom(0.05)}><ZoomIn size={18} />放大</button>
          {supportsLogoColor && <div className="slot-logo-color" role="group" aria-label="左上角 Logo 颜色">
            <span>Logo</span>
            <button type="button" disabled={!editorGeometryReady} className={logoColor === "black" ? "active-tool" : ""} onClick={() => changeLogoColor("black")}>
              <i className="logo-color-swatch black" />黑色
            </button>
            <button type="button" disabled={!editorGeometryReady} className={logoColor === "white" ? "active-tool" : ""} onClick={() => changeLogoColor("white")}>
              <i className="logo-color-swatch white" />白色
            </button>
          </div>}
          <button type="button" disabled={!editorGeometryReady} onClick={reset}><RotateCcw size={18} />恢复自动</button>
          <span className="slot-drag-hint"><Move size={16} />位置 {
            Math.round(targetOffset(draft, activeMoveTarget).x * 100)
          } / {
            Math.round(targetOffset(draft, activeMoveTarget).y * 100)
          }</span>
          {!previewSynced && <span className="slot-preview-pending">保存后会在后台更新精确预览</span>}
          <div className="slot-save-actions">
            {supportsJdFolderSync && <label
              className="slot-sync-toggle"
              title={syncJdFolders ? "本次调整会同时保存到 800 和 750" : `本次调整只保存到 ${targetFolder}`}
            >
              <input
                type="checkbox"
                checked={syncJdFolders}
                onChange={(event) => setSyncJdFolders(event.target.checked)}
              />
              <span>同步 800/750</span>
            </label>}
            <button type="button" className="primary" disabled={!editorGeometryReady} onClick={saveAdjustment}><Save size={18} />{!editorGeometryReady ? "正在读取精确几何" : "保存并退出"}</button>
          </div>
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

function ZoomableImagePreview({ url, onClose }: { url: string; onClose: () => void }) {
  const [zoom, setZoom] = useState(1);
  const [baseSize, setBaseSize] = useState<{ width: number; height: number } | null>(null);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  function clampZoom(value: number) {
    return Math.min(IMAGE_PREVIEW_ZOOM_MAX, Math.max(IMAGE_PREVIEW_ZOOM_MIN, value));
  }

  function resetPreview() {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }

  function adjustZoom(delta: number) {
    setZoom((current) => {
      const next = clampZoom(Number((current + delta).toFixed(2)));
      if (next === current) return current;
      const ratio = next / current;
      setPan((currentPan) => ({ x: currentPan.x * ratio, y: currentPan.y * ratio }));
      return next;
    });
  }

  function zoomAtPointer(event: ReactWheelEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const anchorX = event.clientX - (rect.left + rect.width / 2);
    const anchorY = event.clientY - (rect.top + rect.height / 2);
    const delta = event.deltaY < 0 ? IMAGE_PREVIEW_ZOOM_STEP : -IMAGE_PREVIEW_ZOOM_STEP;
    setZoom((current) => {
      const next = clampZoom(Number((current + delta).toFixed(2)));
      if (next === current) return current;
      const ratio = next / current;
      setPan((currentPan) => ({
        x: anchorX - (anchorX - currentPan.x) * ratio,
        y: anchorY - (anchorY - currentPan.y) * ratio
      }));
      return next;
    });
  }

  function startDrag(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: pan.x,
      originY: pan.y
    };
    setDragging(true);
  }

  function movePreview(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.preventDefault();
    setPan({
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY
    });
  }

  function finishDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragRef.current = null;
    setDragging(false);
  }

  function measureImage(image: HTMLImageElement) {
    const availableWidth = Math.min(window.innerWidth * 0.92, 1600);
    const availableHeight = Math.max(240, window.innerHeight - 160);
    const fitScale = Math.min(
      availableWidth / Math.max(1, image.naturalWidth),
      availableHeight / Math.max(1, image.naturalHeight),
      1
    );
    setBaseSize({
      width: Math.max(1, Math.round(image.naturalWidth * fitScale)),
      height: Math.max(1, Math.round(image.naturalHeight * fitScale))
    });
  }

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "+" || event.key === "=") adjustZoom(IMAGE_PREVIEW_ZOOM_STEP);
      if (event.key === "-") adjustZoom(-IMAGE_PREVIEW_ZOOM_STEP);
      if (event.key === "0") resetPreview();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return <div className="image-modal image-modal-zoomable" role="dialog" aria-modal="true" aria-label="放大图片预览" onClick={onClose}>
    <button className="image-modal-close" type="button" onClick={onClose} aria-label="关闭预览"><X size={22} /></button>
    <div
      className={`image-modal-viewport${dragging ? " is-dragging" : ""}`}
      onClick={(event) => event.stopPropagation()}
      onWheel={zoomAtPointer}
      onPointerDown={startDrag}
      onPointerMove={movePreview}
      onPointerUp={finishDrag}
      onPointerCancel={finishDrag}
      onDoubleClick={resetPreview}
      title="滚轮缩放，按住左键拖动查看，双击复位"
    >
      <div className="image-modal-canvas">
        <img
          src={url}
          alt="图片预览"
          draggable={false}
          onLoad={(event) => measureImage(event.currentTarget)}
          style={baseSize ? {
            width: `${baseSize.width}px`,
            height: `${baseSize.height}px`,
            transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})`
          } : undefined}
        />
      </div>
    </div>
    <div className="image-modal-zoom-controls" role="group" aria-label="图片缩放" onClick={(event) => event.stopPropagation()}>
      <button type="button" disabled={zoom <= IMAGE_PREVIEW_ZOOM_MIN} onClick={() => adjustZoom(-IMAGE_PREVIEW_ZOOM_STEP)} aria-label="缩小图片"><ZoomOut size={20} /></button>
      <output aria-live="polite">{Math.round(zoom * 100)}%</output>
      <button type="button" disabled={zoom >= IMAGE_PREVIEW_ZOOM_MAX} onClick={() => adjustZoom(IMAGE_PREVIEW_ZOOM_STEP)} aria-label="放大图片"><ZoomIn size={20} /></button>
      <button type="button" onClick={resetPreview} aria-label="恢复原始缩放" title="恢复 100%"><RotateCcw size={18} /></button>
    </div>
  </div>;
}

export default function VipOrganizer({ active, initialProductFile, onInitialProductFileConsumed }: VipOrganizerProps) {
  const sessionStorageKey = "vip-organizer-session-id";
  const sessionSnapshotStorageKey = "vip-organizer-session-snapshot-v1";
  const [sessionId, setSessionId] = useState("");
  const sessionIdRef = useRef("");
  const sessionPromiseRef = useRef<Promise<{ session_id: string }> | null>(null);
  const pendingUploadsRef = useRef(0);
  const uploadingKindsRef = useRef<Set<"product" | "model" | "tag">>(new Set());
  const completedUploadKindsRef = useRef<Set<"product" | "model" | "tag">>(new Set());
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
  const [uploadingKinds, setUploadingKinds] = useState<Set<"product" | "model" | "tag">>(() => new Set());
  const uploadsBusy = uploadingKinds.size > 0;
  const uiBusy = busy || uploadsBusy;
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
  const analyzeGenerationRef = useRef(0);
  const analyzeAbortRef = useRef<AbortController | null>(null);
  const slotPreviewSignaturesRef = useRef<Record<string, string>>({});
  const platformWorkspaceRef = useRef<Partial<Record<OrganizerPlatform, {
    slots: Slot[];
    previews: Record<string, string>;
    signatures: Record<string, string>;
  }>>>({});
  // Keep source choices independently from disposable preview caches. A JD
  // preview invalidation (for example after dimensions change) must not erase
  // a designer-confirmed 5.jpg source before the background rebuild finishes.
  const platformSlotHistoryRef = useRef<Partial<Record<OrganizerPlatform, Slot[]>>>({});
  const jdBackgroundPreparedRef = useRef(false);
  const jdBackgroundGenerationRef = useRef(0);
  const jdBackgroundRetryCountRef = useRef(0);
  const jdBackgroundRetryTimerRef = useRef<number | null>(null);
  const [jdBackgroundRetryVersion, setJdBackgroundRetryVersion] = useState(0);
  const jdDimensionSignatureRef = useRef("");
  const reanalyzeTimerRef = useRef<number | null>(null);
  const assetRolesRef = useRef<Record<number, string>>({});
  const assetTagsRef = useRef<Record<number, string[]>>({});
  const [preview, setPreview] = useState<string | null>(null);
  const [info, setInfo] = useState({
    product_name: "ELLE箱包",
    product_length: "",
    product_height: "",
    product_thickness: "",
    main_material: "",
    lining_material: "",
    wearing_method: "",
    disclaimer: "包身长高厚测量均为最长部分\n误差在1-2cm之间因手工测量均属正常"
  });

  const allAssets = useMemo(() => [...(assets.product || []), ...(assets.model || []), ...(assets.tag || [])], [assets]);
  const jdBackgroundInputSignature = useMemo(() => JSON.stringify({
    products: products.map((item) => item.image_id),
    models: models.map((item) => item.image_id),
    tags: tags.map((item) => item.image_id),
    roles: assetRoles,
    assetTags,
    dimensions: [info.product_length.trim(), info.product_height.trim()]
  }), [products, models, tags, assetRoles, assetTags, info.product_length, info.product_height]);
  const hasOrganizerSlots = slots.length > 0;

  useEffect(() => {
    if (active) void api.prewarmHeavyTask("organizer").catch(() => undefined);
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const pasteTagScreenshot = (event: globalThis.ClipboardEvent) => {
      if (busy || uploadsBusy || !event.clipboardData) return;
      const images = Array.from(event.clipboardData.items).flatMap((item, index) => {
        if (item.kind !== "file" || !item.type.startsWith("image/")) return [];
        const blob = item.getAsFile();
        if (!blob) return [];
        const extension = blob.type === "image/jpeg" ? "jpg" : blob.type === "image/webp" ? "webp" : "png";
        return [new File([blob], `粘贴吊牌-${Date.now()}-${index + 1}.${extension}`, { type: blob.type })];
      });
      if (!images.length) return;
      event.preventDefault();
      void upload("tag", images.slice(0, 1));
    };
    window.addEventListener("paste", pasteTagScreenshot);
    return () => window.removeEventListener("paste", pasteTagScreenshot);
  }, [active, busy, uploadsBusy]);

  useEffect(() => {
    if (!initialProductFile) return;
    onInitialProductFileConsumed?.();
    void upload("product", [initialProductFile]);
  }, [initialProductFile]);

  useEffect(() => {
    slotsRef.current = slots;
  }, [slots]);

  function organizerProductInfo() {
    const dimensions = [info.product_length, info.product_height, info.product_thickness]
      .map((value) => value.trim())
      .filter(Boolean)
      .join(" × ");
    return { ...info, dimensions: dimensions ? `${dimensions} mm` : "" };
  }

  function invalidatePlatformWorkspaces() {
    // Rendered workspaces are disposable; platformSlotHistoryRef intentionally
    // survives so designer-confirmed source choices remain authoritative.
    platformWorkspaceRef.current = {};
    jdBackgroundGenerationRef.current += 1;
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
  }

  function saveSessionSnapshot() {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId) return;
    const snapshot = {
      version: ORGANIZER_SESSION_SNAPSHOT_VERSION,
      render_version: ORGANIZER_RENDER_STATE_VERSION,
      session_id: currentSessionId,
      products: productsRef.current,
      models: modelsRef.current,
      tags: tagsRef.current,
      slots: slotsRef.current,
      platform,
      assets,
      asset_roles: assetRolesRef.current,
      asset_tags: assetTagsRef.current,
      manual_asset_ids: Array.from(manualAssetIds),
      api_role_notes: apiRoleNotes,
      slot_previews: slotPreviews,
      slot_preview_signatures: slotPreviewSignaturesRef.current,
      platform_workspaces: platformWorkspaceRef.current,
      platform_slot_history: platformSlotHistoryRef.current,
      info
    };
    try {
      window.sessionStorage.setItem(sessionSnapshotStorageKey, JSON.stringify(snapshot, (_key, value) => {
        if (typeof File !== "undefined" && value instanceof File) return undefined;
        if (typeof Blob !== "undefined" && value instanceof Blob) return undefined;
        if (typeof value === "string" && (/^data:image\//i.test(value) || value.startsWith("blob:"))) return undefined;
        return value;
      }));
    } catch {
      // The ID remains resumable even if the browser refuses a large snapshot.
    }
  }

  function restoreSessionSnapshot(
    nextSessionId: string,
    resumedAssets: Record<string, UploadItem[]>
  ) {
    clearLivePreviewCaches();
    sessionIdRef.current = nextSessionId;
    window.sessionStorage.setItem(sessionStorageKey, nextSessionId);
    let snapshot: any = null;
    try {
      const raw = window.sessionStorage.getItem(sessionSnapshotStorageKey);
      const parsed = raw ? JSON.parse(raw) : null;
      if (
        parsed?.version === ORGANIZER_SESSION_SNAPSHOT_VERSION
        && parsed.render_version === ORGANIZER_RENDER_STATE_VERSION
        && parsed.session_id === nextSessionId
      ) snapshot = parsed;
    } catch {
      snapshot = null;
    }

    setSessionId(nextSessionId);
    analyzeAbortRef.current?.abort();
    analyzeAbortRef.current = null;
    analyzeGenerationRef.current += 1;
    setAdjustmentEditor(null);
    const snapshotMatchesServerAssets = snapshot && (["product", "model", "tag"] as const).every((kind) => {
      const snapshotItems = Array.isArray(snapshot[kind]) ? snapshot[kind] : [];
      const serverItems = Array.isArray(resumedAssets[kind]) ? resumedAssets[kind] : [];
      const snapshotIds = snapshotItems
        .map((item: any) => Number(item?.image_id))
        .filter((imageId: number) => Number.isInteger(imageId))
        .sort((left: number, right: number) => left - right);
      const serverIds = serverItems
        .map((item) => Number(item.image_id))
        .filter((imageId) => Number.isInteger(imageId))
        .sort((left, right) => left - right);
      return snapshotIds.length === serverIds.length
        && snapshotIds.every((imageId: number, index: number) => imageId === serverIds[index]);
    });
    if (snapshotMatchesServerAssets) {
      const restoredProducts = Array.isArray(snapshot.products) ? snapshot.products : [];
      const restoredModels = Array.isArray(snapshot.models) ? snapshot.models : [];
      const restoredTags = Array.isArray(snapshot.tags) ? snapshot.tags : [];
      const restoredSlots = Array.isArray(snapshot.slots) ? snapshot.slots : [];
      const restoredRoles = snapshot.asset_roles || {};
      const restoredTagsByAsset = snapshot.asset_tags || {};
      productsRef.current = restoredProducts;
      modelsRef.current = restoredModels;
      tagsRef.current = restoredTags;
      slotsRef.current = restoredSlots;
      assetRolesRef.current = restoredRoles;
      assetTagsRef.current = restoredTagsByAsset;
      setProducts(restoredProducts);
      setModels(restoredModels);
      setTags(restoredTags);
      setSlots(restoredSlots);
      setPlatform(snapshot.platform === "jd" ? "jd" : "vip");
      setAssets(snapshot.assets || { product: [], model: [], tag: [] });
      setAssetRoles(restoredRoles);
      setAssetTags(restoredTagsByAsset);
      setManualAssetIds(new Set(Array.isArray(snapshot.manual_asset_ids) ? snapshot.manual_asset_ids : []));
      setApiRoleNotes(snapshot.api_role_notes || {});
      setSlotPreviews(snapshot.slot_previews || {});
      slotPreviewSignaturesRef.current = snapshot.slot_preview_signatures || {};
      platformWorkspaceRef.current = snapshot.platform_workspaces || {};
      platformSlotHistoryRef.current = snapshot.platform_slot_history || {};
      if (snapshot.info && typeof snapshot.info === "object") setInfo(snapshot.info);
      // Revalidate every cached JD (folder, file) pair after restoration. A
      // workspace can exist even when a previous background pass was partial.
      jdBackgroundPreparedRef.current = false;
      jdBackgroundRetryCountRef.current = 0;
      jdBackgroundGenerationRef.current += 1;
      return;
    }

    // Upload/delete can complete immediately before a browser crash, leaving
    // sessionStorage one write behind the server. Never restore slots,
    // adjustments or preview URLs against a different authoritative asset
    // set; preserve only the text fields and rebuild from the server rows.
    if (snapshot?.info && typeof snapshot.info === "object") setInfo(snapshot.info);

    const restoredProducts = resumedAssets.product || [];
    const restoredModels = resumedAssets.model || [];
    const restoredTags = resumedAssets.tag || [];
    productsRef.current = restoredProducts;
    modelsRef.current = restoredModels;
    tagsRef.current = restoredTags;
    slotsRef.current = [];
    setProducts(restoredProducts);
    setModels(restoredModels);
    setTags(restoredTags);
    setSlots([]);
    setAssets(Object.fromEntries(Object.entries(resumedAssets).map(([kind, rows]) => [
      kind,
      rows.map((item) => ({ ...item, id: item.image_id }))
    ])));
    setAssetRoles({});
    setAssetTags({});
    setManualAssetIds(new Set());
    assetRolesRef.current = {};
    assetTagsRef.current = {};
    setApiRoleNotes({});
    setSlotPreviews({});
    setPreviewBusy(false);
    slotPreviewSignaturesRef.current = {};
    platformWorkspaceRef.current = {};
    platformSlotHistoryRef.current = {};
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
    jdBackgroundGenerationRef.current += 1;
    if (restoredProducts.length) {
      void analyze(undefined, "vip", undefined, {
        products: restoredProducts,
        models: restoredModels,
        tags: restoredTags
      }, undefined, true, true);
    }
  }

  useEffect(() => {
    const signature = JSON.stringify([info.product_length.trim(), info.product_height.trim()]);
    if (!jdDimensionSignatureRef.current) {
      jdDimensionSignatureRef.current = signature;
      return;
    }
    if (jdDimensionSignatureRef.current === signature) return;
    jdDimensionSignatureRef.current = signature;
    jdBackgroundGenerationRef.current += 1;
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
    const jdWorkspace = platformWorkspaceRef.current.jd;
    if (jdWorkspace) {
      const previews = { ...jdWorkspace.previews };
      const signatures = { ...jdWorkspace.signatures };
      (["800", "750"] as PreviewFolder[]).forEach((targetFolder) => {
        const key = slotPreviewKey("jd", "5.jpg", targetFolder);
        delete previews[key];
        delete signatures[key];
      });
      platformWorkspaceRef.current.jd = {
        ...jdWorkspace,
        previews,
        signatures
      };
    }
  }, [info.product_length, info.product_height]);

  useEffect(() => {
    if (platformSwitching || platformRegenerating) return;
    if (!sessionId || !slots.length) {
      previewAbortRef.current?.abort();
      setPreviewBusy(false);
      setSlotPreviews({});
      return;
    }
    const productInfo = organizerProductInfo();
    const jdSizeReady = jdComparisonDimensionsReady(productInfo);
    const vipInfoReady = vipInfoDimensionsReady(productInfo);
    const previewTargets = slots.flatMap((slot) => {
      if (!slot.image_ids[0] || (slot.file_name === "606.jpg" && slot.image_ids.length < 4)) return [];
      if (platform === "jd" && slot.file_name === "5.jpg" && !jdSizeReady) return [];
      if (platform === "vip" && slot.file_name === "401.jpg" && !vipInfoReady) return [];
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
        if (platform === "vip" && changedTargets.length > 5) {
          const firstScreenNames = new Set(["1.jpg", "2.jpg", "3.jpg", "4.jpg", "15.jpg"]);
          const firstScreenTargets = changedTargets.filter((target) => firstScreenNames.has(target.slot.file_name));
          const remainingTargets = changedTargets.filter((target) => !firstScreenNames.has(target.slot.file_name));
          const batches = [firstScreenTargets, remainingTargets].filter((batch) => batch.length > 0);

          for (const batch of batches) {
            if (requestId !== previewRequestRef.current || controller.signal.aborted) return;
            try {
              const targetFolder = batch[0].targetFolder;
              const batchSlots = batch.map((target) => target.slot);
              const result = await api.previewVipOrganizer({
                session_id: sessionId,
                slots: slotsForPreviewFolder(batchSlots, platform, targetFolder),
                preview_file_names: batch.map((target) => target.slot.file_name),
                product_info: productInfo,
                platform,
                target_folder: targetFolder
              }, controller.signal);
              if (requestId !== previewRequestRef.current || controller.signal.aborted) return;
              const expectedKeys = new Set(batch.map((target) => target.key));
              const successfulEntries = Object.entries(result.previews || {}).flatMap(([fileName, previewUrl]) => {
                const key = slotPreviewKey(platform, fileName, targetFolder);
                return typeof previewUrl === "string" && expectedKeys.has(key)
                  ? [[key, previewUrl] as const]
                  : [];
              });
              const successfulKeys = new Set(successfulEntries.map(([key]) => key));
              const previewEntries = Object.fromEntries(successfulEntries);
              setSlotPreviews((current) => ({ ...current, ...previewEntries }));
              const workspace = platformWorkspaceRef.current[platform];
              platformWorkspaceRef.current[platform] = {
                slots,
                previews: { ...(workspace?.previews || {}), ...previewEntries },
                signatures: {
                  ...(workspace?.signatures || {}),
                  ...Object.fromEntries([...successfulKeys].filter((key) => signatures[key]).map((key) => [key, signatures[key]]))
                }
              };
              slotPreviewSignaturesRef.current = {
                ...slotPreviewSignaturesRef.current,
                ...Object.fromEntries([...successfulKeys].filter((key) => signatures[key]).map((key) => [key, signatures[key]]))
              };
              const failedCount = batch.length - successfulEntries.length;
              if (failedCount) {
                partialPreviewFailure = true;
                setMessage(`${failedCount} 个预览暂未生成，其他预览已更新`);
              }
            } catch (error: any) {
              if (error?.name === "AbortError") return;
              partialPreviewFailure = true;
              setMessage(`${batch.length} 个预览暂未生成，正在继续更新其他预览`);
            }
          }
        } else if (changedTargets.length > 5) {
          const folders = [...new Set(changedTargets.map((target) => target.targetFolder))];
          const groupedResults = await Promise.allSettled(folders.map(async (targetFolder) => {
            const folderTargets = changedTargets.filter((target) => target.targetFolder === targetFolder);
            const result = await api.previewVipOrganizer({
              session_id: sessionId,
              slots: slotsForPreviewFolder(folderTargets.map((target) => target.slot), platform, targetFolder),
              preview_file_names: folderTargets.map((target) => target.slot.file_name),
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
              target_folder: target.targetFolder,
              preview_generation: nextOrganizerPreviewGeneration()
            }, controller.signal);
            return result?.superseded ? null : [target.key, result.preview_url] as const;
          }));
          if (requestId === previewRequestRef.current) {
            const successfulResults = results
              .filter((result): result is PromiseFulfilledResult<readonly [string, string]> => (
                result.status === "fulfilled"
                && result.value !== null
                && typeof result.value[1] === "string"
              ))
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
            const failedCount = results.filter((result) => result.status === "rejected").length;
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

  const vipForegroundPreviewsSynced = (() => {
    if (platform !== "vip" || !slots.length) return false;
    const productInfo = organizerProductInfo();
    const vipInfoReady = vipInfoDimensionsReady(productInfo);
    const vipInfoStarted = [
      productInfo.product_length,
      productInfo.product_height,
      productInfo.product_thickness
    ].some((value) => value.trim().length > 0);
    // Once the designer starts entering dimensions, keep the single image
    // worker reserved for VIP 401 until all three values are valid and its
    // foreground preview has finished. Otherwise JD 5 can begin during the
    // short pause between the height and thickness fields and block 401.
    if (vipInfoStarted && !vipInfoReady) return false;
    const targets = slots.flatMap((slot) => {
      if (!slot.image_ids[0] || (slot.file_name === "606.jpg" && slot.image_ids.length < 4)) return [];
      if (slot.file_name === "401.jpg" && !vipInfoReady) return [];
      return previewFoldersForSlot(slot, "vip").map((targetFolder) => ({
        slot,
        targetFolder,
        key: slotPreviewKey("vip", slot.file_name, targetFolder)
      }));
    });
    return targets.length > 0 && targets.every((target) => (
      Boolean(slotPreviews[target.key])
      && slotPreviewSignaturesRef.current[target.key] === slotPreviewSignature(
        slotForPreviewFolder(target.slot, "vip", target.targetFolder),
        productInfo,
        "vip",
        target.targetFolder
      )
    ));
  })();

  useEffect(() => {
    if (
      platform !== "vip"
      || platformSwitching
      || platformRegenerating
      || previewBusy
      || !vipForegroundPreviewsSynced
      || !sessionId
      || !hasOrganizerSlots
      || !productsRef.current.length
      || jdBackgroundPreparedRef.current
    ) return;
    jdBackgroundPreparedRef.current = true;
    const generation = ++jdBackgroundGenerationRef.current;
    const sessionAtStart = sessionId;
    const backgroundInputSignature = () => JSON.stringify({
      products: productsRef.current.map((item) => item.image_id),
      models: modelsRef.current.map((item) => item.image_id),
      tags: tagsRef.current.map((item) => item.image_id),
      roles: assetRolesRef.current,
      assetTags: assetTagsRef.current,
      dimensions: (() => {
        try {
          return JSON.parse(jdDimensionSignatureRef.current || '["",""]');
        } catch {
          return ["", ""];
        }
      })()
    });
    const inputsAtStart = jdBackgroundInputSignature;
    const isCurrentGeneration = () => generation === jdBackgroundGenerationRef.current
      && sessionIdRef.current === sessionAtStart
      && backgroundInputSignature() === inputsAtStart;
    const scheduleRetry = () => {
      if (!isCurrentGeneration() || jdBackgroundRetryCountRef.current >= 2) return;
      const retryNumber = jdBackgroundRetryCountRef.current + 1;
      jdBackgroundRetryCountRef.current = retryNumber;
      jdBackgroundPreparedRef.current = false;
      const retryDelay = retryNumber === 1 ? 1200 : 2400;
      jdBackgroundRetryTimerRef.current = window.setTimeout(() => {
        jdBackgroundRetryTimerRef.current = null;
        if (!isCurrentGeneration()) return;
        setJdBackgroundRetryVersion((current) => current + 1);
      }, retryDelay);
    };
    let backgroundTaskStarted = false;
    let backgroundTaskFinished = false;
    const timer = window.setTimeout(async () => {
      backgroundTaskStarted = true;
      try {
        const productInfo = organizerProductInfo();
        const existingWorkspace = platformWorkspaceRef.current.jd;
        const workspaceSlotsComplete = Boolean(existingWorkspace?.slots.length)
          && JD_SLOT_FILES.every((fileName) => existingWorkspace?.slots.some((slot) => slot.file_name === fileName));
        let mergedBackgroundSlots: Slot[];
        if (workspaceSlotsComplete) {
          mergedBackgroundSlots = existingWorkspace?.slots || [];
        } else {
          const result = await api.analyzeVipOrganizer({
            session_id: sessionId,
            product_image_ids: productsRef.current.map((item) => item.image_id),
            model_image_ids: modelsRef.current.map((item) => item.image_id),
            tag_image_ids: tagsRef.current.map((item) => item.image_id),
            asset_roles: assetRolesRef.current,
            asset_tags: assetTagsRef.current,
            platform: "jd"
          });
          if (!isCurrentGeneration()) return;
          mergedBackgroundSlots = mergeAnalyzedSlots(
            platformSlotHistoryRef.current.jd || [],
            result.slots as Slot[]
          );
        }
        const renderableTargets = mergedBackgroundSlots.flatMap((slot) => {
          if (!slot.image_ids[0]) return [];
          if (slot.file_name === "5.jpg" && !jdComparisonDimensionsReady(productInfo)) return [];
          return previewFoldersForSlot(slot, "jd").map((targetFolder) => {
            const folderSlot = slotForPreviewFolder(slot, "jd", targetFolder);
            return {
              slot,
              targetFolder,
              key: slotPreviewKey("jd", slot.file_name, targetFolder),
              signature: slotPreviewSignature(folderSlot, productInfo, "jd", targetFolder)
            };
          });
        });
        const missingTargets = renderableTargets.filter((target) => (
          !existingWorkspace?.previews[target.key]
          || existingWorkspace.signatures[target.key] !== target.signature
        ));
        const entries: (readonly [string, string])[] = [];
        for (const targetFolder of ["800", "750"] as PreviewFolder[]) {
          if (!isCurrentGeneration()) return;
          const folderTargets = missingTargets.filter((target) => target.targetFolder === targetFolder);
          if (!folderTargets.length) continue;
          try {
            const expectedKeys = new Set(folderTargets.map((target) => target.key));
            const result = await api.previewVipOrganizer({
              session_id: sessionId,
              slots: slotsForPreviewFolder(folderTargets.map((target) => target.slot), "jd", targetFolder),
              preview_file_names: folderTargets.map((target) => target.slot.file_name),
              product_info: productInfo,
              platform: "jd",
              target_folder: targetFolder
            });
            entries.push(...Object.entries(result.previews || {}).flatMap(([fileName, previewUrl]) => {
              const key = slotPreviewKey("jd", fileName, targetFolder);
              return typeof previewUrl === "string" && expectedKeys.has(key)
                ? [[key, previewUrl] as const]
                : [];
            }));
          } catch {
            // The bounded retry below fills only targets still missing.
          }
        }
        if (!isCurrentGeneration()) return;
        const successfulKeys = new Set(entries.map(([key]) => key));
        const signatures = Object.fromEntries(renderableTargets
          .filter((target) => successfulKeys.has(target.key))
          .map((target) => [target.key, target.signature]));
        platformWorkspaceRef.current.jd = {
          slots: mergedBackgroundSlots,
          previews: { ...(existingWorkspace?.previews || {}), ...Object.fromEntries(entries) },
          signatures: { ...(existingWorkspace?.signatures || {}), ...signatures }
        };
        platformSlotHistoryRef.current.jd = mergedBackgroundSlots;
        saveSessionSnapshot();
        const missingAfterRender = missingTargets.some((target) => !successfulKeys.has(target.key));
        if (missingAfterRender) scheduleRetry();
        else jdBackgroundRetryCountRef.current = 0;
      } catch {
        scheduleRetry();
      } finally {
        backgroundTaskFinished = true;
      }
    }, 800);
    return () => {
      window.clearTimeout(timer);
      if (generation === jdBackgroundGenerationRef.current && !backgroundTaskFinished) {
        if (backgroundTaskStarted) jdBackgroundGenerationRef.current += 1;
        jdBackgroundPreparedRef.current = false;
      }
      if (jdBackgroundRetryTimerRef.current !== null) {
        window.clearTimeout(jdBackgroundRetryTimerRef.current);
        jdBackgroundRetryTimerRef.current = null;
      }
    };
  }, [
    platform,
    platformSwitching,
    platformRegenerating,
    sessionId,
    hasOrganizerSlots,
    jdBackgroundInputSignature,
    jdBackgroundRetryVersion,
    previewBusy,
    vipForegroundPreviewsSynced
  ]);

  useEffect(() => {
    if (!sessionId) return;
    saveSessionSnapshot();
    const persistBeforePageHide = () => saveSessionSnapshot();
    window.addEventListener("pagehide", persistBeforePageHide);
    return () => window.removeEventListener("pagehide", persistBeforePageHide);
  }, [
    sessionId,
    products,
    models,
    tags,
    slots,
    platform,
    assets,
    assetRoles,
    assetTags,
    manualAssetIds,
    apiRoleNotes,
    slotPreviews,
    info
  ]);

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
    let resumed = false;
    const initialSession = previousSessionId
      ? api.resumeVipOrganizerSession(previousSessionId)
        .then((session) => {
          resumed = true;
          return session;
        })
        .catch(() => {
          window.sessionStorage.removeItem(sessionStorageKey);
          window.sessionStorage.removeItem(sessionSnapshotStorageKey);
          return api.startVipOrganizerSession();
        })
      : api.startVipOrganizerSession();
    sessionPromiseRef.current = initialSession;
    initialSession.then(
      (session) => {
        if (!active) return;
        if (resumed) {
          const resumedAssets = (session as { assets?: Record<string, UploadItem[]> }).assets;
          restoreSessionSnapshot(session.session_id, resumedAssets || { product: [], model: [], tag: [] });
        } else {
          applyNewSession(session.session_id);
        }
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

  function applyNewSession(nextSessionId: string) {
    clearLivePreviewCaches();
    window.sessionStorage.removeItem(sessionSnapshotStorageKey);
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
    setPreviewBusy(false);
    slotPreviewSignaturesRef.current = {};
    analyzeAbortRef.current?.abort();
    analyzeAbortRef.current = null;
    analyzeGenerationRef.current += 1;
    platformWorkspaceRef.current = {};
    platformSlotHistoryRef.current = {};
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
    jdBackgroundGenerationRef.current += 1;
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
      setMessage("已开始新一轮，上一轮自动化整理素材和 ZIP 已删除；AI 生成记录不受影响");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function upload(kind: "product" | "model" | "tag", files: FileList | File[] | null, preSkipped = 0) {
    const fileItems = Array.from(files || []);
    if (!fileItems.length) {
      if (preSkipped) setMessage(`已跳过 ${preSkipped} 个不支持或未导入的文件`);
      return;
    }
    const tagUploadConflicts = kind === "tag"
      ? uploadingKindsRef.current.size > 0
      : uploadingKindsRef.current.has("tag");
    if (busy || uploadingKindsRef.current.has(kind) || tagUploadConflicts) return;
    uploadingKindsRef.current = new Set(uploadingKindsRef.current).add(kind);
    setUploadingKinds(new Set(uploadingKindsRef.current));
    pendingUploadsRef.current += 1;
    setMessage(pendingUploadsRef.current > 1
      ? `正在同时上传商品图和模特图，请勿关闭页面……`
      : `正在一次性上传 ${fileItems.length} 张原图，请勿关闭页面……`);
    try {
      const currentSession = await ensureSession();
      const uploaded = await api.uploadVipOrganizerAssets(currentSession, kind, fileItems);
      invalidatePlatformWorkspaces();
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
        completedUploadKindsRef.current.add(kind);
        setMessage(pendingUploadsRef.current > 1
          ? "当前区域已上传，正在等待另一上传区域完成……"
          : "图片已上传，正在准备自动整理……");
      } else {
        setMessage(skipped ? `已上传 ${uploaded.length} 张图片，自动跳过 ${skipped} 个不支持、损坏或未导入的文件` : `已上传 ${uploaded.length} 张图片`);
      }
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      pendingUploadsRef.current -= 1;
      uploadingKindsRef.current = new Set(uploadingKindsRef.current);
      uploadingKindsRef.current.delete(kind);
      setUploadingKinds(new Set(uploadingKindsRef.current));
      if (pendingUploadsRef.current === 0) {
        const completedKinds = new Set(completedUploadKindsRef.current);
        completedUploadKindsRef.current.clear();
        if (completedKinds.size && productsRef.current.length > 0 && modelsRef.current.length > 0) {
          const tagOnlyRefresh = completedKinds.size === 1
            && completedKinds.has("tag")
            && slotsRef.current.length > 0;
          setMessage(tagOnlyRefresh
            ? "吊牌已上传，正在增量刷新吊牌相关输出……"
            : "商品图和模特图已到齐，正在自动整理初稿……");
          await analyze(
            undefined,
            platform,
            undefined,
            {
              products: productsRef.current,
              models: modelsRef.current,
              tags: tagsRef.current
            },
            tagOnlyRefresh ? "tag" : undefined
          );
        }
      }
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
      platformSlotHistoryRef.current = Object.fromEntries(
        Object.entries(platformSlotHistoryRef.current).map(([savedPlatform, savedSlots]) => [
          savedPlatform,
          (savedSlots || []).filter((slot) => !slot.image_ids.includes(item.image_id))
        ])
      ) as Partial<Record<OrganizerPlatform, Slot[]>>;
      platformSlotHistoryRef.current[platform] = nextSlotState;
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
      setPreviewBusy(false);
      clearLivePreviewCaches();
      setSlotPreviews({});
      slotPreviewSignaturesRef.current = {};
      platformWorkspaceRef.current = {};
      jdBackgroundPreparedRef.current = false;
      jdBackgroundRetryCountRef.current = 0;
      if (jdBackgroundRetryTimerRef.current !== null) {
        window.clearTimeout(jdBackgroundRetryTimerRef.current);
        jdBackgroundRetryTimerRef.current = null;
      }
      jdBackgroundGenerationRef.current += 1;

      if (!nextProducts.length) {
        slotsRef.current = [];
        setSlots([]);
        setAssets({ product: [], model: [], tag: [] });
        setMessage("商品原图已删除，请重新上传后再自动整理");
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
    invalidatePlatformWorkspaces();
    const generation = ++analyzeGenerationRef.current;
    analyzeAbortRef.current?.abort();
    const controller = new AbortController();
    analyzeAbortRef.current = controller;
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
      }, controller.signal);
      if (controller.signal.aborted || generation !== analyzeGenerationRef.current) return;
      const currentSlots = slotsRef.current;
      const previousPlatformSlots = platformOverride === platform
        ? currentSlots
        : platformSlotHistoryRef.current[platformOverride] || [];
      let nextSlots: Slot[];
      if (replaceSlots && platformOverride === platform) {
        nextSlots = result.slots as Slot[];
      } else {
        const merged = mergeAnalyzedSlots(previousPlatformSlots, result.slots as Slot[]);
        if (incrementalKind !== "tag") {
          nextSlots = merged;
        } else {
          const mergedByName = new Map(merged.map((slot) => [slot.file_name, slot]));
          nextSlots = previousPlatformSlots.map((slot) => slot.kind === "tag" ? mergedByName.get(slot.file_name) || slot : slot);
        }
      }
      const previousByName = new Map(previousPlatformSlots.map((slot) => [slot.file_name, slotPreviewSignature(slot, organizerProductInfo(), platformOverride)]));
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
      platformSlotHistoryRef.current[platformOverride] = nextSlots;
      setSlots(nextSlots);
      if (platformOverride === "vip") {
        delete platformWorkspaceRef.current.jd;
        jdBackgroundPreparedRef.current = false;
        jdBackgroundGenerationRef.current += 1;
      }
      setAssets(result.assets);
      setAdjustmentEditor(null);
      setMessage(incrementalKind === "tag" ? "吊牌相关输出已增量更新，其他预览保持不变" : "已生成自动整理初稿；黄色或红色可信度项目需要重点确认");
    } catch (error: any) {
      if (error?.name === "AbortError") return;
      if (generation === analyzeGenerationRef.current) setMessage(error.message);
    } finally {
      if (generation === analyzeGenerationRef.current) {
        if (analyzeAbortRef.current === controller) analyzeAbortRef.current = null;
        if (manageBusy) setBusy(false);
      }
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
    const generation = ++analyzeGenerationRef.current;
    analyzeAbortRef.current?.abort();
    const controller = new AbortController();
    analyzeAbortRef.current = controller;
    setBusy(true);
    setMessage("正在用所选图文分析 API 分析全部商品图，本次只调用一次……");
    try {
      const apiResult = await api.analyzeVipOrganizerWithApi({
        session_id: sessionId,
        product_image_ids: products.map((item) => item.image_id),
        api_config_id: analysisConfigId
      }, controller.signal);
      if (controller.signal.aborted || generation !== analyzeGenerationRef.current) return;
      invalidatePlatformWorkspaces();
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
      }, controller.signal);
      if (controller.signal.aborted || generation !== analyzeGenerationRef.current) return;
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
      platformSlotHistoryRef.current[platform] = nextSlots;
      setSlots(nextSlots);
      setAssets(result.assets);
      setAdjustmentEditor(null);
      setMessage("API 已完成一次素材分类，并按固定标签重新整理；请检查低可信度位置");
    } catch (error: any) {
      if (error?.name === "AbortError") return;
      if (generation === analyzeGenerationRef.current) setMessage(error.message);
    } finally {
      if (generation === analyzeGenerationRef.current) {
        if (analyzeAbortRef.current === controller) analyzeAbortRef.current = null;
        setBusy(false);
      }
    }
  }

  function updateAssetRole(imageId: number, role: string) {
    invalidatePlatformWorkspaces();
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
    setMessage("固定标签已修改，正在只更新受影响的输出位置");
  }

  function effectiveAssetTags(asset: any) {
    return assetTags[asset.id] ?? asset.suggested_tags ?? [];
  }

  function toggleAssetTag(asset: any, tag: string) {
    invalidatePlatformWorkspaces();
    const selected = assetTagsRef.current[asset.id] ?? asset.suggested_tags ?? [];
    const nextTags = selected.includes(tag) ? selected.filter((item: string) => item !== tag) : [...selected, tag];
    const next = { ...assetTagsRef.current, [asset.id]: nextTags };
    assetTagsRef.current = next;
    setAssetTags(next);
    setManualAssetIds((current) => new Set(current).add(asset.id));
    scheduleReanalyze(assetRolesRef.current, next);
    setMessage("细节标签已修改，正在只更新受影响的输出位置");
  }

  function resetAssetTags(imageId: number) {
    invalidatePlatformWorkspaces();
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
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    setPreviewBusy(false);
    setSlotPreviews((current) => {
      const next = { ...current };
      affectedNames.forEach((affectedFileName) => {
        const preview800 = slotPreviewKey(platform, affectedFileName, "800");
        const preview750 = slotPreviewKey(platform, affectedFileName, "750");
        // A source thumbnail is not a rendered template. Keeping it under the
        // exact-preview key made the editor mark an old/raw frame as synced.
        delete next[preview800];
        delete next[preview750];
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
      platformSlotHistoryRef.current[platform] = nextSlots;
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
    const baseAdjustments = (currentSlot.adjustments || [])
      .map((item) => normalizeAdjustment(item));
    const previousFolderAdjustments = currentSlot.folder_adjustments || {};
    const folderAdjustments = { ...previousFolderAdjustments };
    const baseLogoColor: LogoColor = currentSlot.logo_color === "white" ? "white" : "black";
    const folderLogoColors = { ...(currentSlot.folder_logo_colors || {}) };
    if (platform === "jd") {
      previewFoldersForSlot(currentSlot, platform).forEach((folder) => {
        if (!folderAdjustments[folder]) {
          folderAdjustments[folder] = baseAdjustments.map((item, index) => normalizeAdjustment(
            adjustmentForSyncedFolder(
              item,
              previousFolderAdjustments[folder]?.[index],
              currentSlot,
              platform,
              "800",
              folder
            )
          ));
        }
        if (!folderLogoColors[folder]) folderLogoColors[folder] = baseLogoColor;
      });
    }
    foldersToUpdate.forEach((folder) => {
      folderAdjustments[folder] = normalizedAdjustments.map((item, index) => normalizeAdjustment(
        adjustmentForSyncedFolder(
          item,
          previousFolderAdjustments[folder]?.[index]
            || (folder === "800" ? currentSlot.adjustments?.[index] : undefined),
          currentSlot,
          platform,
          targetFolder,
          folder
        )
      ));
      folderLogoColors[folder] = logoColor;
    });
    const syncedBaseAdjustments = syncJdFolders && platform === "jd"
      ? folderAdjustments["800"] || normalizedAdjustments
      : normalizedAdjustments;
    const updatedSlot: Slot = {
      ...currentSlot,
      adjustments: targetFolder === "800" || syncJdFolders
        ? syncedBaseAdjustments
        : currentSlot.adjustments,
      folder_adjustments: platform === "jd" ? folderAdjustments : currentSlot.folder_adjustments,
      folder_logo_colors: platform === "jd" ? folderLogoColors : currentSlot.folder_logo_colors,
      logo_color: platform !== "jd" || targetFolder === "800" || syncJdFolders
        ? logoColor
        : currentSlot.logo_color
    };
    setSlots((current) => {
      const nextSlots = current.map((slot) => slot.file_name === fileName ? updatedSlot : slot);
      slotsRef.current = nextSlots;
      platformSlotHistoryRef.current[platform] = nextSlots;
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
    if (platform === "vip" && slots.some((slot) => slot.file_name === "401.jpg") && !vipInfoDimensionsReady(productInfo)) {
      setMessage("请填写有效的商品长、高、厚，再下载唯品会套图");
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
      setMessage(result.missing.length ? `ZIP 已下载，共 ${result.generated_count} 张，缺少：${result.missing.join("、")}` : `${platformName}套图 ZIP 已开始下载`);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function changePlatform(nextPlatform: OrganizerPlatform) {
    if (nextPlatform === platform) return;
    jdBackgroundGenerationRef.current += 1;
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
    if (reanalyzeTimerRef.current !== null) {
      window.clearTimeout(reanalyzeTimerRef.current);
      reanalyzeTimerRef.current = null;
    }
    const scrollTop = window.scrollY;
    previewAbortRef.current?.abort();
    previewRequestRef.current += 1;
    setPreviewBusy(false);
    platformWorkspaceRef.current[platform] = {
      slots: slotsRef.current,
      previews: slotPreviews,
      signatures: { ...slotPreviewSignaturesRef.current }
    };
    platformSlotHistoryRef.current[platform] = slotsRef.current;
    setAdjustmentEditor(null);
    const cached = platformWorkspaceRef.current[nextPlatform];
    if (cached) {
      slotsRef.current = cached.slots;
      platformSlotHistoryRef.current[nextPlatform] = cached.slots;
      setSlots(cached.slots);
      setSlotPreviews({ ...cached.previews });
      slotPreviewSignaturesRef.current = { ...cached.signatures };
      setPlatform(nextPlatform);
      setMessage(`已切换到${nextPlatform === "jd" ? "京东" : "唯品会"}，已恢复该平台预览；缺失项会单独补充`);
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
    setPreviewBusy(false);
    jdBackgroundGenerationRef.current += 1;
    jdBackgroundPreparedRef.current = false;
    jdBackgroundRetryCountRef.current = 0;
    if (jdBackgroundRetryTimerRef.current !== null) {
      window.clearTimeout(jdBackgroundRetryTimerRef.current);
      jdBackgroundRetryTimerRef.current = null;
    }
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
  const activeEditorPreviewKey = activeEditorSlot && adjustmentEditor
    ? slotPreviewKey(platform, activeEditorSlot.file_name, adjustmentEditor.targetFolder)
    : "";
  const activeEditorInitialPreview = activeEditorSlot && adjustmentEditor && activeEditorPreviewKey
    && slotPreviewSignaturesRef.current[activeEditorPreviewKey] === slotPreviewSignature(
      activeEditorSlot,
      organizerProductInfo(),
      platform,
      adjustmentEditor.targetFolder
    )
    ? slotPreviews[activeEditorPreviewKey]
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
            <p>上传商品原图和模特图，吊牌图可按需补充</p>
          </div>
          <div className="button-row organizer-step-actions">
            {sessionId && <button disabled={uiBusy} onClick={startNewSession}><RefreshCw size={18} />开始新一轮</button>}
            <button className="primary" disabled={uiBusy || !products.length} onClick={() => analyze()}>
              {uiBusy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}自动整理初稿
            </button>
          </div>
        </div>
        <div className="organizer-upload-columns">
          <UploadSection title="商品原图" hint="支持多选" items={products} disabled={busy || uploadingKinds.has("product") || uploadingKinds.has("tag")} deleteDisabled={uiBusy} onUpload={(files) => upload("product", files)} onDelete={(item) => void deleteUploadedAsset("product", item)} onPreview={setPreview} />
          <UploadSection title="模特图" hint="支持多选" items={models} disabled={busy || uploadingKinds.has("model") || uploadingKinds.has("tag")} deleteDisabled={uiBusy} onUpload={(files) => upload("model", files)} onDelete={(item) => void deleteUploadedAsset("model", item)} onPreview={setPreview} />
          <UploadSection title="吊牌图" hint="可选 · 支持 Ctrl+V" items={tags} multiple={false} disabled={uiBusy} deleteDisabled={uiBusy} onUpload={(files) => upload("tag", files)} onDelete={(item) => void deleteUploadedAsset("tag", item)} onPreview={setPreview} />
        </div>
      </section>

      {slots.length > 0 && <>
        <section className="panel organizer-analysis-panel">
          <div className="organizer-step-header organizer-analysis-header">
            <div className="organizer-step-heading">
              <h2>2. 素材分析</h2>
              <p>核对自动分类和细节标签，确认后可按最新结果重新整理</p>
            </div>
            <div className="organizer-analysis-toolbar organizer-step-actions">
              <label className="organizer-api-select">
                <span>图文分析 API</span>
                <select value={analysisConfigId} onChange={(event) => setAnalysisConfigId(Number(event.target.value) || "")}>
                  {!analysisConfigs.length && <option value="">暂无图文分析 API</option>}
                  {analysisConfigs.map((item) => <option value={item.id} key={item.id}>{item.config_name}{item.is_default ? "（默认）" : ""}</option>)}
                </select>
              </label>
              <button disabled={uiBusy || !analysisConfigId} onClick={analyzeWithApi}><RefreshCw size={18} />API 分析</button>
              <button className="primary" disabled={uiBusy} onClick={() => analyze()}>
                {uiBusy ? <LoaderCircle className="spin" size={18} /> : <RefreshCw size={18} />}按标签重新整理
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
              <p>尺寸统一填写毫米（mm），用于产品信息图和尺寸对比图</p>
            </div>
          </div>
          <div className="organizer-info-grid">
            <label>商品名称<input value={info.product_name} onChange={(event) => setInfo({ ...info, product_name: event.target.value })} /></label>
            <label>长（mm）<input inputMode="decimal" placeholder="例如：200" value={info.product_length} aria-invalid={Boolean(info.product_length.trim()) && positiveDimensionValue(info.product_length) === null} onChange={(event) => setInfo({ ...info, product_length: event.target.value })} /></label>
            <label>高（mm）<input inputMode="decimal" placeholder="例如：140" value={info.product_height} aria-invalid={Boolean(info.product_height.trim()) && positiveDimensionValue(info.product_height) === null} onChange={(event) => setInfo({ ...info, product_height: event.target.value })} /></label>
            <label>厚（mm）<input inputMode="decimal" placeholder="例如：80" value={info.product_thickness} aria-invalid={Boolean(info.product_thickness.trim()) && positiveDimensionValue(info.product_thickness) === null} onChange={(event) => setInfo({ ...info, product_thickness: event.target.value })} /></label>
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
                    disabled={uiBusy || platformSwitching || platformRegenerating}
                    onClick={() => changePlatform(item.id)}
                  >
                    <span>{item.label}</span>
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="organizer-platform-refresh"
                disabled={uiBusy || platformSwitching || platformRegenerating || !products.length}
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
              <p>逐项确认成品、来源图片和调整结果后再导出</p>
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
                  const currentProductInfo = organizerProductInfo();
                  const outputReady = platform === "jd" && slot.file_name === "5.jpg"
                    ? jdComparisonDimensionsReady(currentProductInfo)
                    : platform === "vip" && slot.file_name === "401.jpg"
                      ? vipInfoDimensionsReady(currentProductInfo)
                      : true;
                  const renderedPreview = outputReady ? slotPreviews[previewKey] : undefined;
                  const outputSize = slotCanvasSize(slot.size, platform, group.folder);
                  return <article className={`organizer-slot${slot.file_name === "606.jpg" ? " is-composite" : ""}`} key={previewKey}>
                    <div
                      className="organizer-slot-preview"
                      style={{ aspectRatio: `${outputSize.width} / ${outputSize.height}` }}
                    >
                      {renderedPreview
                        ? <>
                          <button className="organizer-slot-edit-preview" type="button" disabled={uiBusy} onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder)} aria-label={`调整 ${slot.file_name} 最终成品`}><img src={renderedPreview} alt={`${slot.file_name} 最终成品`} onError={() => {
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
                        : <div className="generated-placeholder"><FileImage size={30} /><span>{!outputReady
                          ? platform === "vip" && slot.file_name === "401.jpg" ? "请填写有效的商品长、高、厚" : "请填写有效的商品长和高"
                          : previewBusy ? "正在套用模板" : "缺少素材"}</span></div>}
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
                          disabled={uiBusy || !slot.image_ids[0] || !outputReady}
                          onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder, "product")}
                        ><Crop size={16} />调整商品图</button>
                        <button
                          type="button"
                          className="organizer-adjust-output"
                          disabled={uiBusy || !slot.image_ids[0] || !outputReady}
                          onClick={() => openAdjustmentEditor(slot.file_name, 0, group.folder, "phone")}
                        ><Smartphone size={16} />调整手机</button>
                      </div> : count === 1 && <button
                        type="button"
                        className="organizer-adjust-output"
                        disabled={uiBusy || !slot.image_ids[0] || !outputReady}
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
                              disabled={uiBusy}
                              onChange={(event) => updateSlot(slot.file_name, index, Number(event.target.value))}
                            >
                              <option value="">请选择</option>
                              {optionsFor(slot).map((asset: any) => <option className={isManualAsset(asset.id) ? "manual-option" : ""} value={asset.id} key={asset.id}>{assetOptionLabel(asset, slot.kind)}</option>)}
                            </select>
                            {count > 1 && <button type="button" disabled={uiBusy || !slot.image_ids[index]} onClick={() => openAdjustmentEditor(slot.file_name, index, group.folder)} title={`调整来源 ${index + 1}`}>
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
            <button className="primary" disabled={uiBusy || previewBusy} onClick={exportZip}>{uiBusy ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />}下载 ZIP</button>
          </div>
        </section>
      </>}

      {message && <div className="alert warning organizer-status-message" role="status">{message}</div>}
      {preview && <ZoomableImagePreview url={preview} onClose={() => setPreview(null)} />}
      {adjustmentEditor && activeEditorSlot && activeEditorAsset && <SlotAdjustmentEditor
        key={`${platform}:${adjustmentEditor.targetFolder}:${activeEditorSlot.file_name}:${adjustmentEditor.sourceIndex}:${adjustmentEditor.targetObject}:${activeEditorAsset.id}`}
        sessionId={sessionId}
        slot={activeEditorSlot}
        sourceIndex={adjustmentEditor.sourceIndex}
        sourceImageId={activeEditorAsset.id}
        sourceUrl={activeEditorAsset.original_url || activeEditorAsset.preview_url}
        compositePrimaryUrl={activeEditorPrimaryAsset?.original_url || activeEditorPrimaryAsset?.preview_url}
        compositePrimaryImageId={activeEditorPrimaryAsset?.id}
        displaySourceUrl={adjustmentEditor.targetObject === "phone" ? "/organizer-assets/iphone_reference.png" : undefined}
        initialPreview={activeEditorInitialPreview}
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
