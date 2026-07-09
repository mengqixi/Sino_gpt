const API_BASE = "";

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(API_BASE + url, {
    ...options,
    headers: options.body instanceof FormData ? options.headers : { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
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
  getApiConfigs: () => request<any[]>("/api/api-configs"),
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
  applyRecolor: (payload: any) => request<any>("/api/recolor/apply", { method: "POST", body: JSON.stringify(payload) }),
  reuseRecolor: (payload: any) => request<any>("/api/recolor/reuse", { method: "POST", body: JSON.stringify(payload) }),
  generate: (payload: any) => request<any>("/api/generate", { method: "POST", body: JSON.stringify(payload) }),
  reuseGeneratedImage: (id: number) => request<any>(`/api/generated-images/${id}/reuse`, { method: "POST" }),
  getHistory: () => request<any[]>("/api/history"),
  deleteJob: (id: number) => request(`/api/jobs/${id}`, { method: "DELETE" })
};
