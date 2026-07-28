import { ClipboardPaste, Download, Eye, FileImage, LoaderCircle, RefreshCw, UploadCloud, X } from "lucide-react";
import type { ClipboardEvent, DragEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

const BROWSER_IMAGE_MAX_EDGE = 2400;
const BROWSER_JPEG_QUALITY = 0.88;
const BROWSER_PHOTO = /\.(?:jpe?g|webp)$/i;
const SUPPORTED_IMAGE_NAME = /\.(?:jpe?g|png|webp)$/i;

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
  const pasteButtonRef = useRef<HTMLButtonElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [prepared, setPrepared] = useState<PreparedCutout | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (active) void api.prewarmHeavyTask("cutout").catch(() => undefined);
  }, [active]);

  useEffect(() => {
    if (!preview) return;
    const closePreview = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreview(null);
    };
    window.addEventListener("keydown", closePreview);
    return () => window.removeEventListener("keydown", closePreview);
  }, [preview]);

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
      setMessage("抠图已完成，可以先用灰底图检查边缘，再下载或用于自动化整理。");
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

  function handlePaste(event: ClipboardEvent<HTMLElement>) {
    const images = Array.from(event.clipboardData.items).flatMap((item, index) => {
      if (item.kind !== "file" || !item.type.startsWith("image/")) return [];
      const file = item.getAsFile();
      if (!file) return [];
      return [SUPPORTED_IMAGE_NAME.test(file.name) ? file : namedClipboardFile(file, index)];
    });
    if (!images.length || busy) return;
    event.preventDefault();
    void prepareCutout(images);
  }

  function namedClipboardFile(blob: Blob, index = 0) {
    const extension = blob.type === "image/jpeg"
      ? "jpg"
      : blob.type === "image/webp"
        ? "webp"
        : "png";
    return new File([blob], `粘贴图片-${Date.now()}-${index + 1}.${extension}`, { type: blob.type });
  }

  async function pasteFromClipboard() {
    if (busy) return;
    pasteButtonRef.current?.focus();
    if (!navigator.clipboard?.read) {
      setMessage("请按 Ctrl+V 粘贴图片");
      return;
    }
    try {
      const items = await navigator.clipboard.read();
      const blobs = await Promise.all(items.flatMap((item) => {
        const imageType = item.types.find((type) => type.startsWith("image/"));
        return imageType ? [item.getType(imageType)] : [];
      }));
      if (!blobs.length) {
        setMessage("剪贴板中没有图片");
        return;
      }
      await prepareCutout([namedClipboardFile(blobs[0])]);
    } catch {
      setMessage("请按 Ctrl+V 粘贴图片");
      pasteButtonRef.current?.focus();
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
      setMessage("透明图已带入“自动化整理”的商品原图。");
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
        <p>白底商品图生成透明 PNG；仅进入本功能时预热抠图进程，空闲 3 分钟后自动退出。</p>
      </header>
      <section
        className={`panel organizer-preparation-panel${dragging ? " is-dragging" : ""}`}
        tabIndex={0}
        aria-label="正面主图抠图上传区"
        onPaste={handlePaste}
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
          <div><h2>上传白底正面主图</h2><p>可拖入、点击选择，或点击此区域后按 Ctrl+V 粘贴；一次处理一张。</p></div>
          <div className="button-row organizer-cutout-upload-actions">
            <button
              ref={pasteButtonRef}
              type="button"
              disabled={busy}
              onClick={() => void pasteFromClipboard()}
            >
              <ClipboardPaste size={18} />粘贴图片
            </button>
            <button
              type="button"
              disabled={busy || !sourceFile}
              onClick={() => sourceFile && void prepareCutout([sourceFile])}
            >
              <RefreshCw size={18} />重新抠图
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
            <button className="organizer-cutout-view" type="button" title="查看透明 PNG 大图" aria-label="放大查看透明 PNG" onClick={() => setPreview(prepared.transparent_url)}><Eye size={18} /></button>
            <figcaption>透明 PNG</figcaption>
          </figure>
          <figure>
            <img src={prepared.gray_preview_url} alt="灰底边缘检查图" />
            <button className="organizer-cutout-view" type="button" title="查看灰底边缘检查大图" aria-label="放大查看灰底边缘检查图" onClick={() => setPreview(prepared.gray_preview_url)}><Eye size={18} /></button>
            <figcaption>灰底边缘检查</figcaption>
          </figure>
          <div className="organizer-cutout-actions">
            <a className="button-link" href={prepared.download_url} download={prepared.file_name}><Download size={18} />导出透明 PNG</a>
            <button type="button" className="primary" disabled={busy} onClick={() => void useAsOrganizerSource()}><FileImage size={18} />用于功能 1 的商品原图</button>
          </div>
        </div> : <div className="organizer-preparation-empty"><FileImage size={28} /><span>尚未生成透明图。</span></div>}
      </section>
      {message && <div className="alert warning">{message}</div>}
      {preview && <div className="image-modal" role="dialog" aria-modal="true" aria-label="放大图片预览" onClick={() => setPreview(null)}>
        <button className="image-modal-close" type="button" onClick={() => setPreview(null)} aria-label="关闭预览"><X size={22} /></button>
        <img src={preview} alt="图片预览" onClick={(event) => event.stopPropagation()} />
      </div>}
    </section>
  );
}
