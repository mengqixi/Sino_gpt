import { Download, Eye, FileImage, LoaderCircle, RefreshCw, RotateCcw, UploadCloud, X, ZoomIn, ZoomOut } from "lucide-react";
import type { DragEvent, PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

const BROWSER_IMAGE_MAX_EDGE = 2400;
const BROWSER_JPEG_QUALITY = 0.88;
const BROWSER_PHOTO = /\.(?:jpe?g|webp)$/i;
const SUPPORTED_IMAGE_NAME = /\.(?:jpe?g|png|webp)$/i;
const PREVIEW_ZOOM_MIN = 0.5;
const PREVIEW_ZOOM_MAX = 5;
const PREVIEW_ZOOM_STEP = 0.25;

type PreparedCutout = {
  prepared_id: string;
  transparent_url: string;
  gray_preview_url: string;
  download_url: string;
  file_name: string;
};

type Props = {
  active: boolean;
  onUseAsOrganizerSource: (file: File) => void;
};

function canvasJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
}

async function prepareCutoutPhoto(file: File): Promise<File> {
  if (!BROWSER_PHOTO.test(file.name) || typeof window.createImageBitmap !== "function") return file;
  let bitmap: ImageBitmap | null = null;
  try {
    bitmap = await window.createImageBitmap(file, { imageOrientation: "from-image" });
    const longest = Math.max(bitmap.width, bitmap.height);
    if (longest <= BROWSER_IMAGE_MAX_EDGE && file.size <= 6 * 1024 * 1024) return file;
    const scale = Math.min(1, BROWSER_IMAGE_MAX_EDGE / longest);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) return file;
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await canvasJpeg(canvas, BROWSER_JPEG_QUALITY);
    canvas.width = 1;
    canvas.height = 1;
    if (!blob || blob.size >= file.size) return file;
    const stem = file.name.replace(/\.[^.]+$/, "").slice(0, 120) || "image";
    return new File([blob], `${stem}.jpg`, { type: "image/jpeg", lastModified: file.lastModified });
  } catch {
    return file;
  } finally {
    bitmap?.close();
  }
}

export default function ProductCutout({ active, onUseAsOrganizerSource }: Props) {
  const sessionRef = useRef("");
  const sessionPromiseRef = useRef<Promise<{ session_id: string }> | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [prepared, setPrepared] = useState<PreparedCutout | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewZoom, setPreviewZoom] = useState(1);
  const [previewBaseSize, setPreviewBaseSize] = useState<{ width: number; height: number } | null>(null);
  const [previewPan, setPreviewPan] = useState({ x: 0, y: 0 });
  const [previewDragging, setPreviewDragging] = useState(false);
  const previewDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (active) void api.prewarmHeavyTask("cutout").catch(() => undefined);
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const pasteScreenshot = (event: globalThis.ClipboardEvent) => {
      if (busy || !event.clipboardData) return;
      const images = Array.from(event.clipboardData.items).flatMap((item, index) => {
        if (item.kind !== "file" || !item.type.startsWith("image/")) return [];
        const file = item.getAsFile();
        if (!file) return [];
        return [SUPPORTED_IMAGE_NAME.test(file.name) ? file : namedClipboardFile(file, index)];
      });
      if (!images.length) return;
      event.preventDefault();
      void prepareCutout(images);
    };
    window.addEventListener("paste", pasteScreenshot);
    return () => window.removeEventListener("paste", pasteScreenshot);
  }, [active, busy]);

  useEffect(() => {
    if (!preview) return;
    const closePreview = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeImagePreview();
      if (event.key === "+" || event.key === "=") adjustPreviewZoom(PREVIEW_ZOOM_STEP);
      if (event.key === "-") adjustPreviewZoom(-PREVIEW_ZOOM_STEP);
      if (event.key === "0") resetImagePreview();
    };
    window.addEventListener("keydown", closePreview);
    return () => window.removeEventListener("keydown", closePreview);
  }, [preview]);

  function clampPreviewZoom(value: number) {
    return Math.min(PREVIEW_ZOOM_MAX, Math.max(PREVIEW_ZOOM_MIN, value));
  }

  function adjustPreviewZoom(delta: number) {
    setPreviewZoom((current) => {
      const next = clampPreviewZoom(Number((current + delta).toFixed(2)));
      const ratio = next / current;
      setPreviewPan((pan) => ({ x: pan.x * ratio, y: pan.y * ratio }));
      return next;
    });
  }

  function resetImagePreview() {
    setPreviewZoom(1);
    setPreviewPan({ x: 0, y: 0 });
  }

  function zoomImagePreviewAtPointer(event: ReactWheelEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const anchorX = event.clientX - (rect.left + rect.width / 2);
    const anchorY = event.clientY - (rect.top + rect.height / 2);
    const delta = event.deltaY < 0 ? PREVIEW_ZOOM_STEP : -PREVIEW_ZOOM_STEP;
    setPreviewZoom((current) => {
      const next = clampPreviewZoom(Number((current + delta).toFixed(2)));
      if (next === current) return current;
      const ratio = next / current;
      setPreviewPan((pan) => ({
        x: anchorX - (anchorX - pan.x) * ratio,
        y: anchorY - (anchorY - pan.y) * ratio
      }));
      return next;
    });
  }

  function startImagePreviewDrag(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    previewDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: previewPan.x,
      originY: previewPan.y
    };
    setPreviewDragging(true);
  }

  function moveImagePreview(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = previewDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.preventDefault();
    setPreviewPan({
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY
    });
  }

  function finishImagePreviewDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = previewDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    previewDragRef.current = null;
    setPreviewDragging(false);
  }

  function openImagePreview(url: string) {
    resetImagePreview();
    setPreviewBaseSize(null);
    setPreview(url);
  }

  function closeImagePreview() {
    setPreview(null);
    resetImagePreview();
    setPreviewBaseSize(null);
    previewDragRef.current = null;
    setPreviewDragging(false);
  }

  function measurePreviewImage(image: HTMLImageElement) {
    const availableWidth = Math.min(window.innerWidth * 0.92, 1600);
    const availableHeight = Math.max(240, window.innerHeight - 160);
    const fitScale = Math.min(
      availableWidth / Math.max(1, image.naturalWidth),
      availableHeight / Math.max(1, image.naturalHeight),
      1
    );
    setPreviewBaseSize({
      width: Math.max(1, Math.round(image.naturalWidth * fitScale)),
      height: Math.max(1, Math.round(image.naturalHeight * fitScale))
    });
  }

  async function ensureSession() {
    if (sessionRef.current) return sessionRef.current;
    if (!sessionPromiseRef.current) sessionPromiseRef.current = api.startVipOrganizerSession();
    const result = await sessionPromiseRef.current;
    sessionRef.current = result.session_id;
    return result.session_id;
  }

  async function prepareCutout(files: FileList | File[] | null) {
    const file = files?.[0];
    if (!file) return;
    if (!SUPPORTED_IMAGE_NAME.test(file.name)) {
      setMessage("请上传 JPG、PNG 或 WebP 图片");
      return;
    }
    setSourceFile(file);
    setBusy(true);
    setMessage("正在生成透明图和灰底检查图……");
    try {
      const [sessionId, preparedFile] = await Promise.all([ensureSession(), prepareCutoutPhoto(file)]);
      const result = await api.prepareVipOrganizerCutout(sessionId, preparedFile);
      setPrepared(result);
      setMessage("抠图已完成，可以先用灰底图检查边缘，再下载或用于自动化整理");
    } catch (error: any) {
      setMessage(error.message || "抠图失败");
    } finally {
      setBusy(false);
    }
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    setDragging(false);
    if (!busy) void prepareCutout(event.dataTransfer.files);
  }

  function namedClipboardFile(blob: Blob, index = 0) {
    const extension = blob.type === "image/jpeg"
      ? "jpg"
      : blob.type === "image/webp"
        ? "webp"
        : "png";
    return new File([blob], `粘贴图片-${Date.now()}-${index + 1}.${extension}`, { type: blob.type });
  }

  function resetCutout() {
    const cutoutSessionId = sessionRef.current;
    sessionRef.current = "";
    sessionPromiseRef.current = null;
    setSourceFile(null);
    setPrepared(null);
    closeImagePreview();
    setMessage("");
    setDragging(false);
    if (cutoutSessionId) {
      void api.cleanupVipOrganizerSession(cutoutSessionId).catch(() => undefined);
    }
  }

  async function useAsOrganizerSource() {
    if (!prepared) return;
    setBusy(true);
    try {
      const response = await fetch(prepared.transparent_url, { cache: "no-store" });
      if (!response.ok) throw new Error("透明图读取失败");
      const blob = await response.blob();
      onUseAsOrganizerSource(new File([blob], prepared.file_name, { type: "image/png" }));
      setMessage("透明图已带入“自动化整理”的商品原图");
    } catch (error: any) {
      setMessage(error.message || "透明图读取失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page organizer-page">
      <header className="page-header">
        <h1>透明图抠图</h1>
        <p>白底商品图生成透明 PNG；仅进入本功能时预热抠图进程，空闲 3 分钟后自动退出</p>
      </header>
      <section
        className={`panel organizer-preparation-panel${dragging ? " is-dragging" : ""}`}
        tabIndex={0}
        aria-label="正面主图抠图上传区"
        onDragEnter={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? "none" : "copy";
          if (!busy) setDragging(true);
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={handleDrop}
      >
        <div className="section-title-row">
          <div><h2>上传白底正面主图</h2><p>拖入或选择图片；复制截图后按 Ctrl+V 可直接抠图</p></div>
          <div className="button-row organizer-cutout-upload-actions">
            <button
              type="button"
              disabled={busy || (!sourceFile && !prepared)}
              onClick={resetCutout}
            >
              <RefreshCw size={18} />重新开始
            </button>
            <label className="organizer-prepare-upload">
              {busy ? <LoaderCircle className="spin" size={18} /> : <UploadCloud size={18} />}
              {busy ? "正在精细抠图" : dragging ? "松开即可抠图" : "选择正面主图"}
              <input type="file" accept="image/*" disabled={busy} onChange={(event) => {
                void prepareCutout(event.target.files);
                event.currentTarget.value = "";
              }} />
            </label>
          </div>
        </div>
        {prepared ? <div className="organizer-cutout-results">
          <figure className="transparent-checker">
            <img src={prepared.transparent_url} alt="透明 PNG 结果" />
            <button className="organizer-cutout-view" type="button" title="查看透明 PNG 大图" aria-label="放大查看透明 PNG" onClick={() => openImagePreview(prepared.transparent_url)}><Eye size={18} /></button>
            <figcaption>透明 PNG</figcaption>
          </figure>
          <figure>
            <img src={prepared.gray_preview_url} alt="灰底边缘检查图" />
            <button className="organizer-cutout-view" type="button" title="查看灰底边缘检查大图" aria-label="放大查看灰底边缘检查图" onClick={() => openImagePreview(prepared.gray_preview_url)}><Eye size={18} /></button>
            <figcaption>灰底边缘检查</figcaption>
          </figure>
          <div className="organizer-cutout-actions">
            <a className="button-link" href={prepared.download_url} download={prepared.file_name}><Download size={18} />导出透明 PNG</a>
            <button type="button" className="primary" disabled={busy} onClick={() => void useAsOrganizerSource()}><FileImage size={18} />用于功能 1 的商品原图</button>
          </div>
        </div> : <div className="organizer-preparation-empty"><FileImage size={28} /><span>尚未生成透明图</span></div>}
      </section>
      {message && <div className="alert warning">{message}</div>}
      {preview && <div className="image-modal image-modal-zoomable" role="dialog" aria-modal="true" aria-label="放大图片预览" onClick={closeImagePreview}>
        <button className="image-modal-close" type="button" onClick={closeImagePreview} aria-label="关闭预览"><X size={22} /></button>
        <div
          className={`image-modal-viewport${previewDragging ? " is-dragging" : ""}`}
          onClick={(event) => event.stopPropagation()}
          onWheel={zoomImagePreviewAtPointer}
          onPointerDown={startImagePreviewDrag}
          onPointerMove={moveImagePreview}
          onPointerUp={finishImagePreviewDrag}
          onPointerCancel={finishImagePreviewDrag}
          onDoubleClick={resetImagePreview}
          title="滚轮缩放，按住左键拖动查看，双击复位"
        >
          <div className="image-modal-canvas">
            <img
              src={preview}
              alt="图片预览"
              draggable={false}
              onLoad={(event) => measurePreviewImage(event.currentTarget)}
              style={previewBaseSize ? {
                width: `${previewBaseSize.width}px`,
                height: `${previewBaseSize.height}px`,
                transform: `translate3d(${previewPan.x}px, ${previewPan.y}px, 0) scale(${previewZoom})`
              } : undefined}
            />
          </div>
        </div>
        <div className="image-modal-zoom-controls" role="group" aria-label="图片缩放" onClick={(event) => event.stopPropagation()}>
          <button type="button" disabled={previewZoom <= PREVIEW_ZOOM_MIN} onClick={(event) => { event.stopPropagation(); adjustPreviewZoom(-PREVIEW_ZOOM_STEP); }} aria-label="缩小图片"><ZoomOut size={20} /></button>
          <output aria-live="polite">{Math.round(previewZoom * 100)}%</output>
          <button type="button" disabled={previewZoom >= PREVIEW_ZOOM_MAX} onClick={(event) => { event.stopPropagation(); adjustPreviewZoom(PREVIEW_ZOOM_STEP); }} aria-label="放大图片"><ZoomIn size={20} /></button>
          <button type="button" onClick={(event) => { event.stopPropagation(); resetImagePreview(); }} aria-label="恢复原始缩放" title="恢复 100%"><RotateCcw size={18} /></button>
        </div>
      </div>}
    </section>
  );
}
