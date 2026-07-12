import { Download, Eraser, Eye, Image as ImageIcon, Pipette, RotateCcw, Save, ScanSearch, Shield, UploadCloud, Wand2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { HexColorPicker } from "react-colorful";
import { api } from "../api/client";

type UploadedImage = {
  image_id: number;
  file_name: string;
  preview_url: string;
};

type Props = {
  onUseAsSource: (image: UploadedImage) => void;
  onSendOriginalToAi: (image: UploadedImage, targetColor: string) => void;
};

const palette = ["#111111", "#f5f2ea", "#b52126", "#8a1f1d", "#2f4d3c", "#5f4635", "#d9c7a3", "#6f7d8f"];

type SelectionBox = {
  left: number;
  top: number;
  right: number;
  bottom: number;
  action: "add" | "remove";
};

function hexToRgb(color: string) {
  const match = /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/.exec(color);
  if (!match) return { r: 0, g: 0, b: 0 };
  return {
    r: Number.parseInt(match[1], 16),
    g: Number.parseInt(match[2], 16),
    b: Number.parseInt(match[3], 16)
  };
}

function rgbToHex(r: number, g: number, b: number) {
  const channel = (value: number) => Math.min(255, Math.max(0, Math.round(value))).toString(16).padStart(2, "0");
  return `#${channel(r)}${channel(g)}${channel(b)}`;
}

function rgbToHsv({ r, g, b }: { r: number; g: number; b: number }) {
  const red = r / 255;
  const green = g / 255;
  const blue = b / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  let h = 0;
  if (delta) {
    if (max === red) h = 60 * (((green - blue) / delta) % 6);
    else if (max === green) h = 60 * ((blue - red) / delta + 2);
    else h = 60 * ((red - green) / delta + 4);
  }
  if (h < 0) h += 360;
  return { h, s: max ? (delta / max) * 100 : 0, v: max * 100 };
}

function hsvToHex(h: number, s: number, v: number) {
  const saturation = s / 100;
  const value = v / 100;
  const chroma = value * saturation;
  const section = h / 60;
  const x = chroma * (1 - Math.abs((section % 2) - 1));
  const offset = value - chroma;
  const [red, green, blue] =
    section < 1 ? [chroma, x, 0] :
    section < 2 ? [x, chroma, 0] :
    section < 3 ? [0, chroma, x] :
    section < 4 ? [0, x, chroma] :
    section < 5 ? [x, 0, chroma] : [chroma, 0, x];
  return rgbToHex((red + offset) * 255, (green + offset) * 255, (blue + offset) * 255);
}

export default function RecolorPanel({ onUseAsSource, onSendOriginalToAi }: Props) {
  const [uploaded, setUploaded] = useState<UploadedImage | null>(null);
  const [targetColor, setTargetColor] = useState("#b52126");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [subjectMask, setSubjectMask] = useState("");
  const [protectMask, setProtectMask] = useState("");
  const [initialProtectMask, setInitialProtectMask] = useState("");
  const [previewImage, setPreviewImage] = useState("");
  const [showOriginal, setShowOriginal] = useState(false);
  const [showProtection, setShowProtection] = useState(true);
  const [result, setResult] = useState<any>(null);
  const [mode, setMode] = useState<"smart" | "protect" | "erase">("protect");
  const [selectionBox, setSelectionBox] = useState<SelectionBox | null>(null);
  const [brushSize, setBrushSize] = useState(6);
  const [zoom, setZoom] = useState(1);
  const [busy, setBusy] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [message, setMessage] = useState("");
  const imageRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef(false);
  const selectionStartRef = useRef<{ x: number; y: number; action: "add" | "remove" } | null>(null);
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
      setInitialProtectMask("");
      setPreviewImage("");
      setShowOriginal(true);
      setShowProtection(true);
      setResult(null);
      setSelectionBox(null);
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
    setInitialProtectMask(data.protect_mask);
    setMessage(`已自动识别主体和五金候选区：${data.segmentation_backend}`);
    return data;
  }

  function exportProtectMask() {
    const canvas = canvasRef.current;
    if (!canvas) return protectMask;
    const context = canvas.getContext("2d");
    if (!context) return protectMask;
    const source = context.getImageData(0, 0, canvas.width, canvas.height);
    const cleanCanvas = document.createElement("canvas");
    cleanCanvas.width = canvas.width;
    cleanCanvas.height = canvas.height;
    const cleanContext = cleanCanvas.getContext("2d");
    if (!cleanContext) return protectMask;
    const clean = cleanContext.createImageData(canvas.width, canvas.height);
    for (let index = 0; index < source.data.length; index += 4) {
      const value = source.data[index + 3] > 24 ? 255 : 0;
      clean.data[index] = value;
      clean.data[index + 1] = value;
      clean.data[index + 2] = value;
      clean.data[index + 3] = 255;
    }
    cleanContext.putImageData(clean, 0, 0);
    return cleanCanvas.toDataURL("image/png");
  }

  async function ensureMasks(protectOverride?: string) {
    if (subjectMask && protectMask) {
      return {
        subject_mask: subjectMask,
        protect_mask: protectOverride || exportProtectMask() || protectMask
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
      setShowProtection(true);
      setMode("smart");
      setMessage("已识别保护区。蓝色区域会保持原色；确认后选择颜色开始预览。");
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setBusy(false);
    }
  }

  async function refreshPreview(protectOverride?: string) {
    if (!uploaded) {
      return;
    }
    setPreviewBusy(true);
    try {
      const masks = await ensureMasks(protectOverride);
      const data = await api.previewRecolor({
        uploaded_image_id: uploaded.image_id,
        target_color: targetColor,
        subject_mask: masks.subject_mask,
        protect_mask: masks.protect_mask
      });
      setPreviewImage(data.preview_image);
      setShowOriginal(false);
      setShowProtection(false);
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
      const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
      const data = imageData.data;
      for (let index = 0; index < data.length; index += 4) {
        const maskValue = data[index];
        data[index] = 0;
        data[index + 1] = 180;
        data[index + 2] = 255;
        data[index + 3] = maskValue > 24 ? Math.min(235, maskValue) : 0;
      }
      context.putImageData(imageData, 0, 0);
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
    if (mode === "smart" || !drawingRef.current || !canvasRef.current) return;
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
    if (!initialProtectMask) return;
    setProtectMask(initialProtectMask);
    setShowOriginal(false);
    setShowProtection(true);
    setMessage("已恢复自动识别得到的保护区，可继续修改或重新预览。");
    if (previewImage) refreshPreview(initialProtectMask);
  }

  function finishPaint() {
    if (!drawingRef.current) return;
    drawingRef.current = false;
    const updatedMask = exportProtectMask();
    if (updatedMask) setProtectMask(updatedMask);
    if (previewImage) {
      refreshPreview(updatedMask);
    }
  }

  function startCanvasAction(event: React.PointerEvent<HTMLCanvasElement>) {
    if (mode !== "smart") {
      drawingRef.current = true;
      paint(event);
      return;
    }
    if (!protectMask) {
      setMessage("请先点击“自动识别”，再用智能框选补充或排除五金。");
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    const point = pointerPosition(event);
    const action = event.button === 2 ? "remove" : "add";
    selectionStartRef.current = { ...point, action };
    setSelectionBox({ left: point.x, top: point.y, right: point.x, bottom: point.y, action });
  }

  function moveCanvasAction(event: React.PointerEvent<HTMLCanvasElement>) {
    if (mode !== "smart") {
      paint(event);
      return;
    }
    const start = selectionStartRef.current;
    if (!start) return;
    const point = pointerPosition(event);
    setSelectionBox({
      left: Math.min(start.x, point.x),
      top: Math.min(start.y, point.y),
      right: Math.max(start.x, point.x),
      bottom: Math.max(start.y, point.y),
      action: start.action
    });
  }

  async function finishCanvasAction(event: React.PointerEvent<HTMLCanvasElement>) {
    if (mode !== "smart") {
      finishPaint();
      return;
    }
    const start = selectionStartRef.current;
    if (!start || !uploaded || !canvasRef.current) return;
    const point = pointerPosition(event);
    selectionStartRef.current = null;
    const box: SelectionBox = {
      left: Math.floor(Math.min(start.x, point.x)),
      top: Math.floor(Math.min(start.y, point.y)),
      right: Math.ceil(Math.max(start.x, point.x)),
      bottom: Math.ceil(Math.max(start.y, point.y)),
      action: start.action
    };
    setSelectionBox(null);
    setBusy(true);
    try {
      const data = await api.selectRecolorHardware({
        uploaded_image_id: uploaded.image_id,
        protect_mask: exportProtectMask(),
        ...box
      });
      if (!data.selected_pixels) {
        setMessage("该区域没有找到清晰轮廓，请框紧一些或使用保护画笔补充。");
        return;
      }
      setProtectMask(data.protect_mask);
      setShowOriginal(false);
      setShowProtection(true);
      setMessage(box.action === "add" ? "已添加智能框选区域。可继续框选其他五金。" : "已排除智能框选区域。");
      if (previewImage) await refreshPreview(data.protect_mask);
    } catch (error: any) {
      setMessage(explainError(error));
    } finally {
      setBusy(false);
    }
  }

  function activateSmartSelection() {
    if (!protectMask) {
      setMessage("请先点击“自动识别”，再使用智能框选修正结果。");
      return;
    }
    setMode("smart");
    setShowOriginal(false);
    setShowProtection(true);
    setMessage("左键拖框添加五金保护，右键拖框排除错误区域；单击会选择点击附近的小区域。");
  }

  function activateBrush(nextMode: "protect" | "erase") {
    if (!protectMask) {
      setMessage("请先点击“自动识别”，再使用画笔修正保护区。");
      return;
    }
    setMode(nextMode);
    setShowOriginal(false);
    setShowProtection(true);
    setMessage(nextMode === "protect" ? "在图片上涂抹需要保持原色的五金。" : "在蓝色区域上涂抹，排除错误保护区域。");
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

  function chooseRgb(channel: "r" | "g" | "b", value: string) {
    const current = hexToRgb(targetColor);
    const next = Number.parseInt(value, 10);
    if (Number.isNaN(next)) return;
    chooseColor(rgbToHex(
      channel === "r" ? next : current.r,
      channel === "g" ? next : current.g,
      channel === "b" ? next : current.b
    ));
  }

  function fineTunePicker(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!event.key.startsWith("Arrow")) return;
    const control = (event.target as HTMLElement).getAttribute("aria-label");
    if (control !== "Color" && control !== "Hue") return;
    event.preventDefault();
    event.stopPropagation();

    const hsv = rgbToHsv(hexToRgb(targetColor));
    if (control === "Hue") {
      const direction = event.key === "ArrowRight" || event.key === "ArrowUp" ? 0.5 : -0.5;
      chooseColor(hsvToHex((hsv.h + direction + 360) % 360, hsv.s, hsv.v));
      return;
    }

    const clamp = (value: number) => Math.min(100, Math.max(0, value));
    const nextSaturation = clamp(hsv.s + (event.key === "ArrowRight" ? 0.5 : event.key === "ArrowLeft" ? -0.5 : 0));
    const nextValue = clamp(hsv.v + (event.key === "ArrowUp" ? 0.5 : event.key === "ArrowDown" ? -0.5 : 0));
    chooseColor(hsvToHex(hsv.h, nextSaturation, nextValue));
  }

  const rgb = hexToRgb(targetColor);

  return (
    <section className="panel recolor-panel">
      {result?.image_url && (
        <div className="panel-title-row">
          <a href={result.image_url} download="recolor.png">
            <Download size={16} />
            下载调色图
          </a>
        </div>
      )}

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
                <button className={showProtection ? "active-tool" : ""} onClick={() => setShowProtection((value) => !value)} disabled={!protectMask || showOriginal}>
                  <Shield size={16} />
                  {showProtection ? "隐藏保护区" : "显示保护区"}
                </button>
                <span>{Math.round(zoom * 100)}%</span>
                <button onClick={() => setZoom(1)}>100%</button>
              </div>
              <div className="mask-viewport" onWheel={wheelZoom}>
                <div className="mask-canvas-wrap" style={{ width: `${zoom * 100}%` }}>
                  <img ref={imageRef} src={showOriginal || !previewImage ? uploaded.preview_url : previewImage} onLoad={drawProtectMask} />
                  <canvas
                    ref={canvasRef}
                    style={{ display: showOriginal || !showProtection ? "none" : "block" }}
                    className={mode === "smart" ? "smart-select-canvas" : ""}
                    onContextMenu={(event) => event.preventDefault()}
                    onPointerDown={startCanvasAction}
                    onPointerMove={moveCanvasAction}
                    onPointerUp={finishCanvasAction}
                    onPointerLeave={() => {
                      if (mode !== "smart") finishPaint();
                    }}
                    onPointerCancel={() => {
                      selectionStartRef.current = null;
                      setSelectionBox(null);
                      finishPaint();
                    }}
                  />
                  {selectionBox && canvasRef.current && (
                    <div
                      className={`smart-selection-box ${selectionBox.action}`}
                      style={{
                        left: `${(selectionBox.left / canvasRef.current.width) * 100}%`,
                        top: `${(selectionBox.top / canvasRef.current.height) * 100}%`,
                        width: `${((selectionBox.right - selectionBox.left) / canvasRef.current.width) * 100}%`,
                        height: `${((selectionBox.bottom - selectionBox.top) / canvasRef.current.height) * 100}%`
                      }}
                    />
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="recolor-controls">
          <label>目标颜色</label>
          <div className="color-row">
            <button
              className="color-picker-trigger"
              style={{ background: /^#[0-9a-fA-F]{6}$/.test(targetColor) ? targetColor : "#000000" }}
              onClick={() => setPickerOpen((current) => !current)}
              title="打开大取色器"
              aria-label="打开大取色器"
            />
            <input value={targetColor} onChange={(event) => chooseColor(event.target.value)} />
          </div>
          {pickerOpen && (
            <div className="large-color-picker" onKeyDownCapture={fineTunePicker}>
              <HexColorPicker color={targetColor} onChange={chooseColor} />
              <div className="rgb-fields">
                <label>
                  <span>R</span>
                  <input type="number" min="0" max="255" step="1" value={rgb.r} onChange={(event) => chooseRgb("r", event.target.value)} />
                </label>
                <label>
                  <span>G</span>
                  <input type="number" min="0" max="255" step="1" value={rgb.g} onChange={(event) => chooseRgb("g", event.target.value)} />
                </label>
                <label>
                  <span>B</span>
                  <input type="number" min="0" max="255" step="1" value={rgb.b} onChange={(event) => chooseRgb("b", event.target.value)} />
                </label>
              </div>
              <button onClick={() => setPickerOpen(false)}>收起取色器</button>
            </div>
          )}
          <div className="palette-row">
            {palette.map((color) => (
              <button key={color} className="swatch" style={{ background: color }} onClick={() => chooseColor(color)} title={color} />
            ))}
          </div>
          <button
            className="primary recolor-to-ai"
            disabled={!uploaded || !/^#[0-9a-fA-F]{6}$/.test(targetColor)}
            onClick={() => uploaded && onSendOriginalToAi(uploaded, targetColor)}
            title="只带入原图和当前目标颜色，不使用本地调色预览及调节参数"
          >
            <Wand2 size={17} />
            原图和当前颜色去 AI 换色
          </button>
          <p className="recolor-help">先用“自动识别”得到初稿，再用“智能框选”修正：左键框选五金，右键框选可排除误识别。之后选择颜色生成预览。</p>
          <div className="toolbar">
            <button onClick={analyze} disabled={busy || !uploaded}>
              <Pipette size={16} />
              自动识别
            </button>
            <button className={mode === "smart" ? "active-tool" : ""} onClick={activateSmartSelection} disabled={!uploaded || busy}>
              <ScanSearch size={16} />
              智能框选
            </button>
            <button className={mode === "protect" ? "active-tool" : ""} onClick={() => activateBrush("protect")}>
              <Shield size={16} />
              保护五金
            </button>
            <button className={mode === "erase" ? "active-tool" : ""} onClick={() => activateBrush("erase")}>
              <Eraser size={16} />
              擦除保护
            </button>
          </div>
          <label>画笔大小：{brushSize}px</label>
          <input type="range" min="1" max="30" value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} />
          <div className="toolbar">
            <button onClick={resetMask} disabled={!initialProtectMask}>
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
