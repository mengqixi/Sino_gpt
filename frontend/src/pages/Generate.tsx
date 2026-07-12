import { Copy, Crop, Download, HelpCircle, RotateCcw, Save, UploadCloud, Wand2, X, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { TASKS, taskLabel } from "../types";

const defaultParams = {
  target_color: "",
  target_material: "",
  model_showcase_requirement: "",
  wearing_method: "",
  scene: "",
  outfit: "",
  bag_length_cm: "",
  bag_width_cm: "",
  bag_height_cm: "",
  extra_requirements: ""
};

const colorParts = ["包身", "肩带", "手提", "挂件", "流苏", "链条皮穿带", "包边", "包底", "五金"];
const sizeOptions = [
  { group: "1K", label: "1K 方图 · 1024 x 1024", value: "1024x1024" },
  { group: "2K", label: "2K 横图 · 1536 x 1024", value: "1536x1024" },
  { group: "2K", label: "2K 竖图 · 1024 x 1536", value: "1024x1536" },
  { group: "2K", label: "2K 方图 · 2048 x 2048", value: "2048x2048" },
  { group: "2K", label: "2K 宽屏 · 2048 x 1152", value: "2048x1152" },
  { group: "2K", label: "2K 竖幅 · 1152 x 2048", value: "1152x2048" },
  { group: "4K", label: "4K 横图 · 3840 x 2160", value: "3840x2160" },
  { group: "4K", label: "4K 竖图 · 2160 x 3840", value: "2160x3840" }
];

function preferredApiConfig(rows: any[]) {
  const enabled = rows.filter((item) => item.enabled);
  return enabled.find((item) => item.config_name?.trim() === "快速") || enabled.find((item) => item.is_default) || enabled[0] || null;
}

function statusLabel(status: string) {
  if (status === "unknown") return "结果未知";
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  if (status === "running") return "生成中";
  return status;
}

export default function Generate({ initialUploadedImages = [] }: { initialUploadedImages?: any[] }) {
  const [taskType, setTaskType] = useState("");
  const [prompts, setPrompts] = useState<any[]>([]);
  const [configs, setConfigs] = useState<any[]>([]);
  const [templateId, setTemplateId] = useState<number | "">("");
  const [apiConfigId, setApiConfigId] = useState<number | "">("");
  const [continueApiConfigId, setContinueApiConfigId] = useState<number | "">("");
  const [uploadedImages, setUploadedImages] = useState<any[]>([]);
  const [params, setParams] = useState(defaultParams);
  const [sizeMode, setSizeMode] = useState("2048x2048");
  const [customImageSize, setCustomImageSize] = useState("2048x2048");
  const [colorScope, setColorScope] = useState("partial");
  const [selectedParts, setSelectedParts] = useState<string[]>(["包身"]);
  const [colorNote, setColorNote] = useState("");
  const [finalPrompt, setFinalPrompt] = useState("");
  const [promptTouched, setPromptTouched] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [resultJobs, setResultJobs] = useState<any[]>([]);
  const [selectedResult, setSelectedResult] = useState<any>(null);
  const [continueUploadedImages, setContinueUploadedImages] = useState<any[]>([]);
  const [continueText, setContinueText] = useState("");
  const [conversation, setConversation] = useState<Array<{ role: string; text: string; imageUrl?: string }>>([]);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [previewScale, setPreviewScale] = useState(1);
  const [cropTarget, setCropTarget] = useState<any>(null);
  const [cropRect, setCropRect] = useState<{ left: number; top: number; right: number; bottom: number } | null>(null);
  const [cropBusy, setCropBusy] = useState(false);
  const [cropError, setCropError] = useState("");
  const cropStageRef = useRef<HTMLDivElement | null>(null);
  const cropStartRef = useRef<{ x: number; y: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  const imageSize = sizeMode === "custom" ? customImageSize : sizeMode;
  const uploaded = uploadedImages[0] || null;

  useEffect(() => {
    if (initialUploadedImages.length) {
      setUploadedImages(initialUploadedImages);
      setMessage("已将调色结果选为生成源图");
    }
  }, [initialUploadedImages]);

  const bagDimensionExtra = useMemo(() => {
    const length = params.bag_length_cm.trim();
    const width = params.bag_width_cm.trim();
    const height = params.bag_height_cm.trim();
    if (!length && !width && !height) return "";
    return `包包实物尺寸参考：长 ${length || "未填"} cm，宽/厚 ${width || "未填"} cm，高 ${height || "未填"} cm。生成模特图时请按这个尺寸控制上身比例，不要把包画得过大或过小。`;
  }, [params.bag_length_cm, params.bag_width_cm, params.bag_height_cm]);

  const modelExtra = useMemo(() => {
    if (taskType !== "model_showcase") return params.extra_requirements;
    return [params.extra_requirements.trim(), bagDimensionExtra].filter(Boolean).join("\n");
  }, [taskType, params.extra_requirements, bagDimensionExtra]);

  const colorExtra = useMemo(() => {
    if (taskType !== "color_change") return params.extra_requirements;
    const scopeText =
      colorScope === "all"
        ? "换色范围：全部可着色部位统一换色，包括包身、肩带、手提、挂件、流苏、链条皮穿带、包边、包底等；五金默认不变，除非备注明确要求。"
        : `换色范围：仅修改${selectedParts.length ? selectedParts.join("、") : "包身"}。未勾选部位保持原图颜色和质感。`;
    return [scopeText, colorNote.trim()].filter(Boolean).join("\n");
  }, [taskType, params.extra_requirements, colorScope, selectedParts, colorNote]);

  const mergedParams = useMemo(
    () => ({
      ...params,
      extra_requirements: taskType === "color_change" ? colorExtra : taskType === "model_showcase" ? modelExtra : params.extra_requirements,
      image_size: imageSize
    }),
    [params, taskType, colorExtra, modelExtra, imageSize]
  );
  const paramsKey = JSON.stringify(mergedParams);

  useEffect(() => {
    reload();
  }, [taskType]);

  async function reload() {
    const [promptRows, configRows] = await Promise.all([taskType ? api.getPrompts(taskType) : Promise.resolve([]), api.getApiConfigs()]);
    setPrompts(promptRows);
    const enabledConfigs = configRows.filter((item) => item.enabled);
    setConfigs(enabledConfigs);
    const defaultPrompt = promptRows.find((item) => item.is_default) || promptRows[0];
    setTemplateId(defaultPrompt?.id || "");
    if (!taskType) {
      setFinalPrompt("");
      setPromptTouched(false);
    }
    const defaultConfig = preferredApiConfig(configRows);
    if (defaultConfig) setApiConfigId(defaultConfig.id);
    if (defaultConfig) setContinueApiConfigId(defaultConfig.id);
  }

  useEffect(() => {
    setPromptTouched(false);
  }, [templateId, taskType]);

  useEffect(() => {
    if (templateId && !promptTouched) renderPrompt(false);
  }, [templateId, taskType, paramsKey, promptTouched]);

  async function renderPrompt(force = true) {
    if (!templateId) return;
    if (promptTouched && !force) return;
    try {
      const data = await api.renderPrompt({ template_id: templateId, task_type: taskType, params: mergedParams });
      setFinalPrompt(data.final_prompt);
      setMessage("");
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function handleUpload(files?: FileList | null) {
    const fileList = Array.from(files || []);
    if (!fileList.length) return;
    try {
      const uploadedRows = await api.uploadImages(fileList);
      setUploadedImages(uploadedRows);
      setMessage("");
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function handleContinueUpload(files?: FileList | null) {
    const fileList = Array.from(files || []);
    if (!fileList.length) return;
    try {
      const uploadedRows = await api.uploadImages(fileList);
      setContinueUploadedImages(uploadedRows);
      setMessage("");
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function saveAsTemplate() {
    if (!finalPrompt.trim()) return;
    const created = await api.createPrompt({
      name: `${taskLabel(taskType)}-自定义模板`,
      task_type: taskType,
      template_content: finalPrompt,
      variables: [],
      is_default: false
    });
    setPrompts([created, ...prompts]);
    setTemplateId(created.id);
    setMessage("已另存为新模板");
  }

  async function submitGenerate(
    uploadedImageIds: number[],
    prompt: string,
    extraParams: any = {},
    append = false,
    selectedConfigId: number | "" = apiConfigId
  ) {
    if (!taskType) {
      setMessage("请先选择功能类型，再填写参数和生成图片");
      return null;
    }
    if (!selectedConfigId) {
      setMessage("请先选择 API 配置");
      return null;
    }
    if (!uploadedImageIds.length) {
      setMessage("请先上传女包原图");
      return null;
    }
    const data = await api.generate({
      task_type: taskType,
      uploaded_image_id: uploadedImageIds[0],
      uploaded_image_ids: uploadedImageIds,
      prompt_template_id: templateId || null,
      final_prompt: prompt,
      api_config_id: selectedConfigId,
      image_size: imageSize,
      params: { ...mergedParams, ...extraParams }
    });
    setResult(data.job);
    setResultJobs((items) => (append ? [...items, data.job] : [data.job]));
    const firstImage = data.job?.results?.[0] || null;
    setSelectedResult(firstImage);
    return data;
  }

  async function generate() {
    if (!uploadedImages.length) {
      setMessage("请先上传女包原图");
      return;
    }
    setLoading(true);
    setMessage("");
    setResult(null);
    setResultJobs([]);
    try {
      const data = await submitGenerate(uploadedImages.map((image) => image.image_id), finalPrompt);
      if (!data) return;
      if (data.job?.status === "success") {
        setConversation((items) => [...items, { role: "生成", text: finalPrompt, imageUrl: data.job?.results?.[0]?.image_url }]);
      }
      if (data.status === "unknown") setMessage(data.job.error_message || "结果未知，可能已扣费；系统不会自动重试");
      if (data.status === "failed") setMessage(data.job.error_message || "生成失败");
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setLoading(false);
    }
  }

  function updateParam(key: string, value: string) {
    setParams((current) => ({ ...current, [key]: value }));
  }

  function togglePart(part: string) {
    setSelectedParts((current) => (current.includes(part) ? current.filter((item) => item !== part) : [...current, part]));
  }

  function replaceResultJob(updatedJob: any) {
    setResult((current: any) => (current?.job_id === updatedJob.job_id ? updatedJob : current));
    setResultJobs((current) => current.map((job) => (job.job_id === updatedJob.job_id ? updatedJob : job)));
  }

  function openCrop(image: any) {
    setCropTarget(image);
    setCropRect(null);
    setCropError("");
  }

  function cropPointerPosition(event: React.PointerEvent<HTMLDivElement>) {
    const rect = cropStageRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return {
      x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
    };
  }

  function startCropSelection(event: React.PointerEvent<HTMLDivElement>) {
    const point = cropPointerPosition(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    cropStartRef.current = point;
    setCropRect(null);
    setCropError("");
  }

  function moveCropSelection(event: React.PointerEvent<HTMLDivElement>) {
    if (!cropStartRef.current) return;
    const point = cropPointerPosition(event);
    if (!point) return;
    const start = cropStartRef.current;
    setCropRect({
      left: Math.min(start.x, point.x),
      top: Math.min(start.y, point.y),
      right: Math.max(start.x, point.x),
      bottom: Math.max(start.y, point.y)
    });
  }

  function finishCropSelection() {
    cropStartRef.current = null;
    if (cropRect && (cropRect.right - cropRect.left < 0.01 || cropRect.bottom - cropRect.top < 0.01)) {
      setCropRect(null);
      setCropError("裁剪区域过小，请重新拖动选择");
    }
  }

  async function saveManualCrop() {
    if (!cropTarget?.id) return;
    if (!cropRect) {
      setCropError("请先在图片上拖动选择裁剪区域");
      return;
    }
    setCropBusy(true);
    setCropError("");
    try {
      const data = await api.cropGeneratedImage(cropTarget.id, cropRect);
      replaceResultJob(data.job);
      setCropTarget(null);
      setMessage("裁剪图片已保存到当前生成记录");
    } catch (error: any) {
      setCropError(error.message || "裁剪失败");
    } finally {
      setCropBusy(false);
    }
  }

  async function splitGridImage() {
    if (!cropTarget?.id) return;
    setCropBusy(true);
    setCropError("");
    try {
      const data = await api.splitGeneratedImage(cropTarget.id);
      replaceResultJob(data.job);
      setCropTarget(null);
      setMessage("已自动切成四张图片，可分别预览、下载和继续修改");
    } catch (error: any) {
      setCropError(error.message || "自动切图失败");
    } finally {
      setCropBusy(false);
    }
  }

  async function continueModify() {
    if (!selectedResult?.id) {
      setMessage("请先选择一张生成结果作为继续修改的源图");
      return;
    }
    if (!continueText.trim()) {
      setMessage("请输入本轮修改要求");
      return;
    }
    setLoading(true);
    setMessage("");
    try {
      const nextSource = await api.reuseGeneratedImage(selectedResult.id);
      const nextSources = [nextSource, ...continueUploadedImages];
      setUploadedImages(nextSources);
      const nextPrompt = [
        "基于上一张生成图继续修改。",
        "只执行本轮明确提出的修改要求，未提到的包型、结构、材质、颜色、五金、Logo、缝线、背景和构图尽量保持上一张图不变。",
        "",
        "本轮修改要求：",
        continueText.trim()
      ].join("\n");
      setFinalPrompt(nextPrompt);
      setPromptTouched(true);
      const data = await submitGenerate(nextSources.map((image) => image.image_id), nextPrompt, {
        continue_from_generated_image_id: selectedResult.id,
        continue_uploaded_image_ids: continueUploadedImages.map((image) => image.image_id),
        continue_text: continueText.trim()
      }, true, continueApiConfigId);
      if (!data) return;
      if (data.job?.status === "success") {
        setConversation((items) => [...items, { role: "继续修改", text: continueText.trim(), imageUrl: data.job?.results?.[0]?.image_url }]);
        setContinueText("");
        setContinueUploadedImages([]);
      }
      if (data.status === "unknown") setMessage(data.job.error_message || "结果未知，可能已扣费；系统不会自动重试");
      if (data.status === "failed") setMessage(data.job.error_message || "继续修改失败");
      const fastConfig = preferredApiConfig(configs);
      if (fastConfig) setContinueApiConfigId(fastConfig.id);
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setLoading(false);
    }
  }

  function restartConversation() {
    setUploadedImages([]);
    setResult(null);
    setResultJobs([]);
    setSelectedResult(null);
    setContinueUploadedImages([]);
    setContinueText("");
    const fastConfig = preferredApiConfig(configs);
    setContinueApiConfigId(fastConfig?.id || "");
    setConversation([]);
    setFinalPrompt("");
    setPromptTouched(false);
    setMessage("");
  }

  const groupedSizes = sizeOptions.reduce<Record<string, typeof sizeOptions>>((groups, item) => {
    groups[item.group] = [...(groups[item.group] || []), item];
    return groups;
  }, {});
  const visibleTasks = TASKS;

  return (
    <div className="page">
      <header className="page-header">
        <h1>生成工作台</h1>
        <p>参数只用于生成提示词初稿，最终请求会使用文本框里的可编辑提示词。</p>
      </header>
      <div className="generate-grid">
        <section className="panel">
          <h2>图片与参数</h2>
          <label className="upload-box">
            <UploadCloud size={28} />
            <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => handleUpload(event.target.files)} />
            <span>{uploadedImages.length ? `已上传 ${uploadedImages.length} 张原图` : "上传女包原图，可多选"}</span>
          </label>
          {uploadedImages.length > 0 && (
            <div className="upload-preview-grid">
              {uploadedImages.map((image) => (
                <figure key={image.image_id}>
                  <img src={image.preview_url} />
                  <figcaption>{image.file_name}</figcaption>
                </figure>
              ))}
            </div>
          )}

          <label>功能类型</label>
          <select value={taskType} onChange={(event) => setTaskType(event.target.value)}>
            <option value="" disabled>请先选择功能类型</option>
            {visibleTasks.map((task) => (
              <option key={task.value} value={task.value}>
                {task.label}
              </option>
            ))}
          </select>
          {!taskType && <div className="alert warning task-type-alert">请尽快选择功能类型，选择后才会加载对应参数和提示词。</div>}

          {taskType === "color_change" && (
            <>
              <Input label="目标颜色" value={params.target_color} onChange={(v) => updateParam("target_color", v)} />
              <label>换色方式</label>
              <select value={colorScope} onChange={(event) => setColorScope(event.target.value)}>
                <option value="partial">部分换色</option>
                <option value="all">全部换色</option>
              </select>
              {colorScope === "partial" && (
                <div className="part-grid">
                  {colorParts.map((part) => (
                    <label className="check-row part-option" key={part}>
                      <input type="checkbox" checked={selectedParts.includes(part)} onChange={() => togglePart(part)} />
                      {part}
                    </label>
                  ))}
                </div>
              )}
              <label>换色备注</label>
              <textarea rows={3} value={colorNote} onChange={(event) => setColorNote(event.target.value)} placeholder="例如：五金保持金色；肩带外侧改黑色，内侧保持原色。" />
            </>
          )}

          {taskType === "material_replace" && (
            <>
              <Input label="目标材质" value={params.target_material} onChange={(v) => updateParam("target_material", v)} />
              <label>额外要求</label>
              <textarea value={params.extra_requirements} onChange={(event) => updateParam("extra_requirements", event.target.value)} rows={4} />
            </>
          )}

          {taskType === "model_showcase" && (
            <>
              <Input
                label="模特展示要求"
                hint="建议写整体方向，例如：法式轻奢、浪漫街拍、两张露脸两张不露脸、包包必须清晰突出。"
                value={params.model_showcase_requirement}
                onChange={(v) => updateParam("model_showcase_requirement", v)}
              />
              <Input
                label="展示方式"
                hint="例如：手提、斜挎、单肩、腋下夹包、肩背。可以写多个，系统会分配到四张图里。"
                value={params.wearing_method}
                onChange={(v) => updateParam("wearing_method", v)}
              />
              <Input
                label="场景"
                hint="例如：巴黎街角、法式咖啡馆、花店门口、拱门长廊、窗边自然光。"
                value={params.scene}
                onChange={(v) => updateParam("scene", v)}
              />
              <Input
                label="服装风格"
                hint="例如：浅色西装、针织开衫、风衣、法式条纹衫、直筒牛仔裤。"
                value={params.outfit}
                onChange={(v) => updateParam("outfit", v)}
              />
              <div className="dimension-grid">
                <Input label="包长 cm" value={params.bag_length_cm} onChange={(v) => updateParam("bag_length_cm", v)} type="number" />
                <Input label="包宽/厚 cm" value={params.bag_width_cm} onChange={(v) => updateParam("bag_width_cm", v)} type="number" />
                <Input label="包高 cm" value={params.bag_height_cm} onChange={(v) => updateParam("bag_height_cm", v)} type="number" />
              </div>
              <Hint text="长宽高只用于控制模特上身比例，不会作为接口固定参数发送给中转站。" />
              <label>额外要求</label>
              <textarea value={params.extra_requirements} onChange={(event) => updateParam("extra_requirements", event.target.value)} rows={4} />
            </>
          )}

          {taskType === "custom_generate" && (
            <>
              <label>自定义生成要求</label>
              <textarea
                value={params.extra_requirements}
                onChange={(event) => updateParam("extra_requirements", event.target.value)}
                rows={9}
                placeholder="例如：保留包型和五金不变，生成巴黎左岸黄昏街拍；模特穿酒红色风衣，单肩背包，画面浪漫、带电影感。"
              />
              <Hint text="可以自由写场景、人物、构图、风格、颜色、需要保留或修改的部位。未明确要求修改的包型、五金、Logo 和缝线将按模板保持原样。" />
            </>
          )}

          {taskType && <><label>图片尺寸</label>
          <select value={sizeMode} onChange={(event) => setSizeMode(event.target.value)}>
            {Object.entries(groupedSizes).map(([group, items]) => (
              <optgroup key={group} label={group}>
                {items.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </optgroup>
            ))}
            <option value="custom">自定义尺寸</option>
          </select>
          {sizeMode === "custom" && <Input label="自定义尺寸" value={customImageSize} onChange={setCustomImageSize} />}</>}

          <label>API 配置</label>
          <select value={apiConfigId} onChange={(event) => setApiConfigId(Number(event.target.value))}>
            {configs.map((config) => (
              <option key={config.id} value={config.id}>
                {config.config_name} {config.is_default ? "(默认)" : ""}
              </option>
            ))}
          </select>
        </section>

        <section className="panel prompt-panel">
          <div className="panel-title-row">
            <h2>最终提示词</h2>
            <button className="icon-button" onClick={() => navigator.clipboard.writeText(finalPrompt)} title="复制提示词">
              <Copy size={17} />
            </button>
          </div>
          {!taskType && <div className="empty-state">选择左侧功能类型后，这里会显示对应的最终提示词。</div>}
          {taskType && <><label>模板选择</label>
          <select value={templateId} onChange={(event) => setTemplateId(Number(event.target.value))}>
            {prompts.map((prompt) => (
              <option key={prompt.id} value={prompt.id}>
                {prompt.name} {prompt.is_default ? "(默认)" : ""}
              </option>
            ))}
          </select>
          <div className="toolbar">
            <button onClick={() => renderPrompt(true)}>
              <RotateCcw size={16} />
              重新渲染
            </button>
            <button onClick={saveAsTemplate}>
              <Save size={16} />
              保存为模板
            </button>
          </div>
          <textarea
            className="prompt-textarea"
            value={finalPrompt}
            onChange={(event) => {
              setPromptTouched(true);
              setFinalPrompt(event.target.value);
            }}
          /></>}
          {taskType && <div className="preview-block">
            <strong>变量替换预览</strong>
            <pre>{JSON.stringify(mergedParams, null, 2)}</pre>
          </div>}
        </section>

        <section className="panel result-panel">
          <div className="panel-title-row">
            <h2>生成结果</h2>
            <button onClick={restartConversation}>重开对话</button>
          </div>
          <button className="primary" onClick={generate} disabled={loading || !taskType}>
            <Wand2 size={18} />
            {loading ? "生成中" : "开始生成"}
          </button>
          {message && <div className="notice">{message}</div>}
          {result && <div className={`status ${result.status}`}>{statusLabel(result.status)}</div>}
          {result?.status === "unknown" && (
            <button
              onClick={() => {
                const fastConfig = preferredApiConfig(configs);
                if (fastConfig) setApiConfigId(fastConfig.id);
                setMessage("已切换到快速 API，尚未重新发送请求。请确认中转站记录后再手动生成。");
              }}
            >
              切换到快速 API
            </button>
          )}
          {result?.status === "failed" && (
            <button onClick={generate} disabled={loading}>
              <RotateCcw size={16} />
              重试上次生成
            </button>
          )}
          <div className="result-thread">
            {resultJobs.map((job, jobIndex) => (
              <article className="result-job" key={job.job_id || jobIndex}>
                <h3>{jobIndex === 0 ? "生成结果" : `继续修改 ${jobIndex}`}</h3>
                <div className="result-grid">
                  {job?.results?.map((image: any, index: number) => (
                    <figure key={image.id} className={selectedResult?.id === image.id ? "selected-result" : ""}>
                      <button
                        className="result-preview-button"
                        onClick={() => {
                          setPreviewImage(image.image_url);
                          setPreviewScale(1);
                        }}
                        title="预览"
                      >
                        <img src={image.image_url} />
                      </button>
                      <div className="result-actions">
                        <button
                          onClick={() => {
                            setPreviewImage(image.image_url);
                            setPreviewScale(1);
                          }}
                        >
                          预览
                        </button>
                        <button onClick={() => setSelectedResult(image)}>选为下一轮</button>
                        <button onClick={() => openCrop(image)}>
                          <Crop size={15} />
                          裁剪
                        </button>
                        <a href={image.image_url} download={`result_${jobIndex + 1}_${index + 1}.png`}>
                          <Download size={15} />
                          下载
                        </a>
                      </div>
                    </figure>
                  ))}
                </div>
              </article>
            ))}
          </div>
          {resultJobs.some((job) => job?.results?.length > 0) && (
            <div className="continue-box">
              <h3>连续修改</h3>
              <p>选择上面一张图，输入下一轮要求后继续生成。</p>
              <label className="mini-upload">
                <UploadCloud size={18} />
                <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => handleContinueUpload(event.target.files)} />
                <span>{continueUploadedImages.length ? `已补充 ${continueUploadedImages.length} 张图片` : "补充上传图片，可多选"}</span>
              </label>
              {continueUploadedImages.length > 0 && (
                <div className="mini-preview-row">
                  {continueUploadedImages.map((image) => (
                    <img key={image.image_id} src={image.preview_url} title={image.file_name} />
                  ))}
                </div>
              )}
              <label>本轮 API 配置</label>
              <select value={continueApiConfigId} onChange={(event) => setContinueApiConfigId(Number(event.target.value))}>
                {configs.map((config) => (
                  <option key={config.id} value={config.id}>
                    {config.config_name} {config.config_name?.trim() === "快速" ? "(默认)" : ""}
                  </option>
                ))}
              </select>
              <textarea rows={4} value={continueText} onChange={(event) => setContinueText(event.target.value)} placeholder="例如：保持包型不变，把肩带改成黑色，五金改成哑光银色。" />
              <button className="primary" onClick={continueModify} disabled={loading}>
                继续修改
              </button>
            </div>
          )}
          {conversation.length > 0 && (
            <div className="conversation-list">
              <h3>对话记录</h3>
              {conversation.map((item, index) => (
                <article key={`${item.role}-${index}`}>
                  <strong>
                    {item.role} {index + 1}
                  </strong>
                  <p>{item.text}</p>
                  {item.imageUrl && (
                    <>
                      <img src={item.imageUrl} />
                      <a href={item.imageUrl} download={`conversation_${index + 1}.png`}>
                        <Download size={15} />
                        下载
                      </a>
                    </>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>
      </div>

      {previewImage && (
        <div className="image-lightbox" role="dialog" aria-modal="true">
          <div className="lightbox-toolbar">
            <button onClick={() => setPreviewScale((value) => Math.max(0.5, Number((value - 0.25).toFixed(2))))} title="缩小">
              <ZoomOut size={18} />
            </button>
            <button onClick={() => setPreviewScale(1)}>{Math.round(previewScale * 100)}%</button>
            <button onClick={() => setPreviewScale((value) => Math.min(3, Number((value + 0.25).toFixed(2))))} title="放大">
              <ZoomIn size={18} />
            </button>
            <button onClick={() => setPreviewImage(null)} title="关闭">
              <X size={18} />
            </button>
          </div>
          <div className="lightbox-stage" onClick={() => setPreviewImage(null)}>
            <img src={previewImage} style={{ transform: `scale(${previewScale})` }} onClick={(event) => event.stopPropagation()} />
          </div>
        </div>
      )}

      {cropTarget && (
        <div className="crop-modal" role="dialog" aria-modal="true" aria-label="裁剪图片">
          <div className="crop-dialog">
            <div className="panel-title-row">
              <div>
                <h2>裁剪图片</h2>
                <p>在图片上拖动框选裁剪区域，或直接自动切成四张。</p>
              </div>
              <button onClick={() => setCropTarget(null)} title="关闭">
                <X size={18} />
              </button>
            </div>
            <div
              className="crop-image-stage"
              ref={cropStageRef}
              onPointerDown={startCropSelection}
              onPointerMove={moveCropSelection}
              onPointerUp={finishCropSelection}
              onPointerCancel={finishCropSelection}
            >
              <img src={cropTarget.image_url} draggable={false} />
              {cropRect && (
                <div
                  className="crop-selection"
                  style={{
                    left: `${cropRect.left * 100}%`,
                    top: `${cropRect.top * 100}%`,
                    width: `${(cropRect.right - cropRect.left) * 100}%`,
                    height: `${(cropRect.bottom - cropRect.top) * 100}%`
                  }}
                />
              )}
            </div>
            {cropRect && <div className="status success">已选择裁剪区域</div>}
            {cropError && <div className="notice">{cropError}</div>}
            <div className="crop-actions">
              <button onClick={splitGridImage} disabled={cropBusy}>
                <Crop size={16} />
                自动切四张
              </button>
              <button className="primary" onClick={saveManualCrop} disabled={cropBusy || !cropRect}>
                <Save size={16} />
                {cropBusy ? "处理中" : "保存裁剪"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Input({
  label,
  value,
  onChange,
  type = "text",
  hint
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  hint?: string;
}) {
  return (
    <label className="field">
      <span className="field-label">
        {label}
        {hint && <Hint text={hint} />}
      </span>
      <input type={type} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function Hint({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className={`hint-wrap ${open ? "open" : ""}`}>
      <button type="button" className="hint-button" onClick={() => setOpen((value) => !value)} title={text}>
        <HelpCircle size={14} />
      </button>
      <span className="hint-popover">{text}</span>
    </span>
  );
}
