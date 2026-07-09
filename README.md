# 女包 AI 生图工具

内部设计工作台，支持女包本地智能调色、材质替换、模特展示图生成、提示词管理、中转站 API 配置和历史记录。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

另开终端启动前端开发服务：

```powershell
cd frontend
npm install
npm run dev
```

开发访问：`http://127.0.0.1:5173`

## Docker 启动

```powershell
docker compose build
docker compose up -d
```

访问：`http://127.0.0.1:8000`

Docker 会挂载以下目录，升级镜像时不会丢数据：

- `data/`：SQLite 数据库
- `backend/uploads/`：上传原图
- `backend/results/`：生成图和本地调色结果
- `backend/models/`：预留 SAM/SAM2 模型目录

## 智能调色

生成页顶部的“智能调色”是本地处理，不调用中转站 API。流程：

1. 上传一张女包图。
2. 点击“自动识别”，生成包包主体和金色五金保护区。
3. 选择目标色。
4. 如有漏识别，用“保护五金”画笔补涂；误识别则用“擦除保护”。
5. 点击“应用调色”，下载结果或选为 AI 生成源图。

第一版使用 OpenCV 自动识别，后续可接 SAM/SAM2 增强分割。

## 中转站 API

API Base URL、模型名、接口路径、字段名、返回图片路径等都在前端 API 设置页配置。API Key 保存在后端 SQLite 中，前端读取时只显示掩码。
