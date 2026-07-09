import { Download, Eraser, Eye, Image as ImageIcon, Pipette, RotateCcw, Save, Shield, UploadCloud } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type UploadedImage = {
  image_id: number;
  file_name: string;
  preview_url: string;
};

type Props = {
  onUseAsSource: (image: UploadedImage) => void;
};

const palette = ["#111111", "#f5f2ea", "#b52126", "#8a1f1d", "#2f4d3c", "#5f4635", "#d9c7a3", "#6f7d8f"];

export default function RecolorPanel({ onUseAsSource }: Props) {
  const [uploaded, setUploaded] = useState<UploadedImage | null>(null);
  const [targetColor, setTargetColor] = useState("#b52126");
  const [subjectMask, setSubjectMask] = useState("");
  const [protectMask, setProtectMask] = useState("");
  const [previewImage, setPreviewImage] = useState("");
  const [showOriginal, setShowOriginal] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [mode, setMode] = useState<"protect" | "erase">("protect");
  const [brushSize, setBrushSize] = useState(6);
  const [zoom, setZoom] = useState(1);
  const [busy, setBusy] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [message, setMessage] = useState("");
  const imageRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef(false);
  const previewTimerRef = useRef<number | null>(null);

  useEffect(() => {
    drawProtectMask();
  }, [uploaded, protectMask]);

  useEffect(() => {
    if (!uploaded || !subjectMask || !protectMask || !/^#[0-9a-fA-F]{6}$/.test(targetColor)) return;
    if (previewTimerRef.current) {
      window.clearTimeout(previewTimerRef.current);
    }
    previewTimerRef.current = window.setTimeout(() => {
      refreshPreview();
    }, 350);
    return () => {
      if (previewTimerRef.current) {
        window.clearTimeout(previewTimerRef.current);
      }
    };
  }, [targetColor, uploaded?.image_id]);

  async function upload(file?: File) {
    if (!file) return;
    try {
      const row = await api.uploadImage(file);
      setUploaded(row);
      setSubjectMask("");
      setProtectMask("");
      setPreviewImage("");
      setShowOriginal(true);
      setResult(null);
      setMessage("");
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  function explainError(error: any) {
    const text = String(error?.message || error || "");
    if (text.includes("Method Not Allowed")) {
      return "调色接口没有加载成功，请重启后端服务后再试。";
    }
    return text || "操作失败";
  }

  async function analyzeMasks() {
    if (!uploaded) {
      throw new Error("请先上传一张女包原图");
    }
    const data = await api.analyzeRecolor({ uploaded_image_id: uploaded.image_id });
    setSubjectMask(data.subject_mask);
    setProtectMask(data.protect_mask);
    setMessage(`已自动识别主体和五金候选区：${data.segmentation_backend}`);
    return data;
  }

  async function ensureMasks() {
    if (subjectMask && protectMask) {
      return {
        subject_mask: subjectMask,
        protect_mask: canvasRef.current?.toDataURL("image/png") || protectMask
      };
    }
    throw new Error("请先点击“自动识别”，确认五金保护区后再调色。");
  }

  async function analyze() {
    setBusy(true);
    try {
      await analyzeMasks();
      setPreviewImage("");
      setShowOriginal(false);
      setMessage("已识别保护区。蓝色区域会保持原色；确认后选择颜色开始预览。");
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setBusy(false);
    }
  }

  async function refreshPreview() {
    if (!uploaded) {
      return;
    }
    setPreviewBusy(true);
    try {
      const masks = await ensureMasks();
      const data = await api.previewRecolor({
        uploaded_image_id: uploaded.image_id,
        target_color: targetColor,
        subject_mask: masks.subject_mask,
        protect_mask: masks.protect_mask
      });
      setPreviewImage(data.preview_image);
      setShowOriginal(false);
      setMessage("已生成调色预览，满意后点击保存结果。");
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function saveResult() {
    if (!uploaded) {
      setMessage("请先上传一张女包原图");
      return;
    }
    setBusy(true);
    try {
      const masks = await ensureMasks();
      const data = await api.applyRecolor({
        uploaded_image_id: uploaded.image_id,
        target_color: targetColor,
        subject_mask: masks.subject_mask,
        protect_mask: masks.protect_mask
      });
      setResult(data);
      setMessage("调色结果已保存到历史记录，可下载或选为 AI 生成源图");
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setBusy(false);
    }
  }

  function drawProtectMask() {
    const canvas = canvasRef.current;
    const image = imageRef.current;
    if (!canvas || !image) return;
    canvas.width = image.naturalWidth || image.clientWidth;
    canvas.height = image.naturalHeight || image.clientHeight;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    if (!protectMask) return;
    const mask = new Image();
    mask.onload = () => {
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(mask, 0, 0, canvas.width, canvas.height);
      context.globalCompositeOperation = "source-in";
      context.fillStyle = "rgba(0, 180, 255, 0.92)";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.globalCompositeOperation = "source-over";
    };
    mask.src = protectMask;
  }

  function pointerPosition(event: React.PointerEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * canvas.width,
      y: ((event.clientY - rect.top) / rect.height) * canvas.height
    };
  }

  function paint(event: React.PointerEvent<HTMLCanvasElement>) {
    if (!drawingRef.current || !canvasRef.current) return;
    const context = canvasRef.current.getContext("2d");
    if (!context) return;
    const point = pointerPosition(event);
    context.save();
    context.globalCompositeOperation = mode === "protect" ? "source-over" : "destination-out";
    context.fillStyle = "rgba(0, 180, 255, 0.92)";
    context.beginPath();
    context.arc(point.x, point.y, brushSize, 0, Math.PI * 2);
    context.fill();
    context.restore();
  }

  function resetMask() {
    drawProtectMask();
    setPreviewImage("");
    setShowOriginal(false);
    setMessage("已重置保护区，请确认后重新选择颜色预览。");
  }

  function finishPaint() {
    drawingRef.current = false;
    if (previewImage) {
      refreshPreview();
    }
  }

  function wheelZoom(event: React.WheelEvent<HTMLDivElement>) {
    if (!uploaded) return;
    event.preventDefault();
    const delta = event.deltaY > 0 ? -0.1 : 0.1;
    setZoom((value) => Math.min(3, Math.max(0.5, Number((value + delta).toFixed(2)))));
  }

  function chooseColor(color: string) {
    const normalized = color.trim();
    const isSameColor = normalized.toLowerCase() === targetColor.toLowerCase();
    setTargetColor(normalized);
    if (!subjectMask || !protectMask) {
      setMessage("请先点击“自动识别”，确认五金保护区后再调色。");
      return;
    }
    if (isSameColor && subjectMask && protectMask && /^#[0-9a-fA-F]{6}$/.test(normalized)) {
      refreshPreview();
    }
  }

  return (
    <section className="panel recolor-panel">
      <div className="panel-title-row">
        <div>
          <h2>智能调色</h2>
          <p>本地换色：包身、图案和花纹跟随目标色变化，五金保护不变。</p>
        </div>
        {result?.image_url && (
          <a href={result.image_url} download="recolor.png">
            <Download size={16} />
            下载调色图
          </a>
        )}
      </div>

      <div className="recolor-layout">
        <div className="recolor-stage">
          {!uploaded && (
            <label className="upload-box recolor-upload">
              <UploadCloud size={28} />
              <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => upload(event.target.files?.[0])} />
              <span>上传一张用于调色的女包图</span>
            </label>
          )}
          {uploaded && (
            <div>
              <div className="stage-toolbar">
                <button className={!showOriginal ? "active-tool" : ""} onClick={() => setShowOriginal(false)} disabled={!previewImage}>
                  <Eye size={16} />
                  调色预览
                </button>
                <button className={showOriginal ? "active-tool" : ""} onClick={() => setShowOriginal(true)}>
                  <ImageIcon size={16} />
                  原图
                </button>
                <span>{Math.round(zoom * 100)}%</span>
                <button onClick={() => setZoom(1)}>100%</button>
              </div>
              <div className="mask-viewport" onWheel={wheelZoom}>
                <div className="mask-canvas-wrap" style={{ width: `${zoom * 100}%` }}>
                  <img ref={imageRef} src={showOriginal || !previewImage ? uploaded.preview_url : previewImage} onLoad={drawProtectMask} />
                  <canvas
                    ref={canvasRef}
                    style={{ display: showOriginal ? "none" : "block" }}
                    onPointerDown={(event) => {
                      drawingRef.current = true;
                      paint(event);
                    }}
                    onPointerMove={paint}
                    onPointerUp={finishPaint}
                    onPointerLeave={finishPaint}
                  />
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="recolor-controls">
          <label>目标颜色</label>
          <div className="color-row">
            <input type="color" value={/^#[0-9a-fA-F]{6}$/.test(targetColor) ? targetColor : "#000000"} onChange={(event) => chooseColor(event.target.value)} />
            <input value={targetColor} onChange={(event) => chooseColor(event.target.value)} />
          </div>
          <div className="palette-row">
            {palette.map((color) => (
              <button key={color} className="swatch" style={{ background: color }} onClick={() => chooseColor(color)} title={color} />
            ))}
          </div>
          <p className="recolor-help">先点“自动识别”并确认蓝色五金保护区。之后点调色盘或修改颜色才会生成预览，不会保存历史。</p>
          <div className="toolbar">
            <button onClick={analyze} disabled={busy || !uploaded}>
              <Pipette size={16} />
              自动识别
            </button>
            <button className={mode === "protect" ? "active-tool" : ""} onClick={() => setMode("protect")}>
              <Shield size={16} />
              保护五金
            </button>
            <button className={mode === "erase" ? "active-tool" : ""} onClick={() => setMode("erase")}>
              <Eraser size={16} />
              擦除保护
            </button>
          </div>
          <label>画笔大小：{brushSize}px</label>
          <input type="range" min="1" max="30" value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} />
          <div className="toolbar">
            <button onClick={resetMask} disabled={!protectMask}>
              <RotateCcw size={16} />
              重置保护区
            </button>
            <button className="primary" onClick={saveResult} disabled={busy || previewBusy || !uploaded}>
              <Save size={16} />
              保存调色结果
            </button>
          </div>
          {previewBusy && <div className="status running">正在更新预览...</div>}
          {message && <div className="notice">{message}</div>}
          {result?.image_url && (
            <div className="recolor-result">
              <strong>已保存到历史记录</strong>
              <img src={result.image_url} />
              <div className="toolbar compact">
                <a href={result.image_url} download="recolor.png">
                  <Download size={15} />
                  下载
                </a>
                <button onClick={() => onUseAsSource(result.uploaded_image)}>保存为 AI 生成源图</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
