from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    print(" ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local Chrome desktop package")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    output_root = args.output_dir.expanduser().resolve()

    if not args.skip_frontend:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        run([npm, "run", "build"], cwd=ROOT / "frontend")

    index = ROOT / "frontend" / "dist" / "index.html"
    if not index.is_file():
        raise SystemExit("frontend/dist 不存在，请先执行 npm run build")

    data_separator = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--onedir",
        "--name",
        "SinoImageTool",
        "--distpath",
        str(output_root),
        "--paths",
        str(ROOT),
        "--add-data",
        f"{ROOT / 'frontend' / 'dist'}{data_separator}frontend/dist",
        "--add-data",
        f"{ROOT / 'backend' / 'assets'}{data_separator}backend/assets",
        "--add-data",
        f"{ROOT / 'backend' / 'prompts'}{data_separator}backend/prompts",
        "--add-data",
        f"{ROOT / 'backend' / 'ecommerce'}{data_separator}backend/ecommerce",
        "--hidden-import",
        "backend.services.cutout_pipeline_worker",
        "--hidden-import",
        "backend.services.organizer_render_worker",
        "--hidden-import",
        "backend.services.prewarm_worker",
        "--hidden-import",
        "backend.services.recolor_worker",
        "--hidden-import",
        "backend.services.cutout_model_worker",
        "--exclude-module",
        "torch",
        "--exclude-module",
        "matplotlib",
        "--osx-bundle-identifier",
        "com.sino.image-tool",
    ]
    if os.name == "nt":
        system_directory = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
        for runtime_name in ("msvcp140.dll", "msvcp140_1.dll"):
            runtime_path = system_directory / runtime_name
            if runtime_path.is_file():
                command.extend([
                    "--add-binary",
                    f"{runtime_path}{data_separator}.",
                ])
    if not args.no_clean:
        command.append("--clean")
    command.append(str(ROOT / "desktop_launcher.py"))
    run(command)
    output = output_root / "SinoImageTool"
    print(f"\nBuild complete: {output}")
    if os.name == "nt":
        archive = output_root / "SinoImageTool-Windows-x64.zip"
        archive.unlink(missing_ok=True)
        shutil.make_archive(
            str(archive.with_suffix("")),
            "zip",
            root_dir=output_root,
            base_dir="SinoImageTool",
        )
        print(f"Portable package: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
