const API_BASE = "";

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(API_BASE + url, {
    ...options,
    headers: options.body instanceof FormData ? options.headers : { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const text = await response.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 300) };
    }
  }
  if (!response.ok) {
    throw new Error(data?.detail || "请求失败");
  }
  return data as T;
}

export const api = {
  getPrompts: (taskType?: string) => request<any[]>(`/api/prompts${taskType ? `?task_type=${taskType}` : ""}`),
  createPrompt: (payload: any) => request<any>("/api/prompts", { method: "POST", body: JSON.stringify(payload) }),
  updatePrompt: (id: number, payload: any) => request<any>(`/api/prompts/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deletePrompt: (id: number) => request(`/api/prompts/${id}`, { method: "DELETE" }),
  setPromptDefault: (id: number) => request(`/api/prompts/${id}/set-default`, { method: "POST" }),
  restorePrompt: (id: number) => request(`/api/prompts/${id}/restore`, { method: "POST" }),
  renderPrompt: (payload: any) => request<{ final_prompt: string }>("/api/prompts/render", { method: "POST", body: JSON.stringify(payload) }),
  getApiConfigs: (apiType?: "image_generation" | "text_analysis") =>
    request<any[]>(`/api/api-configs${apiType ? `?api_type=${apiType}` : ""}`),
  getEcommerceTemplates: () => request<any[]>("/api/ecommerce/templates"),
  planEcommerceCampaign: (payload: any) => request<any>("/api/ecommerce/plan", { method: "POST", body: JSON.stringify(payload) }),
  analyzeVipOrganizer: (payload: any) => request<any>("/api/vip-organizer/analyze", { method: "POST", body: JSON.stringify(payload) }),
  analyzeVipOrganizerWithApi: (payload: any) => request<any>("/api/vip-organizer/analyze-with-api", { method: "POST", body: JSON.stringify(payload) }),
  getVipAnalysisConfig: () => request<any>("/api/vip-organizer/analysis-config"),
  previewVipOrganizer: (payload: any) => request<any>("/api/vip-organizer/preview", { method: "POST", body: JSON.stringify(payload) }),
  previewVipOrganizerSlot: (payload: any) => request<any>("/api/vip-organizer/preview-slot", { method: "POST", body: JSON.stringify(payload) }),
  exportVipOrganizer: (payload: any) => request<any>("/api/vip-organizer/export", { method: "POST", body: JSON.stringify(payload) }),
  startVipOrganizerSession: (previousSessionId?: string) => request<{ session_id: string }>("/api/vip-organizer/session", {
    method: "POST",
    body: JSON.stringify({ previous_session_id: previousSessionId || null })
  }),
  uploadVipOrganizerAssets: (sessionId: string, assetType: string, files: File[]) => {
    const form = new FormData();
    form.append("session_id", sessionId);
    form.append("asset_type", assetType);
    files.forEach((file) => form.append("files", file));
    return request<any[]>("/api/vip-organizer/upload", { method: "POST", body: form });
  },
  createApiConfig: (payload: any) => request<any>("/api/api-configs", { method: "POST", body: JSON.stringify(payload) }),
  updateApiConfig: (id: number, payload: any) => request<any>(`/api/api-configs/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteApiConfig: (id: number) => request(`/api/api-configs/${id}`, { method: "DELETE" }),
  setApiConfigDefault: (id: number) => request(`/api/api-configs/${id}/set-default`, { method: "POST" }),
  testApiConfig: (id: number) => request<any>(`/api/api-configs/${id}/test`, { method: "POST" }),
  uploadImage: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<any>("/api/upload", { method: "POST", body: form });
  },
  uploadImages: (files: File[]) => Promise.all(files.map((file) => api.uploadImage(file))),
  analyzeRecolor: (payload: any) => request<any>("/api/recolor/analyze", { method: "POST", body: JSON.stringify(payload) }),
  selectRecolorHardware: (payload: any) => request<any>("/api/recolor/select", { method: "POST", body: JSON.stringify(payload) }),
  previewRecolor: (payload: any) => request<any>("/api/recolor/preview", { method: "POST", body: JSON.stringify(payload) }),
  applyRecolor: (payload: any) => request<any>("/api/recolor/apply", { method: "POST", body: JSON.stringify(payload) }),
  reuseRecolor: (payload: any) => request<any>("/api/recolor/reuse", { method: "POST", body: JSON.stringify(payload) }),
  generate: (payload: any) => request<any>("/api/generate", { method: "POST", body: JSON.stringify(payload) }),
  getJob: (id: number) => request<any>(`/api/jobs/${id}`),
  reuseGeneratedImage: (id: number) => request<any>(`/api/generated-images/${id}/reuse`, { method: "POST" }),
  splitGeneratedImage: (id: number) => request<any>(`/api/generated-images/${id}/split-grid`, { method: "POST" }),
  cropGeneratedImage: (id: number, payload: { left: number; top: number; right: number; bottom: number }) =>
    request<any>(`/api/generated-images/${id}/crop`, { method: "POST", body: JSON.stringify(payload) }),
  getHistory: () => request<any[]>("/api/history"),
  deleteJob: (id: number) => request(`/api/jobs/${id}`, { method: "DELETE" })
};
