import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Crop,
  Download,
  Eraser,
  Eye,
  FileVideo2,
  Image as ImageIcon,
  Images,
  LoaderCircle,
  PackagePlus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  UploadCloud,
  Video,
  Wand2,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type {
  ProductApiConfig,
  ProductImageApiClient,
  ProductImageAsset,
  ProductImageOutput,
  ProductImageTask,
  ProductImageTaskResponse,
  ProductOutputRole,
  ProductSourceRole,
  ProductMutationResponse
} from "../productImages/types";
import "./ProductImages.css";

const SESSION_KEY = "product-image-current-task-id";
const MAX_IMAGES_PER_ROLE = 5;
const MAX_IMAGE_BYTES = 30 * 1024 * 1024;
const MAX_VIDEO_COUNT = 3;
const MAX_VIDEO_BYTES = 500 * 1024 * 1024;
const MAX_VIDEO_SECONDS = 60;
const MAX_BROWSER_VIDEO_FRAMES = 12;
const BROWSER_FRAME_MAX_EDGE = 1280;
const BROWSER_FRAME_JPEG_QUALITY = 0.82;
const MAX_TRANSPARENT_PNG_BYTES = 40 * 1024 * 1024;
const BROWSER_IMAGE_MAX_EDGE = 4096;
const BROWSER_IMAGE_JPEG_QUALITY = 0.9;

const productApi = api as unknown as ProductImageApiClient;

const SOURCE_DEFINITIONS: Array<{
  role: ProductSourceRole;
  title: string;
  hint: string;
  example: string;
}> = [
  {
    role: "front",
    title: "正面照片",
    hint: "包身正对镜头，轮廓、Logo、肩带和配件尽量完整。",
    example: "/organizer-assets/product_image_examples/front.jpg"
  },
  {
    role: "back",
    title: "背面照片",
    hint: "镜头与包身背面保持平行，完整拍到背部结构。",
    example: "/organizer-assets/product_image_examples/back.jpg"
  },
  {
    role: "semi_side",
    title: "半侧面照片",
    hint: "建议约 30°–45°，同时看清正面与侧面厚度。",
    example: "/organizer-assets/product_image_examples/semi_side.jpg"
  },
  {
    role: "top",
    title: "顶部开口照片",
    hint: "打开拉链或磁扣，移开肩带，清楚拍到开口和内里。",
    example: "/organizer-assets/product_image_examples/top.jpg"
  },
  {
    role: "logo",
    title: "Logo 近照",
    hint: "Logo 与周围材质都要清晰，避免反光、失焦和手部遮挡。",
    example: "/organizer-assets/product_image_examples/logo.jpg"
  }
];

const OUTPUT_DEFINITIONS: Array<{
  role: ProductOutputRole;
  title: string;
  fileName: string;
  ai: boolean;
}> = [
  { role: "front_transparent", title: "正面透明底", fileName: "01_正面透明.png", ai: false },
  { role: "front_main", title: "正面主图", fileName: "02_正面主图.jpg", ai: true },
  { role: "back", title: "背面图", fileName: "03_背面图.jpg", ai: true },
  { role: "semi_side", title: "半侧面图", fileName: "04_半侧面图.jpg", ai: true },
  { role: "top", title: "顶部开口图", fileName: "05_顶部开口图.jpg", ai: true },
  { role: "logo_detail", title: "Logo 细节", fileName: "06_Logo细节.jpg", ai: false }
];

const IMAGE_NAME_PATTERN = /\.(jpe?g|png|webp)$/i;
const BROWSER_DECODABLE_IMAGE_PATTERN = /\.(jpe?g|png|webp)$/i;
const VIDEO_NAME_PATTERN = /\.(mp4|mov)$/i;

function taskFromResponse(response: ProductImageTaskResponse | null | undefined): ProductImageTask | null {
  if (!response) return null;
  if ("task" in response) return response.task;
  return response;
}

function preferredConfig(rows: ProductApiConfig[]) {
  const enabled = rows.filter((item) => item.enabled);
  return enabled.find((item) => item.config_name?.trim() === "快速")
    || enabled.find((item) => item.is_default)
    || enabled[0]
    || null;
}

type SourceSlotView = {
  role: ProductSourceRole;
  assets: ProductImageAsset[];
  selected_asset_id?: number | null;
  status?: string;
  missing_reason?: string | null;
  guidance?: string | null;
};

function sourceSlot(task: ProductImageTask, role: ProductSourceRole): SourceSlotView {
  const reference = task.references?.find((item) => item.role === role);
  return {
    role,
    assets: (task.assets || []).filter((asset) => asset.media_type === "image" && asset.slot === role),
    selected_asset_id: reference?.selected_asset_id,
    status: reference?.status || "missing",
    missing_reason: reference?.status !== "ready" ? reference?.reason : null,
    guidance: reference?.reason
  };
}

function selectedSource(task: ProductImageTask, role: ProductSourceRole) {
  const slot = sourceSlot(task, role);
  return (task.assets || []).find((asset) => asset.id === slot.selected_asset_id) || null;
}

function outputSlot(task: ProductImageTask, role: ProductOutputRole): ProductImageOutput {
  return task.outputs?.find((item) => item.slot === role) || {
    slot: role,
    reference_role: role === "front_transparent" || role === "front_main" ? "front" : role === "logo_detail" ? "logo" : role,
    status: "pending",
    has_result: false,
    variants: {}
  };
}

function outputResult(output: ProductImageOutput) {
  const highres = output.variants?.highres;
  const size800 = output.variants?.["800"];
  const originalUrl = highres?.file_url || size800?.file_url;
  if (!originalUrl) return null;
  return {
    id: highres?.id || size800?.id || 0,
    original_url: originalUrl,
    size_800_url: size800?.file_url || originalUrl
  };
}

function outputError(output: ProductImageOutput) {
  return output.variants?.highres?.error_message
    || output.variants?.["800"]?.error_message
    || "";
}

function latestGenerationCall(task: ProductImageTask, role: ProductOutputRole) {
  const calls = (task.calls || [])
    .filter((call) => call.call_type === "generation" && call.slot === role)
    .sort((first, second) => (first.attempt_no || first.id) - (second.attempt_no || second.id));
  return calls[calls.length - 1] || null;
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    draft: "等待上传",
    analyzing: "分析中",
    analysis_failed: "分析失败",
    analysis_unknown: "分析结果未知",
    needs_material: "需要补拍",
    needs_input: "需要补拍",
    ready: "可以生成",
    ready_to_generate: "可以生成",
    generating: "生成中",
    paused: "已暂停",
    paused_unknown: "结果未知，已暂停",
    completed: "已完成",
    failed: "失败",
    pending: "等待处理",
    running: "处理中",
    success: "已完成",
    unknown: "结果未知",
    stale: "参考已更换",
    needs_source: "缺少真实素材"
  };
  return labels[status] || status;
}

function formatBytes(value?: number | null) {
  if (!value) return "";
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  return `${Math.ceil(value / 1024)} KB`;
}

function isSelectedSourceReady(task: ProductImageTask, role: ProductSourceRole) {
  const slot = sourceSlot(task, role);
  const selected = selectedSource(task, role);
  return Boolean(
    selected
    && slot.status !== "missing"
    && slot.status !== "invalid"
  );
}

function encodeCanvasJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
}

async function prepareLargePhotoInBrowser(file: File): Promise<{ file: File; optimized: boolean }> {
  if (!BROWSER_DECODABLE_IMAGE_PATTERN.test(file.name) || typeof window.createImageBitmap !== "function") {
    return { file, optimized: false };
  }
  let bitmap: ImageBitmap | null = null;
  let workingCanvas: HTMLCanvasElement | null = null;
  try {
    bitmap = await window.createImageBitmap(file, { imageOrientation: "from-image" });
    let width = bitmap.width;
    let height = bitmap.height;
    if (Math.max(width, height) <= BROWSER_IMAGE_MAX_EDGE) return { file, optimized: false };

    while (Math.max(width, height) > BROWSER_IMAGE_MAX_EDGE) {
      const currentMax = Math.max(width, height);
      const targetMax = Math.max(BROWSER_IMAGE_MAX_EDGE, Math.floor(currentMax / 2));
      const ratio = targetMax / currentMax;
      const nextWidth = Math.max(1, Math.round(width * ratio));
      const nextHeight = Math.max(1, Math.round(height * ratio));
      const nextCanvas = document.createElement("canvas");
      nextCanvas.width = nextWidth;
      nextCanvas.height = nextHeight;
      const context = nextCanvas.getContext("2d", { alpha: false });
      if (!context) return { file, optimized: false };
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, nextWidth, nextHeight);
      const source: CanvasImageSource | null = workingCanvas || bitmap;
      if (!source) return { file, optimized: false };
      context.drawImage(source, 0, 0, nextWidth, nextHeight);
      if (workingCanvas) {
        workingCanvas.width = 1;
        workingCanvas.height = 1;
      } else {
        bitmap?.close();
        bitmap = null;
      }
      workingCanvas = nextCanvas;
      width = nextWidth;
      height = nextHeight;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    }

    if (!workingCanvas) return { file, optimized: false };
    let blob = await encodeCanvasJpeg(workingCanvas, BROWSER_IMAGE_JPEG_QUALITY);
    if (blob && blob.size > MAX_IMAGE_BYTES) blob = await encodeCanvasJpeg(workingCanvas, 0.82);
    if (!blob || blob.size > MAX_IMAGE_BYTES) return { file, optimized: false };
    const stem = file.name.replace(/\.[^.]+$/, "").slice(0, 120) || "photo";
    return {
      file: new File([blob], `${stem}_4096.jpg`, { type: "image/jpeg", lastModified: file.lastModified }),
      optimized: true
    };
  } catch {
    // Browser-specific decode failures intentionally fall back to the original upload.
    return { file, optimized: false };
  } finally {
    bitmap?.close();
    if (workingCanvas) {
      workingCanvas.width = 1;
      workingCanvas.height = 1;
    }
  }
}

class BrowserVideoError extends Error {
  allowServerFallback: boolean;

  constructor(message: string, allowServerFallback = true) {
    super(message);
    this.name = "BrowserVideoError";
    this.allowServerFallback = allowServerFallback;
  }
}

function waitForVideoMetadata(video: HTMLVideoElement, fileName: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new BrowserVideoError(`浏览器读取 ${fileName} 超时`)), 15_000);
    const finish = (error?: Error) => {
      window.clearTimeout(timeout);
      video.removeEventListener("loadedmetadata", onLoaded);
      video.removeEventListener("error", onError);
      if (error) reject(error);
      else resolve();
    };
    const onLoaded = () => finish();
    const onError = () => finish(new BrowserVideoError(`浏览器无法解码 ${fileName}`));
    video.addEventListener("loadedmetadata", onLoaded);
    video.addEventListener("error", onError);
  });
}

function seekVideo(video: HTMLVideoElement, time: number, fileName: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new BrowserVideoError(`浏览器抽取 ${fileName} 的画面超时`)), 12_000);
    const finish = (error?: Error) => {
      window.clearTimeout(timeout);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("error", onError);
      if (error) reject(error);
      else resolve();
    };
    const onSeeked = () => {
      const videoWithFrameCallback = video as HTMLVideoElement & {
        requestVideoFrameCallback?: (callback: () => void) => number;
      };
      if (videoWithFrameCallback.requestVideoFrameCallback) {
        videoWithFrameCallback.requestVideoFrameCallback(() => finish());
      } else {
        window.requestAnimationFrame(() => finish());
      }
    };
    const onError = () => finish(new BrowserVideoError(`浏览器无法定位 ${fileName} 的视频画面`));
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("error", onError);
    try {
      video.currentTime = time;
    } catch {
      finish(new BrowserVideoError(`浏览器无法读取 ${fileName} 的候选画面`));
    }
  });
}

function canvasToJpeg(canvas: HTMLCanvasElement, fileName: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new BrowserVideoError(`浏览器无法压缩 ${fileName} 的候选帧`)),
      "image/jpeg",
      BROWSER_FRAME_JPEG_QUALITY
    );
  });
}

async function extractVideoFramesInBrowser(
  file: File,
  onProgress: (completed: number, total: number) => void
): Promise<{ duration: number; frames: File[] }> {
  const objectUrl = URL.createObjectURL(file);
  const video = document.createElement("video");
  const canvas = document.createElement("canvas");
  video.preload = "auto";
  video.muted = true;
  video.playsInline = true;
  video.src = objectUrl;

  try {
    const metadataPromise = waitForVideoMetadata(video, file.name);
    video.load();
    await metadataPromise;
    const duration = video.duration;
    if (!Number.isFinite(duration) || duration <= 0) {
      throw new BrowserVideoError(`浏览器无法读取 ${file.name} 的时长`);
    }
    if (duration > MAX_VIDEO_SECONDS + 0.01) {
      throw new BrowserVideoError(`${file.name} 超过 60 秒`, false);
    }
    if (!video.videoWidth || !video.videoHeight) {
      throw new BrowserVideoError(`浏览器无法读取 ${file.name} 的画面尺寸`);
    }

    const scale = Math.min(1, BROWSER_FRAME_MAX_EDGE / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new BrowserVideoError("当前浏览器不支持 Canvas 视频抽帧");

    // Short clips do not need twelve nearly identical frames; longer clips are capped at twelve.
    const frameCount = Math.min(MAX_BROWSER_VIDEO_FRAMES, Math.max(1, Math.ceil(duration * 2)));
    const stem = file.name.replace(/\.[^.]+$/, "").replace(/[^\w\u4e00-\u9fff.-]+/g, "_").slice(0, 80) || "video";
    const frames: File[] = [];
    for (let index = 0; index < frameCount; index += 1) {
      // Stay slightly away from the first/last encoded frame, which is commonly black or incomplete.
      const requestedTime = duration * (index + 0.5) / frameCount;
      const frameTime = Math.max(0, Math.min(Math.max(0, duration - 0.04), requestedTime));
      await seekVideo(video, frameTime, file.name);
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await canvasToJpeg(canvas, file.name);
      frames.push(new File(
        [blob],
        `${stem}_frame_${String(index + 1).padStart(2, "0")}_${frameTime.toFixed(3)}s.jpg`,
        { type: "image/jpeg", lastModified: Date.now() }
      ));
      onProgress(index + 1, frameCount);
      // Give rendering and GC a chance between frames instead of building one long blocking task.
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    }
    return { duration, frames };
  } finally {
    video.pause();
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(objectUrl);
    canvas.width = 1;
    canvas.height = 1;
  }
}

function createOpaqueMask(width: number, height: number) {
  const mask = document.createElement("canvas");
  mask.width = width;
  mask.height = height;
  const context = mask.getContext("2d");
  if (context) {
    context.fillStyle = "#fff";
    context.fillRect(0, 0, width, height);
  }
  return mask;
}

function createConservativeInitialMask(sourceImage: HTMLImageElement, width: number, height: number) {
  const longest = Math.max(sourceImage.naturalWidth, sourceImage.naturalHeight);
  const scale = Math.min(1, 1200 / Math.max(1, longest));
  const sampleWidth = Math.max(1, Math.round(sourceImage.naturalWidth * scale));
  const sampleHeight = Math.max(1, Math.round(sourceImage.naturalHeight * scale));
  const sampleCanvas = document.createElement("canvas");
  sampleCanvas.width = sampleWidth;
  sampleCanvas.height = sampleHeight;
  const sampleContext = sampleCanvas.getContext("2d", { willReadFrequently: true });
  if (!sampleContext) return createOpaqueMask(width, height);
  sampleContext.drawImage(sourceImage, 0, 0, sampleWidth, sampleHeight);

  let pixels: ImageData;
  try {
    pixels = sampleContext.getImageData(0, 0, sampleWidth, sampleHeight);
  } catch {
    return createOpaqueMask(width, height);
  }

  const patchSize = Math.max(2, Math.min(10, Math.round(Math.min(sampleWidth, sampleHeight) * 0.012)));
  const cornerOrigins = [
    [0, 0],
    [sampleWidth - patchSize, 0],
    [0, sampleHeight - patchSize],
    [sampleWidth - patchSize, sampleHeight - patchSize]
  ];
  const cornerColors = cornerOrigins.map(([originX, originY]) => {
    let red = 0;
    let green = 0;
    let blue = 0;
    let count = 0;
    for (let y = originY; y < originY + patchSize; y += 1) {
      for (let x = originX; x < originX + patchSize; x += 1) {
        const offset = (y * sampleWidth + x) * 4;
        red += pixels.data[offset];
        green += pixels.data[offset + 1];
        blue += pixels.data[offset + 2];
        count += 1;
      }
    }
    return [red / count, green / count, blue / count] as const;
  });
  const brightness = (color: readonly number[]) => color[0] * 0.2126 + color[1] * 0.7152 + color[2] * 0.0722;
  const spread = (color: readonly number[]) => Math.max(...color) - Math.min(...color);
  const distance = (first: readonly number[], second: readonly number[]) => Math.sqrt(
    (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2 + (first[2] - second[2]) ** 2
  );
  const background = [0, 1, 2].map((channel) => (
    cornerColors.reduce((sum, color) => sum + color[channel], 0) / cornerColors.length
  ));
  const cornersReliable = cornerColors.every((color) => brightness(color) >= 210 && spread(color) <= 52 && distance(color, background) <= 34);
  if (!cornersReliable) return createOpaqueMask(width, height);

  const matchesBackground = (pixelIndex: number) => {
    const offset = pixelIndex * 4;
    const color = [pixels.data[offset], pixels.data[offset + 1], pixels.data[offset + 2]];
    return pixels.data[offset + 3] >= 245
      && brightness(color) >= 180
      && spread(color) <= 70
      && distance(color, background) <= 52;
  };
  let edgeSamples = 0;
  let matchingEdgeSamples = 0;
  const edgeStep = Math.max(1, Math.floor((sampleWidth + sampleHeight) / 500));
  const inspectEdge = (index: number) => {
    edgeSamples += 1;
    if (matchesBackground(index)) matchingEdgeSamples += 1;
  };
  for (let x = 0; x < sampleWidth; x += edgeStep) {
    inspectEdge(x);
    inspectEdge((sampleHeight - 1) * sampleWidth + x);
  }
  for (let y = 0; y < sampleHeight; y += edgeStep) {
    inspectEdge(y * sampleWidth);
    inspectEdge(y * sampleWidth + sampleWidth - 1);
  }
  if (!edgeSamples || matchingEdgeSamples / edgeSamples < 0.72) return createOpaqueMask(width, height);

  const pixelCount = sampleWidth * sampleHeight;
  const backgroundConnected = new Uint8Array(pixelCount);
  const queue = new Int32Array(pixelCount);
  let head = 0;
  let tail = 0;
  const enqueue = (index: number) => {
    if (index < 0 || index >= pixelCount || backgroundConnected[index] || !matchesBackground(index)) return;
    backgroundConnected[index] = 1;
    queue[tail] = index;
    tail += 1;
  };
  for (let x = 0; x < sampleWidth; x += 1) {
    enqueue(x);
    enqueue((sampleHeight - 1) * sampleWidth + x);
  }
  for (let y = 0; y < sampleHeight; y += 1) {
    enqueue(y * sampleWidth);
    enqueue(y * sampleWidth + sampleWidth - 1);
  }
  while (head < tail) {
    const index = queue[head];
    head += 1;
    const x = index % sampleWidth;
    if (x > 0) enqueue(index - 1);
    if (x + 1 < sampleWidth) enqueue(index + 1);
    if (index >= sampleWidth) enqueue(index - sampleWidth);
    if (index + sampleWidth < pixelCount) enqueue(index + sampleWidth);
  }

  const removedRatio = tail / pixelCount;
  if (removedRatio < 0.04 || removedRatio > 0.9) return createOpaqueMask(width, height);
  const maskPixels = sampleContext.createImageData(sampleWidth, sampleHeight);
  const protectionRadius = 2;
  for (let y = 0; y < sampleHeight; y += 1) {
    for (let x = 0; x < sampleWidth; x += 1) {
      const index = y * sampleWidth + x;
      let safelyBackground = Boolean(backgroundConnected[index]);
      // Keep a small opaque safety band around every detected foreground edge.
      for (let offsetY = -protectionRadius; safelyBackground && offsetY <= protectionRadius; offsetY += 1) {
        const checkY = y + offsetY;
        if (checkY < 0 || checkY >= sampleHeight) continue;
        for (let offsetX = -protectionRadius; offsetX <= protectionRadius; offsetX += 1) {
          const checkX = x + offsetX;
          if (checkX < 0 || checkX >= sampleWidth) continue;
          if (!backgroundConnected[checkY * sampleWidth + checkX]) {
            safelyBackground = false;
            break;
          }
        }
      }
      const outputOffset = index * 4;
      maskPixels.data[outputOffset] = 255;
      maskPixels.data[outputOffset + 1] = 255;
      maskPixels.data[outputOffset + 2] = 255;
      maskPixels.data[outputOffset + 3] = safelyBackground ? 0 : 255;
    }
  }
  sampleContext.putImageData(maskPixels, 0, 0);
  const mask = createOpaqueMask(width, height);
  const maskContext = mask.getContext("2d");
  if (maskContext) {
    maskContext.clearRect(0, 0, width, height);
    maskContext.imageSmoothingEnabled = true;
    maskContext.drawImage(sampleCanvas, 0, 0, width, height);
  }
  sampleCanvas.width = 1;
  sampleCanvas.height = 1;
  return mask;
}

function SourceUploadCard({
  definition,
  slot,
  selectedId,
  disabled,
  busy,
  onUpload,
  onSelect,
  onDelete,
  onPreview
}: {
  definition: (typeof SOURCE_DEFINITIONS)[number];
  slot: SourceSlotView;
  selectedId?: number | null;
  disabled: boolean;
  busy: boolean;
  onUpload: (files: File[]) => void;
  onSelect: (asset: ProductImageAsset) => void;
  onDelete: (asset: ProductImageAsset) => void;
  onPreview: (url: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const images = slot.assets || [];
  const selected = Boolean(selectedId);

  return (
    <article className={`pi-source-card ${selected ? "is-ready" : ""} ${slot.status === "missing" || slot.status === "invalid" ? "has-issue" : ""}`}>
      <header>
        <div>
          <span className="pi-step-dot">{SOURCE_DEFINITIONS.findIndex((item) => item.role === definition.role) + 1}</span>
          <div><strong>{definition.title}</strong><small>最多 {MAX_IMAGES_PER_ROLE} 张</small></div>
        </div>
        {selected
          ? <span className="pi-ready-label"><Check size={14} />参考已选</span>
          : <span className="pi-required-label">待补全</span>}
      </header>

      <div className="pi-source-guide">
        <button type="button" onClick={() => onPreview(definition.example)} title={`查看${definition.title}示例`}>
          <img src={definition.example} alt={`${definition.title}示例`} />
          <span><Eye size={14} />拍摄示例</span>
        </button>
        <p>{definition.hint}</p>
      </div>

      <button
        type="button"
        className="pi-upload-button"
        disabled={disabled || busy || images.length >= MAX_IMAGES_PER_ROLE}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? <LoaderCircle className="spin" size={18} /> : <UploadCloud size={18} />}
        {busy ? "正在上传" : images.length ? "继续添加照片" : "选择照片"}
      </button>
      <input
        ref={inputRef}
        className="pi-hidden-input"
        type="file"
        accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
        multiple
        disabled={disabled || busy}
        onChange={(event) => {
          onUpload(Array.from(event.target.files || []));
          event.currentTarget.value = "";
        }}
      />

      {images.length > 0 && <div className="pi-asset-grid">
        {images.map((asset) => {
          const active = asset.id === selectedId;
          return <div className={`pi-asset-thumb ${active ? "is-selected" : ""}`} key={asset.id}>
            <button type="button" className="pi-asset-select" onClick={() => onSelect(asset)} title="选为该角度参考图">
              <img src={asset.file_url} alt={asset.file_name} />
              <span>{active ? <><CheckCircle2 size={13} />当前参考</> : "选为参考"}</span>
            </button>
            <button type="button" className="pi-asset-delete" onClick={() => onDelete(asset)} title="删除素材" aria-label={`删除 ${asset.file_name}`}>
              <X size={13} />
            </button>
            {asset.analysis_valid === false && <i title={asset.analysis_reason || "素材不合格"}><AlertTriangle size={14} /></i>}
          </div>;
        })}
      </div>}

      {(slot.missing_reason || slot.guidance) && <div className="pi-slot-warning">
        <AlertTriangle size={15} />
        <span>{slot.missing_reason || slot.guidance}</span>
      </div>}
    </article>
  );
}

function TransparentEditor({
  sourceUrl,
  imageUrl,
  busy,
  onClose,
  onSave
}: {
  sourceUrl: string;
  imageUrl?: string;
  busy: boolean;
  onClose: () => void;
  onSave: (file: File) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const originalRef = useRef<HTMLCanvasElement | null>(null);
  const maskRef = useRef<HTMLCanvasElement | null>(null);
  const initialMaskRef = useRef<HTMLCanvasElement | null>(null);
  const lastPointRef = useRef<{ x: number; y: number } | null>(null);
  const drawingRef = useRef(false);
  const renderFrameRef = useRef<number | null>(null);
  const [mode, setMode] = useState<"erase" | "restore">("erase");
  const [brushSize, setBrushSize] = useState(64);
  const [ready, setReady] = useState(false);
  const [encoding, setEncoding] = useState(false);
  const [error, setError] = useState("");

  function renderComposite() {
    const canvas = canvasRef.current;
    const original = originalRef.current;
    const mask = maskRef.current;
    if (!canvas || !original || !mask) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.globalCompositeOperation = "source-over";
    context.drawImage(original, 0, 0);
    context.globalCompositeOperation = "destination-in";
    context.drawImage(mask, 0, 0);
    context.globalCompositeOperation = "source-over";
  }

  function scheduleRender() {
    if (renderFrameRef.current !== null) return;
    renderFrameRef.current = window.requestAnimationFrame(() => {
      renderFrameRef.current = null;
      renderComposite();
    });
  }

  function resetMask() {
    const mask = maskRef.current;
    const initialMask = initialMaskRef.current;
    if (!mask || !initialMask) return;
    const context = mask.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, mask.width, mask.height);
    context.globalCompositeOperation = "source-over";
    context.drawImage(initialMask, 0, 0);
    renderComposite();
  }

  useEffect(() => {
    let active = true;
    setReady(false);
    setError("");

    function loadImage(url: string) {
      return new Promise<HTMLImageElement>((resolve, reject) => {
        const image = new Image();
        image.crossOrigin = "anonymous";
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error(`Failed to load ${url}`));
        image.src = url;
      });
    }

    void Promise.all([
      loadImage(sourceUrl),
      imageUrl ? loadImage(imageUrl) : Promise.resolve(null)
    ]).then(([sourceImage, transparentImage]) => {
      if (!active) return;
      const dimensionSource = transparentImage || sourceImage;
      const scale = Math.min(1, 4096 / Math.max(dimensionSource.naturalWidth, dimensionSource.naturalHeight));
      const width = Math.max(1, Math.round(dimensionSource.naturalWidth * scale));
      const height = Math.max(1, Math.round(dimensionSource.naturalHeight * scale));
      const canvas = canvasRef.current;
      if (!canvas) return;
      canvas.width = width;
      canvas.height = height;

      const original = document.createElement("canvas");
      original.width = width;
      original.height = height;
      original.getContext("2d")?.drawImage(sourceImage, 0, 0, width, height);

      const initialMask = document.createElement("canvas");
      initialMask.width = width;
      initialMask.height = height;
      const initialContext = initialMask.getContext("2d", { willReadFrequently: Boolean(transparentImage) });
      if (!initialContext) throw new Error("Canvas context unavailable");
      if (transparentImage) {
        initialContext.drawImage(transparentImage, 0, 0, width, height);
        const maskPixels = initialContext.getImageData(0, 0, width, height);
        for (let index = 0; index < maskPixels.data.length; index += 4) {
          maskPixels.data[index] = 255;
          maskPixels.data[index + 1] = 255;
          maskPixels.data[index + 2] = 255;
        }
        initialContext.putImageData(maskPixels, 0, 0);
      } else {
        const localMask = createConservativeInitialMask(sourceImage, width, height);
        initialContext.clearRect(0, 0, width, height);
        initialContext.drawImage(localMask, 0, 0);
        localMask.width = 1;
        localMask.height = 1;
      }

      const mask = document.createElement("canvas");
      mask.width = width;
      mask.height = height;
      mask.getContext("2d")?.drawImage(initialMask, 0, 0);
      originalRef.current = original;
      initialMaskRef.current = initialMask;
      maskRef.current = mask;
      renderComposite();
      setReady(true);
    }).catch(() => {
      if (active) setError("原始正面素材或透明图载入失败，请关闭后重试");
    });

    return () => {
      active = false;
      if (renderFrameRef.current !== null) window.cancelAnimationFrame(renderFrameRef.current);
    };
  }, [sourceUrl, imageUrl]);

  function pointFromEvent(event: React.PointerEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const bounds = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - bounds.left) / bounds.width * canvas.width,
      y: (event.clientY - bounds.top) / bounds.height * canvas.height
    };
  }

  function paint(from: { x: number; y: number }, to: { x: number; y: number }) {
    const mask = maskRef.current;
    if (!mask) return;
    const context = mask.getContext("2d");
    if (!context) return;
    context.save();
    context.globalCompositeOperation = mode === "erase" ? "destination-out" : "source-over";
    context.strokeStyle = "#fff";
    context.lineWidth = brushSize;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.beginPath();
    context.moveTo(from.x, from.y);
    context.lineTo(to.x, to.y);
    context.stroke();
    context.restore();
    scheduleRender();
  }

  function startPaint(event: React.PointerEvent<HTMLCanvasElement>) {
    if (!ready || busy) return;
    const point = pointFromEvent(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drawingRef.current = true;
    lastPointRef.current = point;
    paint(point, point);
  }

  function movePaint(event: React.PointerEvent<HTMLCanvasElement>) {
    if (!drawingRef.current || !lastPointRef.current) return;
    const point = pointFromEvent(event);
    if (!point) return;
    paint(lastPointRef.current, point);
    lastPointRef.current = point;
  }

  function finishPaint() {
    drawingRef.current = false;
    lastPointRef.current = null;
    renderComposite();
  }

  return <div className="pi-modal" role="dialog" aria-modal="true" aria-label="透明底修边">
    <section className="pi-editor-dialog pi-transparent-editor">
      <header>
        <div><h2>透明底修边</h2><p>棋盘格代表透明区域。擦除多余背景，或恢复被误删的包体、链条和肩带。</p></div>
        <button type="button" className="pi-icon-button" onClick={onClose}><X size={20} /></button>
      </header>
      <div className="pi-transparent-stage">
        {!ready && !error && <LoaderCircle className="spin" size={26} />}
        <canvas
          ref={canvasRef}
          onPointerDown={startPaint}
          onPointerMove={movePaint}
          onPointerUp={finishPaint}
          onPointerCancel={finishPaint}
        />
      </div>
      {error && <div className="pi-alert is-error"><AlertTriangle size={16} />{error}</div>}
      <footer className="pi-editor-toolbar">
        <div className="pi-tool-group">
          <button type="button" className={mode === "erase" ? "is-active" : ""} onClick={() => setMode("erase")}><Eraser size={17} />擦除</button>
          <button type="button" className={mode === "restore" ? "is-active" : ""} onClick={() => setMode("restore")}><RefreshCw size={17} />恢复</button>
          <button type="button" onClick={resetMask}><RotateCcw size={17} />重置</button>
        </div>
        <label>画笔 {brushSize}px<input type="range" min="12" max="240" step="4" value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} /></label>
        <button
          type="button"
          className="pi-primary"
          disabled={!ready || busy || encoding}
          onClick={() => {
            renderComposite();
            const canvas = canvasRef.current;
            if (!canvas) return;
            setEncoding(true);
            setError("");
            canvas.toBlob((blob) => {
              setEncoding(false);
              if (!blob) {
                setError("浏览器无法编码透明 PNG，请重试");
                return;
              }
              if (blob.size > MAX_TRANSPARENT_PNG_BYTES) {
                setError("透明 PNG 超过 40MB，请缩小原图后重新上传正面照片。");
                return;
              }
              onSave(new File([blob], "01_正面透明.png", { type: "image/png", lastModified: Date.now() }));
            }, "image/png");
          }}
        >{busy || encoding ? <LoaderCircle className="spin" size={17} /> : <Save size={17} />}{busy ? "上传中" : encoding ? "编码中" : "保存透明 PNG"}</button>
      </footer>
    </section>
  </div>;
}

function LogoCropEditor({
  imageUrl,
  busy,
  onClose,
  onSave
}: {
  imageUrl: string;
  busy: boolean;
  onClose: () => void;
  onSave: (crop: { left: number; top: number; right: number; bottom: number }) => void;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; y: number; centerX: number; centerY: number } | null>(null);
  const [natural, setNatural] = useState({ width: 1, height: 1 });
  const [center, setCenter] = useState({ x: 0.5, y: 0.5 });
  const [zoom, setZoom] = useState(1.7);

  const crop = useMemo(() => {
    const pixelSize = Math.min(natural.width, natural.height) / zoom;
    const width = Math.min(1, pixelSize / natural.width);
    const height = Math.min(1, pixelSize / natural.height);
    const left = Math.max(0, Math.min(1 - width, center.x - width / 2));
    const top = Math.max(0, Math.min(1 - height, center.y - height / 2));
    return { left, top, right: left + width, bottom: top + height, width, height };
  }, [center, natural, zoom]);

  function clampCenter(x: number, y: number, nextZoom = zoom) {
    const pixelSize = Math.min(natural.width, natural.height) / nextZoom;
    const halfWidth = Math.min(1, pixelSize / natural.width) / 2;
    const halfHeight = Math.min(1, pixelSize / natural.height) / 2;
    return {
      x: Math.max(halfWidth, Math.min(1 - halfWidth, x)),
      y: Math.max(halfHeight, Math.min(1 - halfHeight, y))
    };
  }

  function startDrag(event: React.PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { x: event.clientX, y: event.clientY, centerX: center.x, centerY: center.y };
  }

  function moveDrag(event: React.PointerEvent<HTMLDivElement>) {
    const start = dragRef.current;
    const stage = stageRef.current;
    if (!start || !stage) return;
    const bounds = stage.getBoundingClientRect();
    setCenter(clampCenter(
      start.centerX + (event.clientX - start.x) / bounds.width,
      start.centerY + (event.clientY - start.y) / bounds.height
    ));
  }

  return <div className="pi-modal" role="dialog" aria-modal="true" aria-label="Logo 方形裁剪">
    <section className="pi-editor-dialog pi-logo-editor">
      <header>
        <div><h2>Logo 方形裁剪</h2><p>拖动方框确定位置，用缩放滑杆控制 Logo 与周围材质的取景范围。</p></div>
        <button type="button" className="pi-icon-button" onClick={onClose}><X size={20} /></button>
      </header>
      <div className="pi-logo-workspace">
        <div className="pi-logo-source" ref={stageRef}>
          <img src={imageUrl} alt="Logo 原始近照" onLoad={(event) => setNatural({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} draggable={false} />
          <div
            className="pi-logo-crop-box"
            style={{ left: `${crop.left * 100}%`, top: `${crop.top * 100}%`, width: `${crop.width * 100}%`, height: `${crop.height * 100}%` }}
            onPointerDown={startDrag}
            onPointerMove={moveDrag}
            onPointerUp={() => { dragRef.current = null; }}
            onPointerCancel={() => { dragRef.current = null; }}
          ><span>1:1</span></div>
        </div>
        <div className="pi-logo-preview">
          <strong>方形成品预览</strong>
          <div><img src={imageUrl} alt="Logo 裁剪预览" draggable={false} style={{
            left: `${-crop.left / crop.width * 100}%`,
            top: `${-crop.top / crop.height * 100}%`,
            width: `${100 / crop.width}%`,
            height: `${100 / crop.height}%`
          }} /></div>
        </div>
      </div>
      <footer className="pi-editor-toolbar">
        <label>取景缩放 {zoom.toFixed(1)}×<input type="range" min="1" max="4" step="0.1" value={zoom} onChange={(event) => {
          const nextZoom = Number(event.target.value);
          setZoom(nextZoom);
          setCenter((value) => clampCenter(value.x, value.y, nextZoom));
        }} /></label>
        <button type="button" onClick={() => { setCenter({ x: 0.5, y: 0.5 }); setZoom(1.7); }}><RotateCcw size={17} />重置</button>
        <button type="button" className="pi-primary" disabled={busy} onClick={() => onSave({ left: crop.left, top: crop.top, right: crop.right, bottom: crop.bottom })}>
          {busy ? <LoaderCircle className="spin" size={17} /> : <Crop size={17} />}{busy ? "保存中" : "保存 Logo 取景"}
        </button>
      </footer>
    </section>
  </div>;
}

export default function ProductImages() {
  const [task, setTask] = useState<ProductImageTask | null>(null);
  const [productCode, setProductCode] = useState("");
  const [color, setColor] = useState("");
  const [analysisConfigs, setAnalysisConfigs] = useState<ProductApiConfig[]>([]);
  const [generationConfigs, setGenerationConfigs] = useState<ProductApiConfig[]>([]);
  const [analysisConfigId, setAnalysisConfigId] = useState<number | "">("");
  const [generationConfigId, setGenerationConfigId] = useState<number | "">("");
  const [analysisConfirmed, setAnalysisConfirmed] = useState(false);
  const [generationConfirmed, setGenerationConfirmed] = useState(false);
  const [chargeRiskConfirmed, setChargeRiskConfirmed] = useState(false);
  const [frameAssignments, setFrameAssignments] = useState<Record<number, ProductSourceRole>>({});
  const [busyAction, setBusyAction] = useState("");
  const [videoProgress, setVideoProgress] = useState("");
  const [restoring, setRestoring] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");
  const [transparentEditor, setTransparentEditor] = useState<{ sourceUrl: string; imageUrl?: string } | null>(null);
  const [logoEditorUrl, setLogoEditorUrl] = useState("");
  const [regenerateRole, setRegenerateRole] = useState<ProductOutputRole | null>(null);
  const [regenerateConfirmed, setRegenerateConfirmed] = useState(false);
  const [regenerateChargeRiskConfirmed, setRegenerateChargeRiskConfirmed] = useState(false);

  function applyTask(next: ProductImageTask) {
    setTask(next);
    window.sessionStorage.setItem(SESSION_KEY, String(next.id));
    setProductCode(next.product_code || "");
    setColor(next.color || "");
    if (next.analysis_config_id) setAnalysisConfigId(next.analysis_config_id);
    if (next.image_config_id) setGenerationConfigId(next.image_config_id);
  }

  async function refreshTask(taskId = task?.id) {
    if (!taskId) return null;
    const next = taskFromResponse(await productApi.getProductImageTask(taskId));
    if (next) applyTask(next);
    return next;
  }

  async function acceptResponse(response: ProductMutationResponse) {
    const next = response && !("ok" in response) ? taskFromResponse(response) : null;
    if (next) {
      applyTask(next);
      return next;
    }
    return refreshTask();
  }

  async function runMutation(
    key: string,
    operation: () => Promise<ProductMutationResponse>,
    successMessage = ""
  ) {
    setBusyAction(key);
    setError("");
    setMessage("");
    try {
      const response = await operation();
      const next = await acceptResponse(response);
      if (successMessage) setMessage(successMessage);
      return next;
    } catch (requestError: any) {
      setError(requestError?.message || "操作失败");
      return null;
    } finally {
      setBusyAction("");
    }
  }

  useEffect(() => {
    let active = true;
    Promise.all([api.getApiConfigs("text_analysis"), api.getApiConfigs("image_generation")])
      .then(([analysisRows, generationRows]) => {
        if (!active) return;
        const analysis = analysisRows.filter((item: ProductApiConfig) => item.enabled && item.api_type === "text_analysis");
        const generation = generationRows.filter((item: ProductApiConfig) => item.enabled && item.api_type === "image_generation");
        setAnalysisConfigs(analysis);
        setGenerationConfigs(generation);
        setAnalysisConfigId((current) => current || preferredConfig(analysis)?.id || "");
        setGenerationConfigId((current) => current || preferredConfig(generation)?.id || "");
      })
      .catch((requestError: any) => active && setError(requestError?.message || "API 配置加载失败"));

    const taskId = window.sessionStorage.getItem(SESSION_KEY);
    if (!taskId) {
      setRestoring(false);
    } else {
      productApi.getProductImageTask(taskId)
        .then((response) => {
          if (!active) return;
          const restored = taskFromResponse(response);
          if (restored) applyTask(restored);
        })
        .catch(() => {
          if (!active) return;
          window.sessionStorage.removeItem(SESSION_KEY);
          setMessage("上一轮临时素材已过期，请重新建立任务。");
        })
        .finally(() => active && setRestoring(false));
    }
    return () => { active = false; };
  }, []);

  const hasLateUnknown = Boolean(
    task?.outputs?.some((item) => item.status === "unknown")
    || task?.calls?.some((item) => item.status === "unknown" && !item.finished_at)
  );
  const shouldPoll = Boolean(task && (
    ["analyzing", "generating"].includes(task.status)
    || task.outputs?.some((item) => item.status === "running")
    || task.calls?.some((item) => item.status === "running")
    || hasLateUnknown
  ));

  useEffect(() => {
    if (!task?.id || !shouldPoll) return;
    let active = true;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const next = taskFromResponse(await productApi.getProductImageTask(task.id));
        if (active && next) applyTask(next);
      } catch {
        // A transient polling failure must not submit or retry a paid request.
      } finally {
        polling = false;
      }
    };
    const timer = window.setInterval(poll, hasLateUnknown ? 5000 : 1800);
    return () => { active = false; window.clearInterval(timer); };
  }, [task?.id, shouldPoll, hasLateUnknown]);

  async function createTask() {
    const nextCode = productCode.trim();
    const nextColor = color.trim();
    if (!nextCode || !nextColor) {
      setError("请先填写商品款号和颜色；不同颜色需要分别建立任务。");
      return;
    }
    await runMutation(
      "create",
      () => productApi.createProductImageTask({ product_code: nextCode, color: nextColor }),
      "任务已建立。请分别上传五类照片，视频可选。"
    );
  }

  async function uploadImages(role: ProductSourceRole, files: File[]) {
    if (!task || !files.length) return;
    const currentCount = sourceSlot(task, role).assets.length;
    const supported = files.filter((file) => IMAGE_NAME_PATTERN.test(file.name));
    const oversized = supported.filter((file) => file.size > MAX_IMAGE_BYTES);
    const withinSize = supported.filter((file) => file.size <= MAX_IMAGE_BYTES);
    const accepted = withinSize.slice(0, Math.max(0, MAX_IMAGES_PER_ROLE - currentCount));
    const skipped = files.length - accepted.length;
    if (!accepted.length) {
      setError(oversized.length ? "单张照片不能超过 30MB。" : "每个入口最多 5 张，仅支持 JPG、PNG、WebP。" );
      return;
    }
    setGenerationConfirmed(false);
    setBusyAction(`upload:${role}`);
    setError("");
    setMessage("");
    try {
      const prepared: File[] = [];
      let optimizedCount = 0;
      // Decode and release one source at a time so several large photos never coexist as bitmaps.
      for (const file of accepted) {
        const result = await prepareLargePhotoInBrowser(file);
        prepared.push(result.file);
        if (result.optimized) optimizedCount += 1;
      }
      const response = await productApi.uploadProductImages(task.id, role, prepared);
      await acceptResponse(response);
      const notes = [`已上传 ${prepared.length} 张照片`];
      if (optimizedCount) notes.push(`${optimizedCount} 张超大图已在浏览器缩至最长边 4096`);
      if (skipped) notes.push(`另有 ${skipped} 张因格式、大小或数量限制被跳过`);
      setMessage(`${notes.join("；")}。`);
    } catch (requestError: any) {
      setError(requestError?.message || "照片上传失败");
    } finally {
      setBusyAction("");
    }
  }

  async function uploadVideos(files: File[]) {
    if (!task || !files.length) return;
    const remaining = Math.max(0, MAX_VIDEO_COUNT - task.assets.filter((asset) => asset.media_type === "video").length);
    const supported = files.filter((file) => VIDEO_NAME_PATTERN.test(file.name));
    const withinSize = supported.filter((file) => file.size <= MAX_VIDEO_BYTES);
    const candidates = withinSize.slice(0, remaining);
    const validationErrors: string[] = [];
    files.filter((file) => !VIDEO_NAME_PATTERN.test(file.name)).forEach((file) => validationErrors.push(`${file.name} 不是 MP4/MOV`));
    supported.filter((file) => file.size > MAX_VIDEO_BYTES).forEach((file) => validationErrors.push(`${file.name} 超过 500MB`));
    if (withinSize.length > candidates.length) validationErrors.push(`另有 ${withinSize.length - candidates.length} 个视频超过本任务最多 3 个的限制`);
    if (!candidates.length) {
      setError(validationErrors.join("；") || "最多选择 3 个 MP4/MOV 视频，每个不超过 500MB。");
      return;
    }
    setBusyAction("upload:video-local");
    setError("");
    setMessage("");
    setGenerationConfirmed(false);
    try {
      const fallbackCandidates: Array<{ file: File; reason: string }> = [];
      let browserUploaded = 0;
      let serverUploaded = 0;
      for (let fileIndex = 0; fileIndex < candidates.length; fileIndex += 1) {
        const file = candidates[fileIndex];
        let extracted: { duration: number; frames: File[] };
        try {
          setVideoProgress(`本地处理 ${fileIndex + 1}/${candidates.length}：读取 ${file.name}`);
          extracted = await extractVideoFramesInBrowser(file, (completed, total) => {
            setVideoProgress(`本地处理 ${fileIndex + 1}/${candidates.length}：抽取 ${completed}/${total} 帧`);
          });
        } catch (localError: any) {
          if (localError instanceof BrowserVideoError && !localError.allowServerFallback) {
            validationErrors.push(localError.message);
          } else {
            fallbackCandidates.push({ file, reason: localError?.message || "浏览器无法完成抽帧" });
          }
          continue;
        }
        try {
          setVideoProgress(`仅上传 ${file.name} 的 ${extracted.frames.length} 张候选帧`);
          const response = await productApi.uploadProductVideoFrames(task.id, {
            files: extracted.frames,
            video_name: file.name,
            duration_seconds: extracted.duration
          });
          const next = taskFromResponse(response);
          if (next) applyTask(next);
          browserUploaded += 1;
        } catch (requestError: any) {
          validationErrors.push(`${file.name} 的候选帧上传失败：${requestError?.message || "请求失败"}`);
        }
      }

      if (fallbackCandidates.length) {
        const failedNames = fallbackCandidates.map((item) => `${item.file.name}（${item.reason}）`).join("、");
        const confirmed = window.confirm(
          `以下视频无法在浏览器本地解码或抽帧：${failedNames}\n\n是否改为上传原视频到服务器处理？这会占用服务器磁盘和内存；服务器仍会检查 60 秒限制。选择“取消”则不上传这些视频。`
        );
        if (confirmed) {
          setBusyAction("upload:video-server");
          // Upload one fallback video at a time to avoid a multi-video request occupying excessive server memory.
          for (let index = 0; index < fallbackCandidates.length; index += 1) {
            const fallback = fallbackCandidates[index];
            setVideoProgress(`服务器兼容处理 ${index + 1}/${fallbackCandidates.length}：${fallback.file.name}`);
            try {
              const response = await productApi.uploadProductVideos(task.id, [fallback.file]);
              const next = taskFromResponse(response);
              if (next) applyTask(next);
              serverUploaded += 1;
            } catch (requestError: any) {
              validationErrors.push(`${fallback.file.name}：${requestError?.message || "服务器处理失败"}`);
              break;
            }
          }
        } else {
          validationErrors.push(`${fallbackCandidates.length} 个无法本地抽帧的视频已按你的选择取消上传`);
        }
      }

      if (browserUploaded || serverUploaded) {
        const parts: string[] = [];
        if (browserUploaded) parts.push(`${browserUploaded} 个视频已在浏览器抽帧，仅上传候选帧`);
        if (serverUploaded) parts.push(`${serverUploaded} 个视频经确认后上传原文件兼容处理`);
        setMessage(`${parts.join("；")}。`);
      }
      if (validationErrors.length) setError(validationErrors.join("；"));
    } finally {
      setBusyAction("");
      setVideoProgress("");
    }
  }

  async function deleteAsset(asset: ProductImageAsset) {
    if (!task) return;
    setGenerationConfirmed(false);
    await runMutation("delete:asset", () => productApi.deleteProductAsset(task.id, asset.id), "素材已删除。");
  }

  async function selectReference(role: ProductSourceRole, asset: ProductImageAsset) {
    if (!task || sourceSlot(task, role).selected_asset_id === asset.id) return;
    setGenerationConfirmed(false);
    await runMutation(
      `select:${role}`,
      () => productApi.selectProductReference(task.id, { role, asset_id: asset.id }),
      `已将 ${asset.file_name} 选为${SOURCE_DEFINITIONS.find((item) => item.role === role)?.title || role}参考。`
    );
  }

  async function analyzeOnce() {
    if (!task || task.analysis_used) return;
    if (!analysisConfigId) return setError("请先在 API 设置中新增并启用图文分析 API。");
    if (!analysisConfirmed) return setError("请先确认本次将调用图文分析 API 1 次。");
    const hasSource = task.assets.some((asset) => asset.media_type === "image" || asset.media_type === "frame");
    if (!hasSource) return setError("请至少上传一张照片或一个视频。");
    const next = await runMutation(
      "analyze",
      () => productApi.analyzeProductImages(task.id, { api_config_id: Number(analysisConfigId), confirmed_call_count: 1 }),
      "一次素材分析已提交。系统不会因补充素材再次调用分析 API。"
    );
    if (next) setAnalysisConfirmed(false);
  }

  const missingRoles = useMemo(() => {
    if (!task || !task.analysis_used) return [] as ProductSourceRole[];
    const backendMissing = new Set(task.missing_roles || []);
    return SOURCE_DEFINITIONS
      .filter((item) => backendMissing.has(item.role) || !isSelectedSourceReady(task, item.role))
      .map((item) => item.role);
  }, [task]);

  const remainingGenerationCalls = useMemo(() => {
    if (!task) return 4;
    const reported = task.call_plan?.generation_calls;
    if (typeof reported === "number") return Math.max(0, reported);
    return OUTPUT_DEFINITIONS.filter((item) => item.ai && outputSlot(task, item.role).status !== "success").length;
  }, [task]);

  async function generateRemaining() {
    if (!task || missingRoles.length) return;
    if (!task.analysis_used) return setError("请先完成一次素材分析。");
    if (!generationConfigId) return setError("请选择生图 API。");
    if (!generationConfirmed) return setError(`请先确认接下来最多调用生图 API ${remainingGenerationCalls} 次。`);
    if (task.call_plan?.unknown_retry_warning && !chargeRiskConfirmed) {
      return setError("上一次结果未知且可能已经扣费，请先确认重复调用风险。");
    }
    if (!remainingGenerationCalls) return;
    const resume = task.status === "paused" || task.status === "paused_unknown";
    const payload = {
      api_config_id: Number(generationConfigId),
      confirmed_call_count: remainingGenerationCalls,
      acknowledge_possible_charge: Boolean(task.call_plan?.unknown_retry_warning && chargeRiskConfirmed)
    };
    const next = await runMutation(
      "generate",
      () => resume
        ? productApi.resumeProductImages(task.id, payload)
        : productApi.generateProductImages(task.id, payload),
      "生成任务已开始。四个角度会串行处理，失败或结果未知时将自动暂停。"
    );
    if (next) {
      setGenerationConfirmed(false);
      setChargeRiskConfirmed(false);
    }
  }

  async function confirmRegenerate() {
    if (!task || !regenerateRole || !generationConfigId || !regenerateConfirmed) return;
    const role = regenerateRole;
    const latestCall = latestGenerationCall(task, role);
    const unknownStillRunning = latestCall?.status === "unknown" && !latestCall.finished_at;
    const unknownRetryRisk = latestCall?.status === "unknown" && Boolean(latestCall.finished_at);
    if (unknownStillRunning) return setError("这张图的上一次调用仍在等待晚到结果，暂时不能重复提交。");
    if (unknownRetryRisk && !regenerateChargeRiskConfirmed) return setError("请先确认上一次未知结果可能已经扣费。 ");
    setRegenerateRole(null);
    setRegenerateConfirmed(false);
    setRegenerateChargeRiskConfirmed(false);
    await runMutation(
      `regenerate:${role}`,
      () => productApi.regenerateProductImage(task.id, role, {
        api_config_id: Number(generationConfigId),
        confirmed_call_count: 1,
        acknowledge_possible_charge: unknownRetryRisk
      }),
      "单张重新生成已提交；新结果成功前会保留旧结果。"
    );
  }

  async function saveTransparent(file: File) {
    if (!task) return;
    const next = await runMutation(
      "save:transparent",
      () => productApi.uploadProductTransparent(task.id, file),
      "透明 PNG 修边结果已保存，并同步生成 800×800 版本。"
    );
    if (next) setTransparentEditor(null);
  }

  async function saveLogoCrop(crop: { left: number; top: number; right: number; bottom: number }) {
    if (!task) return;
    const next = await runMutation(
      "save:logo",
      () => productApi.cropProductLogo(task.id, crop),
      "Logo 方形取景已保存，并同步生成高清与 800×800 版本。"
    );
    if (next) setLogoEditorUrl("");
  }

  async function startNextRound() {
    if (!task) return;
    if (!window.confirm("开始下一轮后，本轮上传照片、视频和抽取帧会被删除，历史结果仍会保留。是否继续？")) return;
    const currentId = task.id;
    const result = await runMutation("next", () => productApi.deleteProductSources(currentId));
    if (!result) return;
    window.sessionStorage.removeItem(SESSION_KEY);
    setTask(null);
    setProductCode("");
    setColor("");
    setAnalysisConfirmed(false);
    setGenerationConfirmed(false);
    setChargeRiskConfirmed(false);
    setFrameAssignments({});
    setMessage("本轮源素材已删除，生成结果仍保留在历史记录中。请填写下一款商品。");
  }

  const videos = useMemo(
    () => task?.assets.filter((asset) => asset.media_type === "video") || [],
    [task]
  );
  const frames = useMemo(
    () => task?.assets.filter((asset) => asset.media_type === "frame") || [],
    [task]
  );

  const frontSource = task ? selectedSource(task, "front") : null;
  const logoSource = task ? selectedSource(task, "logo") : null;
  const canGenerate = Boolean(task?.analysis_used && !missingRoles.length && remainingGenerationCalls > 0);
  const anyBusy = Boolean(busyAction) || task?.status === "analyzing" || task?.status === "generating";
  const selectedRegenerateCall = task && regenerateRole ? latestGenerationCall(task, regenerateRole) : null;
  const regenerateUnknownPending = selectedRegenerateCall?.status === "unknown" && !selectedRegenerateCall.finished_at;
  const regenerateUnknownRetryRisk = selectedRegenerateCall?.status === "unknown" && Boolean(selectedRegenerateCall.finished_at);

  if (restoring) {
    return <section className="page product-images-page"><div className="pi-restoring"><LoaderCircle className="spin" size={26} />正在恢复商品图任务…</div></section>;
  }

  return (
    <section className="page product-images-page">
      <header className="page-header pi-page-header">
        <div>
          <h1>生成商品图</h1>
          <p>用同一个实物包的照片和可选视频，制作六张真实、统一的标准商品图；缺少角度时不会凭空生成。</p>
        </div>
        {task && <div className={`pi-task-status status-${task.status}`}><span />{statusLabel(task.status)}</div>}
      </header>

      {(message || error) && <div className={`pi-alert ${error ? "is-error" : "is-success"}`}>
        {error ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
        <span>{error || message}</span>
        <button type="button" onClick={() => { setError(""); setMessage(""); }}><X size={16} /></button>
      </div>}

      <section className="panel pi-identity-panel">
        <div className="pi-section-heading">
          <div><span>01</span><div><h2>建立商品任务</h2><p>一次任务只处理一个款号的一种颜色，防止不同商品被混用。</p></div></div>
          {task && <button type="button" disabled={anyBusy} onClick={startNextRound}><PackagePlus size={17} />开始下一轮</button>}
        </div>
        <div className="pi-identity-fields">
          <label><span>商品款号 <b>*</b></span><input value={productCode} disabled={Boolean(task)} placeholder="例如 E06S1424375BU" onChange={(event) => setProductCode(event.target.value)} /></label>
          <label><span>颜色 <b>*</b></span><input value={color} disabled={Boolean(task)} placeholder="例如 牛仔蓝" onChange={(event) => setColor(event.target.value)} /></label>
          {!task
            ? <button type="button" className="pi-primary" disabled={busyAction === "create"} onClick={createTask}>{busyAction === "create" ? <LoaderCircle className="spin" size={18} /> : <PackagePlus size={18} />}建立任务</button>
            : <div className="pi-task-identity"><CheckCircle2 size={18} /><span><strong>{task.product_code}</strong><small>{task.color}{task.version ? ` · 版本 ${task.version}` : ""}</small></span></div>}
        </div>
      </section>

      {!task && <div className="pi-empty-workspace"><Images size={42} /><strong>先填写款号和颜色</strong><p>建立任务后即可上传照片和视频。</p></div>}

      {task && <>
        <section className="panel pi-sources-panel">
          <div className="pi-section-heading">
            <div><span>02</span><div><h2>上传实拍素材</h2><p>每个入口可传 1–5 张；系统分析后会自动选择最佳参考，你也可以手动更换。</p></div></div>
            <small>JPG / PNG / WebP · 单张不超过 30MB</small>
          </div>
          <div className="pi-source-grid">
            {SOURCE_DEFINITIONS.map((definition) => {
              const slot = sourceSlot(task, definition.role);
              return <SourceUploadCard
                key={definition.role}
                definition={definition}
                slot={slot}
                selectedId={slot.selected_asset_id}
                disabled={anyBusy || task.inputs_deleted}
                busy={busyAction === `upload:${definition.role}`}
                onUpload={(files) => uploadImages(definition.role, files)}
                onSelect={(asset) => selectReference(definition.role, asset)}
                onDelete={deleteAsset}
                onPreview={setPreviewUrl}
              />;
            })}
          </div>

          <article className="pi-video-panel">
            <div className="pi-video-copy">
              <span><Video size={23} /></span>
              <div><strong>补充视频（可选） · 本地抽帧，仅上传候选帧</strong><p>最多 3 个 MP4/MOV，每个不超过 60 秒、500MB。浏览器逐帧压缩后最多上传 12 张候选帧，不上传原视频；缺失角度由你手动指定。</p></div>
            </div>
            <label className={`pi-video-upload ${anyBusy ? "is-disabled" : ""}`}>
              {busyAction.startsWith("upload:video") ? <LoaderCircle className="spin" size={18} /> : <UploadCloud size={18} />}
              {busyAction.startsWith("upload:video") ? (videoProgress || "正在本地抽帧") : "选择视频"}
              <input type="file" accept=".mp4,.mov,video/mp4,video/quicktime" multiple disabled={anyBusy || videos.length >= MAX_VIDEO_COUNT} onChange={(event) => { uploadVideos(Array.from(event.target.files || [])); event.currentTarget.value = ""; }} />
            </label>
            {videos.length > 0 && <div className="pi-video-list">{videos.map((videoItem) => <div key={videoItem.id}>
              <FileVideo2 size={20} />
              <span><strong>{videoItem.file_name}</strong><small>{videoItem.duration_seconds?.toFixed(1)} 秒 {formatBytes(videoItem.file_size)}</small></span>
              <button type="button" disabled={anyBusy} onClick={() => deleteAsset(videoItem)}><Trash2 size={15} />删除</button>
            </div>)}</div>}
          </article>

          {frames.length > 0 && <section className="pi-frame-picker">
            <header><div><h3>视频候选帧</h3><p>需要用视频补充角度时，先选择用途，再把清晰的一帧设为参考。</p></div><small>{frames.length} 帧</small></header>
            <div className="pi-frame-strip">{frames.map((frame) => {
              const assigned = SOURCE_DEFINITIONS.find((definition) => sourceSlot(task, definition.role).selected_asset_id === frame.id)?.role;
              const role = frameAssignments[frame.id] || assigned || missingRoles[0] || "front";
              return <article key={frame.id} className={assigned ? "is-assigned" : ""}>
                <button type="button" className="pi-frame-preview" onClick={() => setPreviewUrl(frame.file_url)}><img src={frame.file_url} alt={`视频帧 ${frame.frame_time_seconds || 0}`} /><Eye size={15} /></button>
                <small>{typeof frame.frame_time_seconds === "number" ? `${frame.frame_time_seconds.toFixed(1)}s` : frame.file_name}</small>
                <select value={role} onChange={(event) => setFrameAssignments((current) => ({ ...current, [frame.id]: event.target.value as ProductSourceRole }))}>
                  {SOURCE_DEFINITIONS.map((item) => <option value={item.role} key={item.role}>{item.title.replace("照片", "")}</option>)}
                </select>
                <button type="button" disabled={anyBusy || assigned === role} onClick={() => selectReference(role, frame)}>{assigned === role ? <><Check size={14} />已分配</> : "设为参考"}</button>
              </article>;
            })}</div>
          </section>}
        </section>

        <section className="panel pi-analysis-panel">
          <div className="pi-section-heading">
            <div><span>03</span><div><h2>分析并检查参考素材</h2><p>图文分析只允许调用一次，用于挑选最佳照片或视频帧并指出缺失角度。</p></div></div>
            {task.analysis_used && <span className="pi-used-call"><CheckCircle2 size={16} />分析调用已使用 · 1 次</span>}
          </div>

          {!task.analysis_used ? <div className="pi-call-confirmation">
            <label><span>图文分析 API</span><select value={analysisConfigId} disabled={anyBusy} onChange={(event) => { setAnalysisConfigId(Number(event.target.value) || ""); setAnalysisConfirmed(false); }}>
              {!analysisConfigs.length && <option value="">暂无可用图文分析 API</option>}
              {analysisConfigs.map((config) => <option value={config.id} key={config.id}>{config.config_name}{config.model_name ? ` / ${config.model_name}` : ""}{config.is_default ? "（默认）" : ""}</option>)}
            </select></label>
            <div className="pi-call-count"><strong>本步骤调用 API 1 次</strong><small>分析后补照片或手选视频帧，不会再次分析或扣除此项费用。</small></div>
            <label className="pi-check-line"><input type="checkbox" checked={analysisConfirmed} onChange={(event) => setAnalysisConfirmed(event.target.checked)} />我已确认素材属于同一个实物包，并确认调用图文分析 API 1 次</label>
            <button type="button" className="pi-primary" disabled={anyBusy || !analysisConfirmed || !analysisConfigId} onClick={analyzeOnce}>
              {busyAction === "analyze" || task.status === "analyzing" ? <LoaderCircle className="spin" size={18} /> : <Wand2 size={18} />}
              {task.status === "analyzing" ? "正在分析" : "确认并分析一次"}
            </button>
          </div> : <>
            {missingRoles.length > 0 ? <div className="pi-missing-panel">
              <AlertTriangle size={22} />
              <div><strong>还不能开始生成，请补全 {missingRoles.length} 个角度</strong><p>缺少必要实拍依据时，整组六张不会调用生图 API。请上传对应照片，或在上方手动分配清晰的视频帧。</p>
                <div>{missingRoles.map((role) => <span key={role}>{SOURCE_DEFINITIONS.find((item) => item.role === role)?.title}</span>)}</div>
              </div>
            </div> : <div className="pi-ready-panel"><CheckCircle2 size={22} /><div><strong>五类真实参考已齐全</strong><p>可以检查下方调用次数并开始生成；每张图只使用自己的对应角度参考。</p></div></div>}

            <div className="pi-reference-summary">{SOURCE_DEFINITIONS.map((definition) => {
              const asset = selectedSource(task, definition.role);
              return <article key={definition.role} className={asset ? "is-ready" : ""}>
                {asset ? <button type="button" onClick={() => setPreviewUrl(asset.file_url)}><img src={asset.file_url} alt={definition.title} /></button> : <div><ImageIcon size={25} /></div>}
                <span><strong>{definition.title.replace("照片", "")}</strong><small>{asset ? asset.file_name : "缺少参考"}</small></span>
              </article>;
            })}</div>
          </>}
        </section>

        {task.analysis_used && <section className="panel pi-generation-panel">
          <div className="pi-section-heading">
            <div><span>04</span><div><h2>确认费用并生成</h2><p>正面主图、背面、半侧面和顶部开口按顺序生成；透明图与 Logo 图使用真实素材本地处理。</p></div></div>
          </div>
          <div className="pi-generation-controls">
            <label><span>生图 API</span><select value={generationConfigId} disabled={anyBusy} onChange={(event) => { setGenerationConfigId(Number(event.target.value) || ""); setGenerationConfirmed(false); setChargeRiskConfirmed(false); }}>
              {!generationConfigs.length && <option value="">暂无可用生图 API</option>}
              {generationConfigs.map((config) => <option value={config.id} key={config.id}>{config.config_name}{config.model_name ? ` / ${config.model_name}` : ""}{config.is_default ? "（默认）" : ""}</option>)}
            </select></label>
            <div className="pi-generation-count"><strong>接下来最多调用 {remainingGenerationCalls} 次</strong><small>已完成的角度会跳过；失败或结果未知时立即暂停，不会自动重试。</small></div>
            <label className="pi-check-line"><input type="checkbox" checked={generationConfirmed} disabled={!canGenerate || anyBusy} onChange={(event) => setGenerationConfirmed(event.target.checked)} />我已检查五张参考素材，并确认本轮最多调用生图 API {remainingGenerationCalls} 次</label>
            {task.call_plan?.unknown_retry_warning && <label className="pi-check-line pi-risk-line"><input type="checkbox" checked={chargeRiskConfirmed} disabled={Boolean(task.call_plan.unknown_request_still_running)} onChange={(event) => setChargeRiskConfirmed(event.target.checked)} />我知道上一次结果未知且可能已经扣费，仍确认继续调用剩余 API</label>}
            <button type="button" className="pi-primary" disabled={!canGenerate || anyBusy || !generationConfirmed || !generationConfigId || Boolean(task.call_plan?.unknown_request_still_running) || Boolean(task.call_plan?.unknown_retry_warning && !chargeRiskConfirmed)} onClick={generateRemaining}>
              {busyAction === "generate" || task.status === "generating" ? <LoaderCircle className="spin" size={18} /> : task.status === "paused" || task.status === "paused_unknown" ? <RefreshCw size={18} /> : <Wand2 size={18} />}
              {task.status === "generating" ? "正在串行生成" : task.status === "paused" || task.status === "paused_unknown" ? "从暂停处继续" : "确认并开始生成"}
            </button>
          </div>
          {hasLateUnknown && <div className="pi-unknown-warning"><AlertTriangle size={17} /><span>有一张结果暂时未知，可能已经扣费。系统会继续查询晚到结果，不会自动重新提交。</span></div>}
        </section>}

        {task.analysis_used && <section className="panel pi-results-panel">
          <div className="pi-section-heading">
            <div><span>05</span><div><h2>六张商品图</h2><p>预览后可分别下载高清和 800×800 版本；不满意的 API 图片可单张重新生成。</p></div></div>
            {task.zip_url && <a className="pi-download-zip" href={task.zip_url} download><Download size={17} />下载全部 ZIP</a>}
          </div>
          <div className="pi-output-grid">{OUTPUT_DEFINITIONS.map((definition) => {
            const output = outputSlot(task, definition.role);
            const result = outputResult(output);
            const errorText = outputError(output);
            const attempts = task.calls.filter((call) => call.call_type === "generation" && call.slot === definition.role);
            const latestAttempt = latestGenerationCall(task, definition.role);
            const unknownAttemptPending = latestAttempt?.status === "unknown" && !latestAttempt.finished_at;
            const canRegenerate = definition.ai && output.status !== "running" && !unknownAttemptPending && !anyBusy && !task.inputs_deleted && Boolean(generationConfigId);
            return <article className={`pi-output-card status-${output.status}`} key={definition.role}>
              <header><div><strong>{definition.title}</strong><small>{definition.fileName}</small></div><span>{statusLabel(output.status)}</span></header>
              <div className={`pi-output-preview ${definition.role === "front_transparent" ? "is-transparent" : ""}`}>
                {result
                  ? <button type="button" onClick={() => setPreviewUrl(result.original_url)}><img src={result.size_800_url || result.original_url} alt={definition.title} /></button>
                  : output.status === "running"
                    ? <div><LoaderCircle className="spin" size={28} /><span>正在处理</span></div>
                    : <div><ImageIcon size={30} /><span>{missingRoles.includes(output.reference_role) ? "缺少真实素材" : "等待生成"}</span></div>}
              </div>
              {errorText && <p className="pi-output-error">{errorText}</p>}
              <div className="pi-output-actions">
                {result && <>
                  <a href={result.original_url} download={definition.fileName}><Download size={15} />高清</a>
                  <a href={result.size_800_url} download={definition.fileName}><Download size={15} />800</a>
                </>}
                {definition.role === "front_transparent" && frontSource && <button type="button" disabled={anyBusy} onClick={() => setTransparentEditor({ sourceUrl: frontSource.file_url, imageUrl: result?.original_url })}><Eraser size={15} />{result ? "修边" : "手工抠图"}</button>}
                {definition.role === "logo_detail" && logoSource && <button type="button" disabled={anyBusy} onClick={() => setLogoEditorUrl(logoSource.file_url)}><Crop size={15} />裁剪</button>}
                {definition.ai && <button type="button" disabled={!canRegenerate} title={unknownAttemptPending ? "上一次调用仍在等待晚到结果" : undefined} onClick={() => { setRegenerateRole(definition.role); setRegenerateConfirmed(false); setRegenerateChargeRiskConfirmed(false); }}><RefreshCw size={15} />重新生成</button>}
              </div>
              {attempts.length > 1 && <details className="pi-attempts"><summary>查看 {attempts.length} 次调用记录</summary>{attempts.map((attempt, index) => <div key={attempt.id}><span>第 {attempt.attempt_no || index + 1} 次 · {statusLabel(attempt.status)}</span><small>{attempt.api_config_name || attempt.config_name || ""}</small></div>)}</details>}
            </article>;
          })}</div>
          {task.status === "completed" && <div className="pi-complete-banner"><CheckCircle2 size={22} /><div><strong>{task.product_code} · {task.color} 已完成</strong><p>请人工检查包型、颜色、Logo、五金和配件。需要处理下一款时点击“开始下一轮”。</p></div></div>}
        </section>}
      </>}

      {previewUrl && <div className="pi-lightbox" role="dialog" aria-modal="true" onClick={() => setPreviewUrl("")}><button type="button" onClick={() => setPreviewUrl("")}><X size={20} />关闭</button><img src={previewUrl} alt="图片预览" onClick={(event) => event.stopPropagation()} /></div>}

      {transparentEditor && <TransparentEditor sourceUrl={transparentEditor.sourceUrl} imageUrl={transparentEditor.imageUrl} busy={busyAction === "save:transparent"} onClose={() => setTransparentEditor(null)} onSave={saveTransparent} />}
      {logoEditorUrl && <LogoCropEditor imageUrl={logoEditorUrl} busy={busyAction === "save:logo"} onClose={() => setLogoEditorUrl("")} onSave={saveLogoCrop} />}

      {regenerateRole && <div className="pi-modal" role="dialog" aria-modal="true" aria-label="确认单张重新生成">
        <section className="pi-confirm-dialog">
          <header><div><h2>确认重新生成</h2><p>{OUTPUT_DEFINITIONS.find((item) => item.role === regenerateRole)?.title}</p></div><button type="button" className="pi-icon-button" onClick={() => setRegenerateRole(null)}><X size={20} /></button></header>
          <div className="pi-confirm-call"><RefreshCw size={24} /><div><strong>本次将调用生图 API 1 次</strong><p>旧结果会保留到新结果成功，不会覆盖历史任务。</p></div></div>
          {regenerateUnknownPending && <div className="pi-unknown-warning"><AlertTriangle size={17} /><span>上一次调用结果未知且仍可能晚到，现在不能重复提交。</span></div>}
          {regenerateUnknownRetryRisk && <label className="pi-check-line pi-risk-line"><input type="checkbox" checked={regenerateChargeRiskConfirmed} onChange={(event) => setRegenerateChargeRiskConfirmed(event.target.checked)} />我知道上一次结果未知且可能已经扣费，仍确认重新调用</label>}
          <label className="pi-check-line"><input type="checkbox" checked={regenerateConfirmed} disabled={regenerateUnknownPending} onChange={(event) => setRegenerateConfirmed(event.target.checked)} />我确认额外调用生图 API 1 次</label>
          <footer><button type="button" onClick={() => setRegenerateRole(null)}>取消</button><button type="button" className="pi-primary" disabled={!regenerateConfirmed || regenerateUnknownPending || Boolean(regenerateUnknownRetryRisk && !regenerateChargeRiskConfirmed)} onClick={confirmRegenerate}><Check size={17} />确认重新生成</button></footer>
        </section>
      </div>}
    </section>
  );
}
