import { Download, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { taskLabel } from "../types";

function statusLabel(status: string) {
  if (status === "unknown") return "结果未知，可能已扣费";
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  if (status === "running") return "生成中";
  return status;
}

export default function HistoryPage() {
  const [items, setItems] = useState<any[]>([]);
  const [message, setMessage] = useState("");

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, []);

  async function load() {
    try {
      setItems(await api.getHistory());
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function remove(id: number) {
    await api.deleteJob(id);
    setMessage("已删除任务");
    await load();
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>历史记录</h1>
        <p>查看每次生成使用的最终提示词、API 配置和结果图片。</p>
      </header>
      {message && <div className="notice">{message}</div>}
      <section className="history-list">
        {items.map((item) => (
          <article className="history-item" key={item.job_id}>
            <div className="history-media">
              {item.results?.slice(0, 4).map((image: any, index: number) => (
                <img key={index} src={image.image_url} />
              ))}
            </div>
            <div className="history-body">
              <div className="history-title">
                <strong>{taskLabel(item.task_type)}</strong>
                <span className={`status ${item.status}`}>{statusLabel(item.status)}</span>
              </div>
              <p>{item.final_prompt}</p>
              <small>
                {item.api_config_name || "未知配置"} / {item.model_name || "未记录模型"} / {item.image_size} / {item.created_at}
              </small>
              {item.error_message && <div className="notice">{item.error_message}</div>}
              <div className="toolbar">
                {item.results?.map((image: any, index: number) => (
                  <a key={index} href={image.image_url} download>
                    <Download size={15} />
                    下载 {index + 1}
                  </a>
                ))}
                <button onClick={() => remove(item.job_id)}>
                  <Trash2 size={15} />
                  删除
                </button>
              </div>
            </div>
          </article>
        ))}
        {!items.length && <div className="empty">暂无历史记录</div>}
      </section>
    </div>
  );
}
