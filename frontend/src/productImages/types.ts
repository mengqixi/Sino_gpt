export type ProductSourceRole = "front" | "back" | "semi_side" | "top" | "logo";

export type ProductOutputRole =
  | "front_transparent"
  | "front_main"
  | "back"
  | "semi_side"
  | "top"
  | "logo_detail";

export type ProductTaskStatus =
  | "draft"
  | "analyzing"
  | "analysis_failed"
  | "analysis_unknown"
  | "needs_material"
  | "ready"
  | "generating"
  | "paused"
  | "paused_unknown"
  | "completed"
  | "failed"
  | string;

export type ProductAssetMediaType = "image" | "video" | "frame";
export type ProductOutputStatus = "pending" | "running" | "success" | "failed" | "unknown" | "stale" | "needs_source";
export type ProductOutputVariantName = "highres" | "800";

export interface ProductImageAsset {
  id: number;
  slot: ProductSourceRole | "video";
  media_type: ProductAssetMediaType;
  file_name: string;
  file_url: string;
  file_size?: number | null;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  duration_seconds?: number | null;
  sharpness?: number | null;
  parent_asset_id?: number | null;
  frame_time_seconds?: number | null;
  analysis_role?: ProductSourceRole | null;
  analysis_valid?: boolean | null;
  analysis_confidence?: number | null;
  analysis_reason?: string | null;
  created_at?: string;
}

export interface ProductReference {
  role: ProductSourceRole;
  selected_asset_id?: number | null;
  status: "missing" | "ready" | "invalid" | string;
  selection_source?: "analysis" | "manual" | "supplemental" | string | null;
  confidence?: number | null;
  reason?: string | null;
  selected_asset_url?: string | null;
  file_name?: string | null;
  media_type?: ProductAssetMediaType | null;
  width?: number | null;
  height?: number | null;
  sharpness?: number | null;
}

export interface ProductOutputVariant {
  id: number;
  slot: ProductOutputRole;
  variant: ProductOutputVariantName | "raw";
  status: ProductOutputStatus;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  source_asset_id?: number | null;
  api_config_id?: number | null;
  prompt?: string | null;
  error_message?: string | null;
  file_url?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProductImageOutput {
  slot: ProductOutputRole;
  reference_role: ProductSourceRole;
  status: ProductOutputStatus;
  has_result: boolean;
  variants: Partial<Record<ProductOutputVariantName | "raw", ProductOutputVariant>>;
}

export interface ProductImageCall {
  id: number;
  call_type: "analysis" | "generation";
  slot?: ProductOutputRole | null;
  attempt_no?: number;
  status: "pending" | "running" | "success" | "failed" | "unknown";
  api_config_id?: number | null;
  api_config_name?: string | null;
  config_name?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string;
}

export interface ProductCallPlan {
  analysis_calls: number;
  generation_calls: number;
  maximum_total_calls: number;
  pending_generation_slots: ProductOutputRole[];
  unknown_retry_warning: boolean;
  unknown_request_still_running: boolean;
}

export interface ProductImageTask {
  id: string;
  product_code: string;
  color: string;
  version?: number;
  status: ProductTaskStatus;
  analysis_config_id?: number | null;
  image_config_id?: number | null;
  analysis_used: boolean;
  analysis_status?: string | null;
  missing_roles: ProductSourceRole[];
  analysis_notes?: Record<string, unknown>;
  error_message?: string | null;
  generation_active?: boolean;
  inputs_deleted: boolean;
  inputs_deleted_at?: string | null;
  references: ProductReference[];
  assets: ProductImageAsset[];
  outputs: ProductImageOutput[];
  calls: ProductImageCall[];
  download_url?: string | null;
  zip_url?: string | null;
  call_plan: ProductCallPlan;
  last_activity_at?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ProductImageTaskEnvelope {
  task: ProductImageTask;
}

export type ProductImageTaskResponse = ProductImageTask | ProductImageTaskEnvelope;
export type ProductMutationResponse = ProductImageTaskResponse | { ok: true } | null;

export interface ProductApiConfig {
  id: number;
  config_name: string;
  model_name?: string;
  api_type: "image_generation" | "text_analysis";
  enabled: boolean;
  is_default?: boolean;
}

export interface ProductImageApiClient {
  createProductImageTask(payload: { product_code: string; color: string; previous_task_id?: string | null }): Promise<ProductImageTaskResponse>;
  getProductImageTask(taskId: string): Promise<ProductImageTaskResponse>;
  uploadProductImages(taskId: string, role: ProductSourceRole, files: File[]): Promise<ProductImageTaskResponse>;
  uploadProductVideoFrames(
    taskId: string,
    payload: { files: File[]; video_name: string; duration_seconds: number }
  ): Promise<ProductImageTaskResponse>;
  uploadProductVideos(taskId: string, files: File[]): Promise<ProductImageTaskResponse>;
  deleteProductAsset(taskId: string, assetId: number): Promise<ProductMutationResponse>;
  analyzeProductImages(
    taskId: string,
    payload: { api_config_id: number; confirmed_call_count: 1 }
  ): Promise<ProductImageTaskResponse>;
  selectProductReference(
    taskId: string,
    payload: { role: ProductSourceRole; asset_id: number }
  ): Promise<ProductImageTaskResponse>;
  generateProductImages(
    taskId: string,
    payload: { api_config_id: number; confirmed_call_count: number }
  ): Promise<ProductImageTaskResponse>;
  resumeProductImages(
    taskId: string,
    payload: { api_config_id: number; confirmed_call_count: number; acknowledge_possible_charge?: boolean }
  ): Promise<ProductImageTaskResponse>;
  regenerateProductImage(
    taskId: string,
    role: ProductOutputRole,
    payload: { api_config_id: number; confirmed_call_count: 1; acknowledge_possible_charge?: boolean }
  ): Promise<ProductImageTaskResponse>;
  saveProductTransparent(
    taskId: string,
    payload: { image_data_url: string }
  ): Promise<ProductImageTaskResponse>;
  uploadProductTransparent(taskId: string, file: File): Promise<ProductImageTaskResponse>;
  cropProductLogo(
    taskId: string,
    payload: { left: number; top: number; right: number; bottom: number }
  ): Promise<ProductImageTaskResponse>;
  deleteProductSources(taskId: string): Promise<ProductMutationResponse>;
}
