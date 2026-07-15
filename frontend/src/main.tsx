import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { FolderKanban, History, KeyRound, LayoutDashboard, Palette, ShoppingBag, Wand2 } from "lucide-react";
import Generate from "./pages/Generate";
import RecolorPage from "./pages/Recolor";
import PromptTemplates from "./pages/PromptTemplates";
import ApiConfigs from "./pages/ApiConfigs";
import HistoryPage from "./pages/History";
import Ecommerce from "./pages/Ecommerce";
import VipOrganizer from "./pages/VipOrganizer";
import "./styles.css";

type Page = "recolor" | "ecommerce" | "organizer" | "generate" | "prompts" | "api" | "history";

type GenerateIntent = {
  images: any[];
  taskType?: string;
  targetColor?: string;
  message?: string;
};

function App() {
  const [page, setPage] = useState<Page>("recolor");
  const [generateIntent, setGenerateIntent] = useState<GenerateIntent | null>(null);
  const nav = [
    { key: "recolor" as Page, label: "智能调色", icon: Palette },
    { key: "generate" as Page, label: "AI 生成", icon: Wand2 },
    { key: "ecommerce" as Page, label: "电商生图", icon: ShoppingBag },
    { key: "organizer" as Page, label: "自动化整理", icon: FolderKanban },
    { key: "prompts" as Page, label: "提示词管理", icon: LayoutDashboard },
    { key: "api" as Page, label: "API 设置", icon: KeyRound },
    { key: "history" as Page, label: "历史记录", icon: History }
  ];

  return (
    <div className="app-shell">
      <aside className="sidebar">
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
        {page === "ecommerce" && <Ecommerce />}
        {page === "organizer" && <VipOrganizer />}
        {page === "prompts" && <PromptTemplates />}
        {page === "api" && <ApiConfigs />}
        {page === "history" && <HistoryPage />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
