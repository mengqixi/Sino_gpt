import { Download, Eraser, Paintbrush, Pipette, RotateCcw, Shield, UploadCloud } from "lucide-react";
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
  const [brushSize, setBrushSize] = useState(18);
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

  async function analyze() {
    if (!uploaded) {
      setMessage("请先上传一张女包原图");
      return;
    }
    setBusy(true);
    try {
      const data = await api.analyzeRecolor({ uploaded_image_id: uploaded.image_id });
      setSubjectMask(data.subject_mask);
      setProtectMask(data.protect_mask);
      setMessage(`已自动识别主体和五金候选区：${data.segmentation_backend}`);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!uploaded || !subjectMask || !protectMask) {
      setMessage("请先上传图片并自动识别调色区域");
      return;
    }
    setBusy(true);
    try {
      const editedProtectMask = canvasRef.current?.toDataURL("image/png") || protectMask;
      const data = await api.applyRecolor({
        uploaded_image_id: uploaded.image_id,
        target_color: targetColor,
        subject_mask: subjectMask,
        protect_mask: editedProtectMask
      });
      setResult(data);
      setMessage("调色结果已保存，可下载或选为生成源图");
    } catch (error: any) {
      setMessage(error.message);
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
            <div className="mask-canvas-wrap">
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
          <label>画笔大小</label>
          <input type="range" min="6" max="60" value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} />
          <div className="toolbar">
            <button onClick={resetMask} disabled={!protectMask}>
              <RotateCcw size={16} />
              重置保护区
            </button>
            <button className="primary" onClick={apply} disabled={busy || !subjectMask}>
              <Paintbrush size={16} />
              应用调色
            </button>
          </div>
          {message && <div className="notice">{message}</div>}
          {result?.image_url && (
            <div className="recolor-result">
              <img src={result.image_url} />
              <button onClick={() => onUseAsSource(result.uploaded_image)}>选为生成源图</button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
