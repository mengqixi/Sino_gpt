# 本地 Chrome 版本

该版本面向内部人员使用。用户不需要安装 Python 或 Node.js，双击程序后会启动内置后端，并使用独立的 Chrome 应用窗口打开工作台。

## 构建 Windows 版本

在 Windows x64、Python 3.12 环境执行：

```powershell
python -m pip install -r requirements-desktop.txt
python desktop/build.py
dist/SinoImageTool/SinoImageTool.exe --self-test
dist/SinoImageTool/SinoImageTool.exe --server-self-test
```

构建脚本会同时生成 `dist/SinoImageTool-Windows-x64.zip`。分发这个压缩包，不能只复制其中的 exe。

## 构建 macOS 版本

在 Apple Silicon Mac、原生 arm64 Python 3.12 环境执行：

```bash
python3 -m pip install -r requirements-desktop.txt
python3 desktop/build.py
```

生成的 `.app` 位于 `dist/`。PyInstaller 不能跨系统构建，因此 macOS 包必须在 Mac 上构建。

内部未签名版本第一次打开时，可能需要在 Finder 中右键应用并选择“打开”。

也可以在 GitHub Actions 中手动运行 `Build local desktop packages`，同时生成 Windows x64 和 macOS Apple Silicon 两个内部测试包。

## 运行数据

- Windows：`%LOCALAPPDATA%/SinoImageTool`
- macOS：`~/Library/Application Support/SinoImageTool`

数据库、上传图片、生成结果、自动整理会话和日志均写入上述目录，不会写入安装目录。

程序优先查找 Google Chrome。Windows 如果没有 Chrome，会尝试 Microsoft Edge；其他情况会使用系统默认浏览器，并显示一个用于退出本地服务的小窗口。

## 仅在本机注入 API 配置

GitHub 构建产物默认不包含任何 API 密钥。需要生成内部私有包时，可在持有 API 数据库的本机执行：

```powershell
python desktop/inject_api.py --platform windows --database data/app.db --source clean-windows.zip --output SinoImageTool-Windows-x64-with-api.zip
python desktop/inject_api.py --platform macos --database data/app.db --source clean-macos.zip --output SinoImageTool-macOS-arm64-with-api.zip
```

私有包首次启动时会把内置配置合并到本机数据目录；相同版本只导入一次。私有包内含可提取的 API 密钥，只能在内部传递，不能上传 GitHub。
