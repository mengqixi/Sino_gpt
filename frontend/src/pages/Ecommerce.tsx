import { Check, Download, Eye, Images, LoaderCircle, Pause, UploadCloud } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

const BASIC_IDS = ["hero-image", "lifestyle-scene", "detail-macro", "model-showcase", "infographic", "size-spec", "multi-angle-grid", "magazine-editorial", "luxury-atmospherics"];
const SIZES = ["1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152", "1152x2048"];

function preferredConfig(rows: any[]) {
  const enabled = rows.filter((item) => item.enabled && (item.api_type || "image_generation") === "image_generation");
  return enabled.find((item) => item.config_name?.trim() === "快速") || enabled.find((item) => item.is_default) || enabled[0] || null;
}

export default function Ecommerce() {
  const [templates, setTemplates] = useState<any[]>([]);
  const [configs, setConfigs] = useState<any[]>([]);
  const [selected, setSelected] = useState<string[]>(BASIC_IDS);
  const [uploads, setUploads] = useState<any[]>([]);
  const [apiConfigId, setApiConfigId] = useState<number | "">("");
  const [form, setForm] = useState({
    product_name: "ELLE女包",
    product_description: "严格依据上传参考图，还原包型、颜色、材质、肩带、五金和Logo",
    selling_points: "法式设计、精致五金、实用容量、轻奢质感、日常易搭配",
    dimensions: "",
    platform: "ELLE箱包电商详情页",
    brand_positioning: "ELLE箱包，法式轻奢、优雅、现代、浪漫",
    audience: "重视设计感、品质和日常搭配的都市女性",
    palette: "黑、象牙白、酒红、深绿与低饱和金色点缀",
    visual_style: "自然柔光、真实材质、浪漫巴黎气质、干净高级的商业摄影",
    copy_text: "不虚构价格、销量、评价、认证和产品参数",
    extra_requirements: ""
  });
  const [sizes, setSizes] = useState<Record<string, string>>({});
  const [plan, setPlan] = useState<any>(null);
  const [editedPrompts, setEditedPrompts] = useState<Record<string, string>>({});
  const [jobs, setJobs] = useState<Record<string, any>>({});
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const [message, setMessage] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.getEcommerceTemplates(), api.getApiConfigs("image_generation")])
      .then(([templateRows, configRows]) => {
        setTemplates(templateRows);
        setConfigs(configRows.filter((item: any) => item.enabled));
        setSizes(Object.fromEntries(templateRows.map((item: any) => [item.id, item.default_size])));
        const preferred = preferredConfig(configRows);
        if (preferred) setApiConfigId(preferred.id);
      })
      .catch((error) => setMessage(error.message));
  }, []);

  const groups = useMemo(() => {
    const result: Record<string, any[]> = {};
    templates.forEach((item) => (result[item.category] ||= []).push(item));
    return result;
  }, [templates]);

  function updateForm(key: string, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
    setConfirmed(false);
  }

  function toggle(id: string) {
    setSelected((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
    setPlan(null);
    setConfirmed(false);
  }

  async function upload(files?: FileList | null) {
    const items = Array.from(files || []);
    if (!items.length) return;
    try {
      setMessage("");
      setUploads(await api.uploadImages(items));
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function createPlan() {
    try {
      setMessage("");
      const data = await api.planEcommerceCampaign({ ...form, template_ids: selected, sizes });
      setPlan(data);
      setEditedPrompts(Object.fromEntries(data.items.map((item: any) => [item.template_id, item.final_prompt])));
      setJobs({});
      setConfirmed(false);
      document.getElementById("ecommerce-plan")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  const completedCount = plan?.items.filter((item: any) => jobs[item.template_id]?.status === "success").length || 0;

  async function generateBatch() {
    if (!confirmed) return setMessage("请先确认提示词和调用次数");
    if (!uploads.length) return setMessage("请先上传至少一张ELLE箱包参考图");
    if (!apiConfigId) return setMessage("请选择API配置");
    setBusy(true);
    setMessage("");
    const nextJobs = { ...jobs };
    try {
      for (let index = 0; index < plan.items.length; index += 1) {
        const item = plan.items[index];
        if (nextJobs[item.template_id]?.status === "success") continue;
        setProgress(`正在生成 ${index + 1}/${plan.items.length}：${item.name}`);
        const data = await api.generate({
          task_type: "ecommerce",
          uploaded_image_id: uploads[0].image_id,
          uploaded_image_ids: uploads.map((image) => image.image_id),
          prompt_template_id: null,
          final_prompt: editedPrompts[item.template_id],
          api_config_id: apiConfigId,
          image_size: item.image_size,
          params: { ecommerce_template_id: item.template_id, ecommerce_template_name: item.name, campaign_brand: "ELLE箱包" }
        });
        nextJobs[item.template_id] = data.job;
        setJobs({ ...nextJobs });
        if (data.job?.status !== "success") {
          const reason = data.job?.error_message ? `原因：${data.job.error_message}` : "中转站没有返回可用图片。";
          setMessage(
            data.job?.status === "unknown"
              ? `“${item.name}”结果未知，批量任务已暂停，避免继续扣费。${reason}`
              : `“${item.name}”生成失败，批量任务已暂停。${reason}`
          );
          break;
        }
      }
    } catch (error: any) {
      setMessage(`${error.message}。批量任务已暂停。`);
    } finally {
      setBusy(false);
      setProgress("");
    }
  }

  return (
    <section className="page ecommerce-page">
      <header className="page-header">
        <h1>ELLE箱包电商生图</h1>
        <p>选择需要的电商图片模板，检查每张提示词和调用次数后，再按顺序批量生成。</p>
      </header>

      <div className="ecom-setup">
        <section className="panel ecom-source">
          <h2>产品参考图</h2>
          <label className="upload-zone compact">
            <UploadCloud size={24} />
            <span>{uploads.length ? `已上传 ${uploads.length} 张参考图` : "上传ELLE箱包参考图，可多选"}</span>
            <input type="file" accept="image/*" multiple onChange={(event) => upload(event.target.files)} />
          </label>
          {uploads.length > 0 && <div className="ecom-upload-grid">{uploads.map((item) => <img key={item.image_id} src={item.preview_url} alt={item.file_name} />)}</div>}
          <h2>产品信息</h2>
          <label>产品名称<input value={form.product_name} onChange={(event) => updateForm("product_name", event.target.value)} /></label>
          <label>产品描述<textarea rows={3} value={form.product_description} onChange={(event) => updateForm("product_description", event.target.value)} /></label>
          <label>核心卖点<textarea rows={3} value={form.selling_points} onChange={(event) => updateForm("selling_points", event.target.value)} /></label>
          <label>长宽高/容量<input placeholder="例如：长26 × 高18 × 宽9 cm" value={form.dimensions} onChange={(event) => updateForm("dimensions", event.target.value)} /></label>
          <label>使用渠道<input value={form.platform} onChange={(event) => updateForm("platform", event.target.value)} /></label>
        </section>

        <section className="panel ecom-style">
          <h2>整组视觉锁定</h2>
          <label>品牌定位<input value={form.brand_positioning} onChange={(event) => updateForm("brand_positioning", event.target.value)} /></label>
          <label>目标人群<input value={form.audience} onChange={(event) => updateForm("audience", event.target.value)} /></label>
          <label>品牌配色<input value={form.palette} onChange={(event) => updateForm("palette", event.target.value)} /></label>
          <label>画面风格<textarea rows={3} value={form.visual_style} onChange={(event) => updateForm("visual_style", event.target.value)} /></label>
          <label>文案规则<textarea rows={2} value={form.copy_text} onChange={(event) => updateForm("copy_text", event.target.value)} /></label>
          <label>额外要求<textarea rows={3} value={form.extra_requirements} onChange={(event) => updateForm("extra_requirements", event.target.value)} /></label>
          <label>API配置<select value={apiConfigId} onChange={(event) => setApiConfigId(Number(event.target.value))}>{configs.map((item) => <option key={item.id} value={item.id}>{item.config_name} / {item.model_name}</option>)}</select></label>
        </section>
      </div>

      <section className="panel ecom-templates">
        <div className="section-title-row">
          <div><h2>图片模板</h2><p>共25套。默认选择适合ELLE女包详情页的基础套图，实验模板也完整保留。</p></div>
          <div className="button-row">
            <button onClick={() => setSelected(BASIC_IDS)}>基础套图</button>
            <button onClick={() => setSelected(templates.map((item) => item.id))}>全选25套</button>
            <button onClick={() => setSelected([])}>清空</button>
          </div>
        </div>
        {Object.entries(groups).map(([category, items]) => (
          <div className="template-group" key={category}>
            <h3>{category}</h3>
            <div className="template-grid">
              {items.map((item) => (
                <article className={`template-option ${selected.includes(item.id) ? "selected" : ""}`} key={item.id}>
                  <button className="template-check" onClick={() => toggle(item.id)} aria-label={`选择${item.name}`}><span>{selected.includes(item.id) && <Check size={16} />}</span><strong>{item.name}</strong>{item.recommended && <small>推荐</small>}</button>
                  <p>{item.handbag_direction}</p>
                  <select value={sizes[item.id] || item.default_size} onChange={(event) => setSizes((current) => ({ ...current, [item.id]: event.target.value }))}>{SIZES.map((size) => <option key={size}>{size}</option>)}</select>
                </article>
              ))}
            </div>
          </div>
        ))}
        <div className="ecom-plan-bar"><strong>已选 {selected.length} 套，将调用 API {selected.length} 次</strong><button className="primary" disabled={!selected.length} onClick={createPlan}><Images size={18} />生成提示词方案</button></div>
      </section>

      {plan && <section className="panel ecom-plan" id="ecommerce-plan">
        <div className="section-title-row"><div><h2>确认提示词与调用次数</h2><p>可逐张修改。批量生成按下列顺序执行，异常时立即暂停。</p></div><strong>{plan.count} 张 / {plan.count} 次调用</strong></div>
        <div className="plan-list">{plan.items.map((item: any, index: number) => {
          const job = jobs[item.template_id];
          return <article className="plan-item" key={item.template_id}>
            <div className="plan-item-head"><div><span>{index + 1}</span><strong>{item.name}</strong><small>{item.image_size}</small></div>{job && <em className={`status ${job.status}`}>{job.status === "success" ? "成功" : job.status === "unknown" ? "结果未知" : "失败"}</em>}</div>
            <textarea rows={10} value={editedPrompts[item.template_id] || ""} onChange={(event) => { setEditedPrompts((current) => ({ ...current, [item.template_id]: event.target.value })); setConfirmed(false); }} />
            {job?.error_message && <div className="alert warning">{job.error_message}</div>}
            {job?.results?.length > 0 && <div className="ecom-results">{job.results.map((image: any) => <div key={image.id}><img src={image.image_url} alt={item.name} /><div><button onClick={() => setPreview(image.image_url)}><Eye size={16} />预览</button><a className="button-link" href={image.image_url} download><Download size={16} />下载</a></div></div>)}</div>}
          </article>;
        })}</div>
        <div className="batch-confirm"><label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />我已检查 {plan.count} 张提示词，确认将调用 API {plan.count} 次{completedCount > 0 && `（已成功 ${completedCount} 张，将自动跳过）`}</label><button className="primary" disabled={busy || !confirmed} onClick={generateBatch}>{busy ? <LoaderCircle className="spin" size={18} /> : <Check size={18} />}{busy ? progress : completedCount > 0 ? "从失败处继续生成" : "确认并开始批量生成"}</button></div>
      </section>}

      {message && <div className="alert warning"><Pause size={18} />{message}</div>}
      {preview && <div className="image-modal" onClick={() => setPreview(null)}><button onClick={() => setPreview(null)}>关闭</button><img src={preview} alt="电商图片预览" onClick={(event) => event.stopPropagation()} /></div>}
    </section>
  );
}
