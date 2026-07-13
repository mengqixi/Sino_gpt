import { Copy, RotateCcw, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { TASKS, VARIABLES, taskLabel } from "../types";
import { copyText } from "../utils/clipboard";

export default function PromptTemplates() {
  const [taskType, setTaskType] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    load();
  }, [taskType]);

  async function load() {
    const rows = await api.getPrompts(taskType || undefined);
    setItems(rows);
    setSelected(rows[0] || null);
  }

  async function save() {
    if (!selected) return;
    const payload = {
      name: selected.name,
      task_type: selected.task_type,
      template_content: selected.template_content,
      variables: selected.variables || [],
      is_default: selected.is_default
    };
    if (selected.id) {
      const updated = await api.updatePrompt(selected.id, payload);
      setSelected(updated);
      setMessage("已保存模板");
    } else {
      const created = await api.createPrompt(payload);
      setSelected(created);
      setMessage("已新增模板");
    }
    await load();
  }

  async function saveCopy() {
    if (!selected) return;
    const created = await api.createPrompt({
      name: `${selected.name}-副本`,
      task_type: selected.task_type,
      template_content: selected.template_content,
      variables: selected.variables || [],
      is_default: false
    });
    await load();
    setSelected(created);
  }

  async function remove() {
    if (!selected?.id) return;
    await api.deletePrompt(selected.id);
    setMessage("已删除模板");
    await load();
  }

  async function restore() {
    if (!selected?.id) return;
    await api.restorePrompt(selected.id);
    setMessage("已恢复系统默认内容");
    await load();
  }

  function insertVariable(variable: string) {
    setSelected({ ...selected, template_content: `${selected.template_content || ""}{{${variable}}}` });
  }

  async function copyTemplate() {
    const copied = await copyText(selected?.template_content || "");
    setMessage(copied ? "提示词已复制" : "提示词为空或复制失败");
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>提示词管理</h1>
        <p>模板内容可以自由修改，变量按钮只是便捷插入，不限制最终提示词。</p>
      </header>
      <div className="manage-layout">
        <section className="panel list-panel">
          <div className="panel-title-row">
            <h2>模板列表</h2>
            <button onClick={() => setSelected({ name: "新提示词模板", task_type: "color_change", template_content: "", variables: [] })}>
              新增
            </button>
          </div>
          <select value={taskType} onChange={(event) => setTaskType(event.target.value)}>
            <option value="">全部类型</option>
            {TASKS.map((task) => (
              <option key={task.value} value={task.value}>
                {task.label}
              </option>
            ))}
          </select>
          <div className="template-list">
            {items.map((item) => (
              <button key={item.id} className={selected?.id === item.id ? "selected" : ""} onClick={() => setSelected(item)}>
                <strong>{item.name}</strong>
                <span>
                  {taskLabel(item.task_type)} {item.is_default ? "默认" : ""} {item.is_system ? "系统" : ""}
                </span>
              </button>
            ))}
          </div>
        </section>
        <section className="panel editor-panel">
          {selected ? (
            <>
              <div className="panel-title-row">
                <h2>编辑模板</h2>
                <div className="toolbar compact">
                  <button onClick={copyTemplate} disabled={!selected.template_content}>
                    <Copy size={16} />
                    复制
                  </button>
                  <button onClick={saveCopy}>另存</button>
                  {selected.is_system && (
                    <button onClick={restore}>
                      <RotateCcw size={16} />
                      恢复
                    </button>
                  )}
                  {!selected.is_system && selected.id && (
                    <button onClick={remove}>
                      <Trash2 size={16} />
                      删除
                    </button>
                  )}
                  <button className="primary" onClick={save}>
                    <Save size={16} />
                    保存
                  </button>
                </div>
              </div>
              {message && <div className="notice">{message}</div>}
              <div className="inline-fields">
                <Input label="模板名称" value={selected.name || ""} onChange={(v) => setSelected({ ...selected, name: v })} />
                <label className="field">
                  <span>任务类型</span>
                  <select value={selected.task_type} onChange={(event) => setSelected({ ...selected, task_type: event.target.value })}>
                    {TASKS.map((task) => (
                      <option key={task.value} value={task.value}>
                        {task.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="check-row">
                <input type="checkbox" checked={!!selected.is_default} onChange={(event) => setSelected({ ...selected, is_default: event.target.checked })} />
                设为默认模板
              </label>
              <div className="variable-row">
                {VARIABLES.map((variable) => (
                  <button key={variable} onClick={() => insertVariable(variable)}>
                    {`{{${variable}}}`}
                  </button>
                ))}
              </div>
              <textarea className="template-textarea" value={selected.template_content || ""} onChange={(event) => setSelected({ ...selected, template_content: event.target.value })} />
            </>
          ) : (
            <div className="empty">暂无模板</div>
          )}
        </section>
      </div>
    </div>
  );
}

function Input({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
