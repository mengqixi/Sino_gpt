import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { FolderKanban, History, KeyRound, LayoutDashboard, Palette, PanelLeftClose, PanelLeftOpen, Scissors, ShoppingBag, Wand2 } from "lucide-react";
import Generate from "./pages/Generate";
import RecolorPage from "./pages/Recolor";
import PromptTemplates from "./pages/PromptTemplates";
import ApiConfigs from "./pages/ApiConfigs";
import HistoryPage from "./pages/History";
import ProductImages from "./pages/ProductImages";
import ProductCutout from "./pages/ProductCutout";
import VipOrganizer from "./pages/VipOrganizer";
import "./styles.css";

type Page = "recolor" | "product_images" | "cutout" | "organizer" | "generate" | "prompts" | "api" | "history";

type GenerateIntent = {
  images: any[];
  taskType?: string;
  targetColor?: string;
  message?: string;
};

function App() {
  const [page, setPage] = useState<Page>("recolor");
  const [generateIntent, setGenerateIntent] = useState<GenerateIntent | null>(null);
  const [organizerSourceIntent, setOrganizerSourceIntent] = useState<File | null>(null);
  const [sidebarVisible, setSidebarVisible] = useState(true);
  const nav = [
    { key: "recolor" as Page, label: "智能调色", icon: Palette },
    { key: "generate" as Page, label: "AI 生成", icon: Wand2 },
    { key: "product_images" as Page, label: "生成商品图", icon: ShoppingBag },
    { key: "cutout" as Page, label: "透明图抠图", icon: Scissors },
    { key: "organizer" as Page, label: "自动化整理", icon: FolderKanban },
    { key: "prompts" as Page, label: "提示词管理", icon: LayoutDashboard },
    { key: "api" as Page, label: "API 设置", icon: KeyRound },
    { key: "history" as Page, label: "历史记录", icon: History }
  ];

  return (
    <div className={`app-shell${sidebarVisible ? "" : " sidebar-collapsed"}`}>
      <button
        type="button"
        className={`sidebar-toggle${sidebarVisible ? " is-open" : ""}`}
        onClick={() => setSidebarVisible((visible) => !visible)}
        aria-label={sidebarVisible ? "隐藏左侧功能栏" : "显示左侧功能栏"}
        title={sidebarVisible ? "隐藏左侧功能栏" : "显示左侧功能栏"}
      >
        {sidebarVisible ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}
      </button>
      <aside className="sidebar" aria-hidden={!sidebarVisible}>
        <div className="brand">
          <div className="brand-mark">B</div>
          <div>
            <strong>女包 AI 生图工具</strong>
            <span>内部设计工作台</span>
          </div>
        </div>
        <nav>
          {nav.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.key} className={page === item.key ? "active" : ""} onClick={() => setPage(item.key)}>
                <Icon size={18} />
                {item.label}
              </button>
            );
          })}
        </nav>
      </aside>
      <main>
        {page === "recolor" && (
          <RecolorPage
            onUseAsSource={(image) => {
              setGenerateIntent({ images: [image], message: "已将调色结果选为生成源图" });
              setPage("generate");
            }}
            onSendOriginalToAi={(image, targetColor) => {
              setGenerateIntent({
                images: [image],
                taskType: "color_change",
                targetColor,
                message: `已带入原图和目标颜色 ${targetColor}`
              });
              setPage("generate");
            }}
          />
        )}
        <div style={{ display: page === "generate" ? "block" : "none" }}>
          <Generate initialIntent={generateIntent} onIntentConsumed={() => setGenerateIntent(null)} />
        </div>
        <div style={{ display: page === "product_images" ? "block" : "none" }}>
          <ProductImages />
        </div>
        <div style={{ display: page === "cutout" ? "block" : "none" }}>
          <ProductCutout
            active={page === "cutout"}
            onUseAsOrganizerSource={(file) => {
              setOrganizerSourceIntent(file);
              setPage("organizer");
              window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
            }}
          />
        </div>
        <div style={{ display: page === "organizer" ? "block" : "none" }}>
          <VipOrganizer
            active={page === "organizer"}
            initialProductFile={organizerSourceIntent}
            onInitialProductFileConsumed={() => setOrganizerSourceIntent(null)}
          />
        </div>
        {page === "prompts" && <PromptTemplates />}
        {page === "api" && <ApiConfigs />}
        {page === "history" && <HistoryPage />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
