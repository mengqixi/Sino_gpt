# 箱包 AI 商品图工作台

> 面向中文箱包电商团队的一体化图片工作台：在浏览器中完成智能调色、AI 生图、标准商品图、透明图抠图，以及唯品会/京东套图整理。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=20232A)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)

项目地址：[mengqixi/Sino_gpt](https://github.com/mengqixi/Sino_gpt)

## 核心能力

- **智能调色**：本地识别包身与五金保护区，支持智能框选、保护/擦除画笔和即时换色预览。
- **AI 生成**：支持包身换色、材质替换、模特展示和自定义生图；生成结果可继续对话修改。
- **标准商品图**：从实拍照片和可选视频中筛选真实视角，串行生成标准角度图，并导出高清及 800×800 版本。
- **透明图抠图**：独立于自动化整理的透明底处理模块，提供本地初稿、擦除、恢复和结果下载。
- **唯品会套图**：自动整理并生成 15 个规定槽位，包括透明正面、产品信息、组合展示和吊牌图。
- **京东套图**：同时输出 800×800 与 750×1000 目录，支持 Logo 黑白切换、商品/手机/标线/文字独立调整。
- **提示词与 API 管理**：分别管理生图 API 和图文分析 API；密钥只保存在后端，前端仅显示掩码。
- **历史与清理**：保留生成任务、原图、提示词和结果，按数量及时间自动清理失效文件。

## 自动化整理

自动化整理以 OpenCV、NumPy 和 Pillow 为主，在本地完成素材分类、主体分析、透明图准备和模板排版；只有用户主动点击“API 分析素材”时才调用图文分析接口。

### 唯品会

输出 15 个标准文件：

- `1.jpg`、`50.jpg`、`601.jpg`～`603.jpg`：模特图
- `2.jpg`、`3.jpg`、`4.jpg`、`15.jpg`：商品与细节图
- `30.png`：正面透明底
- `401.jpg`：产品信息与长、高、厚标线
- `604.jpg`～`606.jpg`：结构细节与多角度组合
- `801.jpg`：吊牌信息

### 京东

- `0-无logo.jpg`：800×800 模特主图
- `1.jpg`～`5.jpg`：同时生成 800×800 与 750×1000 版本
- `5.jpg`：商品尺寸与 iPhone 17 Pro Max 参照图
- `透明.png`：800×800 正面透明底

多数图片槽位可以在保存前独立选择来源、裁剪、拖动和缩放，自动生成的资料图也支持调整成品元素。尺寸图支持调整商品、手机、长/高/厚标线及 iPhone 文字；前端即时预览与后端精确成图使用同一套布局数据。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | React 18、TypeScript、Vite、Canvas |
| 后端 | FastAPI、Pydantic、Uvicorn |
| 图像处理 | Pillow、OpenCV、NumPy |
| 数据 | SQLite、本地持久化目录 |
| 视频兼容 | 浏览器本地抽帧，FFmpeg 服务端兜底 |

## 快速开始

建议使用 Python 3.12 和 Node.js 20。

### 1. 获取代码

```powershell
git clone https://github.com/mengqixi/Sino_gpt.git
cd Sino_gpt
```

### 2. 启动后端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

后端会自动初始化 SQLite 数据库以及上传、结果和整理任务目录。

### 3. 启动前端

另开一个终端：

```powershell
cd frontend
npm install
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。前端开发服务会把 `/api`、`/uploads`、`/results` 和 `/organizer-assets` 代理到本地 8000 端口。

## Docker

```powershell
docker compose build
docker compose up -d
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。生产镜像会在构建阶段编译前端，运行阶段只启动单个 Uvicorn worker。

持久化目录：

- `data/`：SQLite 数据库、商品图任务和自动化整理数据
- `backend/uploads/`：上传原图
- `backend/results/`：生成图和本地处理结果
- `backend/models/`：可选的本地模型目录

## API 配置

网站内的“API 设置”支持配置不同中转站协议，包括：

- Base URL、接口路径和模型名
- `multipart/form-data` 或 `application/json`
- 图片、提示词、模型、数量、尺寸和质量字段名
- Bearer、Header 或自定义认证
- base64 或 URL 返回图片及嵌套字段路径

API 配置按用途隔离：

- **生图 API**：用于 AI 生图和标准商品图。
- **图文分析 API**：用于素材角度与角色识别。

后端会再次校验配置用途，避免把分析接口误用于生图。HTTP 524 会记录为“结果未知”且不会自动重试，以避免重复扣费。

## 低配服务器部署

项目针对 2GB 内存服务器采用以下约束：

- Python 3.12、单 Uvicorn worker，不开启 `--reload`。
- 前端在开发机运行 `npm run build`，服务器只运行已提交的 `frontend/dist`。
- 图片任务限制并发；视频优先在浏览器抽帧和压缩，FFmpeg 只作为兼容兜底。
- Dockerfile 已限制 OpenMP、OpenBLAS、MKL 和内存分配器线程数。
- `data/`、上传和结果目录使用持久化磁盘，不放入 tmpfs。
- 建议准备 1～2GB swap 作为异常解码的保护，但不以 swap 代替并发限制。

生产更新前先在本地构建并提交前端：

```powershell
cd frontend
npm run build
cd ..
git add frontend/dist frontend/src backend README.md
git commit -m "update application"
git push origin main
```

服务器使用仓库配套的增量更新脚本拉取 `main` 并重启服务。数据库、上传文件和结果目录应放在 Git 工作区之外或通过挂载持久化。

## 项目结构

```text
backend/
  app.py                    # FastAPI 入口
  routers/                  # HTTP API
  services/                 # 生图、调色、商品图、抠图和整理逻辑
  assets/                   # 模板图片与内置字体
  tests/                    # 后端单元测试
frontend/
  src/
    pages/                  # 各功能页面
    api/                    # 前端 API 客户端
  dist/                     # 已构建的生产前端
data/                       # SQLite 与任务数据（运行时）
docs/                       # API 与开发说明
```

## 测试与构建

```powershell
python -m unittest discover -s backend/tests -p "test_*.py"

cd frontend
npm run build
```

健康检查：

```text
GET /api/health
```

## 数据与安全

- API Key 保存在后端 SQLite 中，列表接口只返回掩码。
- 自动化整理使用独立会话和临时目录，不写入 AI 生图历史。
- “开始新一轮”会清理上一轮素材和临时预览；异常遗留素材按时限清理。
- 请勿把真实密钥、服务器密码、生产数据库或用户图片提交到 Git。

## 说明

本项目主要服务于箱包电商内部设计与运营流程。不同平台的图片规范可能变化，上线前仍需人工检查商品结构、颜色、Logo、五金、尺寸和配件是否与实物一致。
