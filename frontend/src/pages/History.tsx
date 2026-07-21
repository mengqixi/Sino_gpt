import { Archive, Download, Images, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { taskLabel } from "../types";
import "./History.css";

const PRODUCT_OUTPUTS = [
  { role: "front_transparent", label: "正面透明底" },
  { role: "front_main", label: "正面主图" },
  { role: "back", label: "背面图" },
  { role: "semi_side", label: "半侧面" },
  { role: "top", label: "顶部开口图" },
  { role: "logo_detail", label: "Logo 细节" }
] as const;

type ProductOutputRole = (typeof PRODUCT_OUTPUTS)[number]["role"];

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    unknown: "结果未知，可能已扣费",
    success: "成功",
    failed: "失败",
    running: "生成中",
    draft: "待上传",
    uploading: "上传中",
    analyzing: "素材分析中",
    analysis_failed: "分析失败",
    analysis_unknown: "分析结果未知",
    needs_input: "待补充素材",
    needs_material: "待补充素材",
    ready: "待生成",
    ready_to_generate: "待生成",
    generating: "生成中",
    paused: "已暂停",
    paused_failed: "生成失败，已暂停",
    paused_unknown: "结果未知，已暂停",
    paused_late_success: "检测到迟到结果，已暂停",
    completed: "已完成"
  };
  return labels[status] || status || "状态未知";
}

function statusTone(status: string) {
  if (["success", "completed"].includes(status)) return "success";
  if (["failed", "analysis_failed", "paused_failed"].includes(status)) return "failed";
  if (["unknown", "analysis_unknown", "paused_unknown", "paused_late_success"].includes(status)) return "unknown";
  if (["running", "analyzing", "generating", "uploading"].includes(status)) return "running";
  return "";
}

function historyList(value: any): any[] {
  if (Array.isArray(value)) return value;
  for (const key of ["items", "tasks", "history", "results"]) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  return [];
}

function firstUrl(...values: any[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function outputUrl(value: any): string | null {
  if (!value) return null;
  if (typeof value === "string") return value;
  return firstUrl(
    value.size_800_url,
    value.url_800,
    value.preview_url,
    value.thumbnail_url,
    value.file_url,
    value.image_url,
    value.url,
    typeof value["800"] === "string" ? value["800"] : null,
    value.current_result?.size_800_url,
    value.current_result?.file_url,
    value.current_result?.original_url,
    value.variants?.["800"]?.file_url,
    value.variants?.["800"]?.url,
    value.variants?.highres?.file_url,
    value.variants?.highres?.url,
    value.original_url
  );
}

function normalizeRole(value: unknown): ProductOutputRole | null {
  const role = String(value || "");
  const aliases: Record<string, ProductOutputRole> = {
    front_transparent: "front_transparent",
    transparent_front: "front_transparent",
    transparent: "front_transparent",
    front_main: "front_main",
    front: "front_main",
    back: "back",
    semi_side: "semi_side",
    half_side: "semi_side",
    top: "top",
    top_open: "top",
    logo_detail: "logo_detail",
    logo: "logo_detail"
  };
  return aliases[role] || null;
}

function previewMap(item: any): Partial<Record<ProductOutputRole, string>> {
  const previews: Partial<Record<ProductOutputRole, string>> = {};
  const assign = (roleValue: unknown, value: any) => {
    const role = normalizeRole(roleValue);
    const url = outputUrl(value);
    if (role && url) previews[role] = url;
  };

  const previewUrls = item?.preview_urls ?? item?.previews ?? item?.thumbnails;
  if (Array.isArray(previewUrls)) {
    previewUrls.forEach((value, index) => {
      const role = normalizeRole(value?.slot ?? value?.role ?? value?.output_role) || PRODUCT_OUTPUTS[index]?.role;
      if (role) assign(role, value);
    });
  } else if (previewUrls && typeof previewUrls === "object") {
    Object.entries(previewUrls).forEach(([role, value]) => assign(role, value));
  }

  const outputs = item?.outputs;
  if (Array.isArray(outputs)) {
    outputs.forEach((value) => assign(value?.slot ?? value?.role ?? value?.output_role, value));
  } else if (outputs && typeof outputs === "object") {
    Object.entries(outputs).forEach(([role, value]) => assign(role, value));
  }

  return previews;
}

function formatTime(value: unknown) {
  if (!value) return "时间未记录";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function productTaskId(item: any) {
  return String(item?.id ?? item?.task_id ?? "");
}

function productTitle(item: any) {
  const productCode = item?.product_code || item?.sku || item?.style_number || "未填写款号";
  const color = item?.color || item?.colour || "未填写颜色";
  const version = Number(item?.version ?? item?.task_version ?? 1) || 1;
  return `${productCode} · ${color} · 第 ${version} 版`;
}

function productGroupKey(item: any) {
  const productCode = String(item?.product_code || item?.sku || item?.style_number || "未填写款号").trim();
  const color = String(item?.color || item?.colour || "未填写颜色").trim();
  return `${productCode.toLocaleLowerCase()}\u0000${color.toLocaleLowerCase()}`;
}

function productGroupTitle(item: any) {
  const productCode = item?.product_code || item?.sku || item?.style_number || "未填写款号";
  const color = item?.color || item?.colour || "未填写颜色";
  return `${productCode} · ${color}`;
}

function groupProductHistory(items: any[]) {
  const groups = new Map<string, { key: string; title: string; items: any[]; latest: number }>();
  items.forEach((item) => {
    const key = productGroupKey(item);
    const timestamp = new Date(String(item?.created_at || item?.updated_at || 0)).getTime() || 0;
    const group = groups.get(key) || { key, title: productGroupTitle(item), items: [], latest: 0 };
    group.items.push(item);
    group.latest = Math.max(group.latest, timestamp);
    groups.set(key, group);
  });
  return Array.from(groups.values())
    .map((group) => ({
      ...group,
      items: group.items.sort((left, right) => {
        const versionDifference = Number(right?.version || 1) - Number(left?.version || 1);
        if (versionDifference) return versionDifference;
        return String(right?.created_at || "").localeCompare(String(left?.created_at || ""));
      })
    }))
    .sort((left, right) => right.latest - left.latest);
}

function productZipUrl(item: any) {
  return firstUrl(item?.zip_url, item?.download_url, item?.archive_url);
}

export default function HistoryPage() {
  const [items, setItems] = useState<any[]>([]);
  const [productItems, setProductItems] = useState<any[]>([]);
  const [message, setMessage] = useState("");
  const [deletingKey, setDeletingKey] = useState("");
  const [brokenPreviews, setBrokenPreviews] = useState<Record<string, boolean>>({});
  const productGroups = useMemo(() => groupProductHistory(productItems), [productItems]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, []);

  async function load() {
    const [legacyResult, productResult] = await Promise.allSettled([
      api.getHistory(),
      api.getProductImageHistory()
    ]);
    const errors: string[] = [];

    if (legacyResult.status === "fulfilled") {
      setItems(historyList(legacyResult.value));
    } else {
      errors.push(`AI 生图历史：${legacyResult.reason?.message || "加载失败"}`);
    }

    if (productResult.status === "fulfilled") {
      setProductItems(historyList(productResult.value));
    } else {
      errors.push(`商品图任务：${productResult.reason?.message || "加载失败"}`);
    }

    if (errors.length) setMessage(errors.join("；"));
  }

  async function remove(id: number) {
    if (!window.confirm("确认删除这条 AI 生图记录及其结果吗？")) return;
    const key = `legacy-${id}`;
    setDeletingKey(key);
    try {
      await api.deleteJob(id);
      setMessage("已删除 AI 生图记录");
      await load();
    } catch (error: any) {
      setMessage(error?.message || "删除失败");
    } finally {
      setDeletingKey("");
    }
  }

  async function removeProductTask(item: any) {
    const id = productTaskId(item);
    if (!id) {
      setMessage("任务编号缺失，无法删除");
      return;
    }
    if (!window.confirm(`确认删除“${productTitle(item)}”整组任务及六张结果吗？此操作无法撤销。`)) return;
    const key = `product-${id}`;
    setDeletingKey(key);
    try {
      await api.deleteProductImageTask(id);
      setProductItems((current) => current.filter((entry) => productTaskId(entry) !== id));
      setMessage("已删除整组商品图任务");
    } catch (error: any) {
      setMessage(error?.message || "删除整组任务失败");
    } finally {
      setDeletingKey("");
    }
  }

  return (
    <div className="page history-page">
      <header className="page-header">
        <h1>历史记录</h1>
        <p>查看商品图整组任务，以及原有 AI 生图使用的提示词、API 配置和结果。</p>
      </header>
      {message && <div className="notice">{message}</div>}

      <section className="history-section" aria-labelledby="product-history-title">
        <div className="history-section-heading">
          <div>
            <Images size={19} aria-hidden="true" />
            <h2 id="product-history-title">商品图任务</h2>
          </div>
          <span>{productGroups.length} 组 · {productItems.length} 个版本</span>
        </div>
        <div className="history-list product-history-list">
          {productGroups.map((group) => (
            <article className="product-history-group" key={group.key}>
              <header className="product-history-group-heading">
                <div><strong>{group.title}</strong><small>同款同色的全部生成版本</small></div>
                <span>{group.items.length} 个版本</span>
              </header>
              <div className="product-history-versions">
                {group.items.map((item) => {
                  const id = productTaskId(item);
                  const status = String(item?.internal_status || item?.status || "");
                  const previews = previewMap(item);
                  const zipUrl = productZipUrl(item);
                  const generatedCount = Number(item?.generated_count ?? item?.output_count ?? Object.keys(previews).length) || 0;
                  const zipReady = Boolean(item?.zip_url) || status === "completed" || generatedCount >= PRODUCT_OUTPUTS.length;
                  const cacheVersion = String(item?.updated_at || item?.created_at || "");
                  const version = Number(item?.version ?? item?.task_version ?? 1) || 1;

                  return (
                    <section className="history-item product-history-item" key={id || productTitle(item)}>
                      <div className="product-history-media">
                        {PRODUCT_OUTPUTS.map(({ role, label }) => {
                          const url = previews[role];
                          const previewKey = `${id}-${role}-${url || "empty"}-${cacheVersion}`;
                          return (
                            <figure key={role}>
                              {url && !brokenPreviews[previewKey] ? (
                                <img
                                  src={url}
                                  alt={`${productTitle(item)} ${label}`}
                                  loading="lazy"
                                  onError={() => setBrokenPreviews((current) => ({ ...current, [previewKey]: true }))}
                                />
                              ) : (
                                <div className="product-history-placeholder" aria-label={`${label}暂无缩略图`}>
                                  <Images size={18} aria-hidden="true" />
                                  <span>暂无</span>
                                </div>
                              )}
                              <figcaption>{label}</figcaption>
                            </figure>
                          );
                        })}
                      </div>
                      <div className="history-body product-history-body">
                        <div className="history-title">
                          <strong>第 {version} 版</strong>
                          <span className={`status ${statusTone(status)}`}>{statusLabel(status)}</span>
                        </div>
                        <p className="product-history-summary">
                          已生成 {generatedCount}/{PRODUCT_OUTPUTS.length} 张
                          {item?.inputs_deleted || item?.source_deleted_at ? " · 原始素材已清理" : ""}
                        </p>
                        <small>创建于 {formatTime(item?.created_at)}</small>
                        {item?.error_message && <div className="notice">{item.error_message}</div>}
                        <div className="toolbar">
                          {zipUrl && zipReady ? (
                            <a href={zipUrl} download>
                              <Archive size={15} />
                              下载本版 ZIP
                            </a>
                          ) : (
                            <button disabled title="六张商品图齐全后可下载 ZIP">
                              <Archive size={15} />
                              ZIP 尚未就绪
                            </button>
                          )}
                          <button
                            onClick={() => void removeProductTask(item)}
                            disabled={deletingKey === `product-${id}`}
                          >
                            <Trash2 size={15} />
                            {deletingKey === `product-${id}` ? "删除中…" : "删除本版"}
                          </button>
                        </div>
                      </div>
                    </section>
                  );
                })}
              </div>
            </article>
          ))}
          {!productGroups.length && <div className="empty">暂无商品图任务</div>}
        </div>
      </section>

      <section className="history-section" aria-labelledby="legacy-history-title">
        <div className="history-section-heading">
          <div>
            <Images size={19} aria-hidden="true" />
            <h2 id="legacy-history-title">AI 生图记录</h2>
          </div>
          <span>{items.length} 条</span>
        </div>
        <div className="history-list">
          {items.map((item) => (
            <article className="history-item" key={item.job_id}>
              <div className="history-media">
                {item.results?.slice(0, 4).map((image: any, index: number) => (
                  <img key={image.id ?? index} src={image.image_url} alt={`AI 生图结果 ${index + 1}`} loading="lazy" />
                ))}
              </div>
              <div className="history-body">
                <div className="history-title">
                  <strong>{taskLabel(item.task_type)}</strong>
                  <span className={`status ${statusTone(String(item.status || ""))}`}>{statusLabel(item.status)}</span>
                </div>
                <p>{item.final_prompt}</p>
                <small>
                  {item.api_config_name || "未知配置"} / {item.model_name || "未记录模型"} / {item.image_size} / {formatTime(item.created_at)}
                </small>
                {item.error_message && <div className="notice">{item.error_message}</div>}
                <div className="toolbar">
                  {item.results?.map((image: any, index: number) => (
                    <a key={image.id ?? index} href={image.image_url} download>
                      <Download size={15} />
                      下载 {index + 1}
                    </a>
                  ))}
                  <button onClick={() => void remove(item.job_id)} disabled={deletingKey === `legacy-${item.job_id}`}>
                    <Trash2 size={15} />
                    {deletingKey === `legacy-${item.job_id}` ? "删除中…" : "删除"}
                  </button>
                </div>
              </div>
            </article>
          ))}
          {!items.length && <div className="empty">暂无 AI 生图记录</div>}
        </div>
      </section>
    </div>
  );
}
