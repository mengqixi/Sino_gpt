import { Download, Eraser, Paintbrush, Pipette, RotateCcw, Shield, UploadCloud, ZoomIn, ZoomOut } from "lucide-react";
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
  const [result, setResult] = useState<any>(null);
  const [mode, setMode] = useState<"protect" | "erase">("protect");
  const [brushSize, setBrushSize] = useState(6);
  const [zoom, setZoom] = useState(1);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const imageRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef(false);

  useEffect(() => {
    drawProtectMask();
  }, [uploaded, protectMask]);

  async function upload(file?: File) {
    if (!file) return;
    try {
      const row = await api.uploadImage(file);
      setUploaded(row);
      setSubjectMask("");
      setProtectMask("");
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

  async function analyze() {
    setBusy(true);
    try {
      await analyzeMasks();
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!uploaded) {
      setMessage("请先上传一张女包原图");
      return;
    }
    setBusy(true);
    try {
      let activeSubjectMask = subjectMask;
      let activeProtectMask = canvasRef.current?.toDataURL("image/png") || protectMask;
      if (!activeSubjectMask || !activeProtectMask) {
        const masks = await analyzeMasks();
        activeSubjectMask = masks.subject_mask;
        activeProtectMask = masks.protect_mask;
      }
      const data = await api.applyRecolor({
        uploaded_image_id: uploaded.image_id,
        target_color: targetColor,
        subject_mask: activeSubjectMask,
        protect_mask: activeProtectMask
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
    if (!canvas || !image || !protectMask) return;
    canvas.width = image.naturalWidth || image.clientWidth;
    canvas.height = image.naturalHeight || image.clientHeight;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    const mask = new Image();
    mask.onload = () => {
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(mask, 0, 0, canvas.width, canvas.height);
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
    context.fillStyle = "white";
    context.beginPath();
    context.arc(point.x, point.y, brushSize, 0, Math.PI * 2);
    context.fill();
    context.restore();
  }

  function resetMask() {
    drawProtectMask();
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
                <button onClick={() => setZoom((value) => Math.max(0.5, Number((value - 0.25).toFixed(2))))}>
                  <ZoomOut size={16} />
                  缩小
                </button>
                <span>{Math.round(zoom * 100)}%</span>
                <button onClick={() => setZoom((value) => Math.min(3, Number((value + 0.25).toFixed(2))))}>
                  <ZoomIn size={16} />
                  放大
                </button>
                <button onClick={() => setZoom(1)}>100%</button>
              </div>
              <div className="mask-viewport">
                <div className="mask-canvas-wrap" style={{ width: `${zoom * 100}%` }}>
                  <img ref={imageRef} src={uploaded.preview_url} onLoad={drawProtectMask} />
                  <canvas
                    ref={canvasRef}
                    onPointerDown={(event) => {
                      drawingRef.current = true;
                      paint(event);
                    }}
                    onPointerMove={paint}
                    onPointerUp={() => (drawingRef.current = false)}
                    onPointerLeave={() => (drawingRef.current = false)}
                  />
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="recolor-controls">
          <label>目标颜色</label>
          <div className="color-row">
            <input type="color" value={targetColor} onChange={(event) => setTargetColor(event.target.value)} />
            <input value={targetColor} onChange={(event) => setTargetColor(event.target.value)} />
          </div>
          <div className="palette-row">
            {palette.map((color) => (
              <button key={color} className="swatch" style={{ background: color }} onClick={() => setTargetColor(color)} title={color} />
            ))}
          </div>
          <p className="recolor-help">可以直接点“应用当前颜色并保存”。系统会先识别包身调色区域，再把当前目标色应用到包身、图案和花纹上；五金保护区可用画笔修正。</p>
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
            <button className="primary" onClick={apply} disabled={busy || !uploaded}>
              <Paintbrush size={16} />
              应用当前颜色并保存
            </button>
          </div>
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
