import { Image, MessageSquareText, Plus, Save, TestTube2, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { api } from "../api/client";

const sharedDefaults = {
  api_key: "",
  method: "POST",
  auth_type: "bearer",
  auth_header_name: "Authorization",
  auth_header_prefix: "Bearer",
  model_field_name: "model",
  extra_params_json: "{}",
  response_text_path: "choices.0.message.content",
  timeout_seconds: 350,
  enabled: true,
  is_default: false
};

const imageDefaults = {
  ...sharedDefaults,
  config_name: "新生图配置",
  api_type: "image_generation",
  api_base_url: "https://your-relay-domain.com",
  model_name: "gpt-image-2",
  endpoint_path: "/v1/images/edits",
  request_content_type: "multipart/form-data",
  image_field_name: "image",
  prompt_field_name: "prompt",
  count_field_name: "n",
  size_field_name: "size",
  quality_field_name: "quality",
  response_image_type: "base64",
  response_image_path: "data.0.b64_json"
};

const textDefaults = {
  ...sharedDefaults,
  config_name: "新文本分析配置",
  api_type: "text_analysis",
  api_base_url: "https://n.tokeness.io/v1",
  model_name: "gpt-5.6-sol",
  endpoint_path: "/chat/completions",
  request_content_type: "application/json",
  image_field_name: "image",
  prompt_field_name: "prompt",
  count_field_name: "n",
  size_field_name: "size",
  quality_field_name: "quality",
  response_image_type: "base64",
  response_image_path: "data.0.b64_json"
};

function typeLabel(type: string) {
  return type === "text_analysis" ? "文本分析 API" : "生图 API";
}

export default function ApiConfigs() {
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [message, setMessage] = useState("");

  useEffect(() => { load(); }, []);

  async function load(preferredId?: number) {
    const rows = await api.getApiConfigs();
    setItems(rows);
    setSelected(rows.find((item) => item.id === preferredId) || rows[0] || imageDefaults);
  }

  async function save() {
    const payload = { ...selected };
    delete payload.id;
    delete payload.api_key_masked;
    delete payload.created_at;
    delete payload.updated_at;
    if (!payload.api_key) delete payload.api_key;
    const saved = selected.id
      ? await api.updateApiConfig(selected.id, payload)
      : await api.createApiConfig(payload);
    setMessage(selected.id ? "已保存 API 配置" : "已新增 API 配置");
    await load(saved.id);
  }

  async function remove() {
    if (!selected?.id) return;
    await api.deleteApiConfig(selected.id);
    setMessage("已删除 API 配置");
    await load();
  }

  async function test() {
    if (!selected?.id) return setMessage("请先保存配置再测试");
    const result = await api.testApiConfig(selected.id);
    setMessage(result.message || "测试完成");
  }

  function update(key: string, value: any) {
    setSelected({ ...selected, [key]: value });
  }

  function renderGroup(title: string, type: string, icon: ReactNode) {
    const rows = items.filter((item) => (item.api_type || "image_generation") === type);
    return <div className="api-config-group">
      <h3>{icon}{title}<span>{rows.length}</span></h3>
      {rows.length === 0 && <p>暂无配置</p>}
      {rows.map((item) => (
        <button key={item.id} className={selected?.id === item.id ? "selected" : ""} onClick={() => setSelected(item)}>
          <strong>{item.config_name}</strong>
          <span>{item.model_name} {item.is_default ? "默认" : ""} {item.enabled ? "启用" : "停用"}</span>
          <small>{item.api_key_masked || "未配置 Key"}</small>
        </button>
      ))}
    </div>;
  }

  const isText = selected?.api_type === "text_analysis";

  return (
    <div className="page">
      <header className="page-header">
        <h1>API 设置</h1>
        <p>生图 API 仅供 AI 生图和电商生图使用；文本分析 API 仅供自动化整理分析素材。密钥只显示掩码。</p>
      </header>
      <div className="manage-layout">
        <section className="panel list-panel">
          <div className="panel-title-row"><h2>配置列表</h2></div>
          <div className="api-add-buttons">
            <button onClick={() => setSelected({ ...imageDefaults })}><Plus size={16} />新增生图</button>
            <button onClick={() => setSelected({ ...textDefaults })}><Plus size={16} />新增文本</button>
          </div>
          <div className="template-list">
            {renderGroup("生图 API", "image_generation", <Image size={16} />)}
            {renderGroup("文本分析 API", "text_analysis", <MessageSquareText size={16} />)}
          </div>
        </section>
        <section className="panel editor-panel">
          {selected && <>
            <div className="panel-title-row">
              <div><h2>编辑配置</h2><span className={`api-type-badge ${isText ? "text" : "image"}`}>{typeLabel(selected.api_type)}</span></div>
              <div className="toolbar compact">
                <button onClick={test}><TestTube2 size={16} />测试</button>
                {selected.id && <button onClick={remove}><Trash2 size={16} />删除</button>}
                <button className="primary" onClick={save}><Save size={16} />保存</button>
              </div>
            </div>
            {message && <div className="notice">{message}</div>}
            <div className="form-grid">
              <Input label="配置名称" value={selected.config_name || ""} onChange={(v) => update("config_name", v)} />
              <label className="field"><span>API 用途</span><select value={selected.api_type || "image_generation"} onChange={(event) => update("api_type", event.target.value)}><option value="image_generation">生图 API</option><option value="text_analysis">文本分析 API</option></select></label>
              <Input label="API Base URL" value={selected.api_base_url || ""} onChange={(v) => update("api_base_url", v)} />
              <Input label="API Key" type="password" placeholder={selected.api_key_masked || "输入后保存"} value={selected.api_key || ""} onChange={(v) => update("api_key", v)} />
              <Input label="模型名称" value={selected.model_name || ""} onChange={(v) => update("model_name", v)} />
              <Input label="接口路径" value={selected.endpoint_path || ""} onChange={(v) => update("endpoint_path", v)} />
              <Select label="请求方式" value={selected.method || "POST"} onChange={(v) => update("method", v)} options={["POST"]} />
              <Select label="请求内容类型" value={selected.request_content_type || (isText ? "application/json" : "multipart/form-data")} onChange={(v) => update("request_content_type", v)} options={isText ? ["application/json"] : ["multipart/form-data", "application/json"]} />
              <Select label="认证方式" value={selected.auth_type || "bearer"} onChange={(v) => update("auth_type", v)} options={["bearer", "raw", "none"]} />
              <Input label="认证 Header 名称" value={selected.auth_header_name || ""} onChange={(v) => update("auth_header_name", v)} />
              <Input label="认证 Header 前缀" value={selected.auth_header_prefix || ""} onChange={(v) => update("auth_header_prefix", v)} />
              <Input label="模型字段名" value={selected.model_field_name || "model"} onChange={(v) => update("model_field_name", v)} />
              {isText ? <Input label="返回文本字段路径" value={selected.response_text_path || ""} onChange={(v) => update("response_text_path", v)} /> : <>
                <Input label="图片字段名" value={selected.image_field_name || ""} onChange={(v) => update("image_field_name", v)} />
                <Input label="提示词字段名" value={selected.prompt_field_name || ""} onChange={(v) => update("prompt_field_name", v)} />
                <Input label="输出张数字段名" value={selected.count_field_name || ""} onChange={(v) => update("count_field_name", v)} />
                <Input label="尺寸字段名" value={selected.size_field_name || ""} onChange={(v) => update("size_field_name", v)} />
                <Input label="质量字段名" value={selected.quality_field_name || ""} onChange={(v) => update("quality_field_name", v)} />
                <Select label="返回图片类型" value={selected.response_image_type || "base64"} onChange={(v) => update("response_image_type", v)} options={["base64", "url"]} />
                <Input label="返回图片字段路径" value={selected.response_image_path || ""} onChange={(v) => update("response_image_path", v)} />
              </>}
              <Input label="超时时间（秒）" type="number" value={String(selected.timeout_seconds || 350)} onChange={(v) => update("timeout_seconds", Number(v || 350))} />
            </div>
            <label>额外参数 JSON</label>
            <textarea rows={5} value={selected.extra_params_json || "{}"} onChange={(event) => update("extra_params_json", event.target.value)} />
            <div className="check-row-group">
              <label className="check-row"><input type="checkbox" checked={!!selected.enabled} onChange={(event) => update("enabled", event.target.checked)} />是否启用</label>
              <label className="check-row"><input type="checkbox" checked={!!selected.is_default} onChange={(event) => update("is_default", event.target.checked)} />设为该用途的默认配置</label>
            </div>
          </>}
        </section>
      </div>
    </div>
  );
}

function Input({ label, value, onChange, type = "text", placeholder = "" }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return <label className="field"><span>{label}</span><input type={type} value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} /></label>;
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return <label className="field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option key={option} value={option}>{option}</option>)}</select></label>;
}
