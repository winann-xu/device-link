"""
模块：packaging.py
功能：免安装 exe 打包脚本
     使用 Nuitka standalone 编译为单个文件，PyInstaller 作为备用方案。

用法：
    python build/packaging.py          # Nuitka 打包
    python build/packaging.py --pyinstaller  # PyInstaller 打包

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path

# 中文 Windows 控制台为 GBK，emoji 打印会 UnicodeEncodeError 中断后续步骤（zip 打包）
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# 项目根目录
ROOT = Path(__file__).parent.parent
DIST = ROOT / 'dist'
BUILD = ROOT / 'build'


def clean():
    """清理构建产物。"""
    import shutil
    for d in [DIST, BUILD]:
        if d.exists():
            shutil.rmtree(d)
            print(f"已清理: {d}")


def build_with_nuitka():
    """
    使用 Nuitka standalone 编译 DEVICE LINK 为单个 exe。

    关键参数：
      --standalone: 独立运行（不依赖 Python 环境）
      --windows-console-mode=disable: Windows 下不显示控制台窗口
      --enable-plugin=pyside6: 打包 PySide6 依赖
    """
    print("=== Nuitka Standalone 打包 ===")

    main_script = ROOT / 'src' / 'main.py'
    if not main_script.exists():
        print(f"错误: 找不到主程序 {main_script}")
        sys.exit(1)

    cmd = [
        sys.executable, '-m', 'nuitka',
        '--standalone',
        '--windows-console-mode=disable',
        '--enable-plugin=pyside6',
        f'--include-data-dir={ROOT / "config"}={ROOT / "config"}',
        f'--include-data-dir={ROOT / "assets"}={ROOT / "assets"}',
        f'--output-dir={DIST}',
        '--output-filename=DEVICE-LINK.exe',
        '--assume-yes-for-downloads',
        str(main_script),
    ]

    try:
        result = subprocess.run(cmd, check=True, cwd=str(ROOT))
        print(f"\n✅ Nuitka 打包成功! 输出: {DIST / 'DEVICE-LINK.exe'}")
        create_zip_package()
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n⚠ Nuitka 打包失败: {e}")
        print("尝试 PyInstaller 备用方案...")
        return build_with_pyinstaller()


def build_with_pyinstaller():
    """
    备用方案：PyInstaller --onefile --windowed 打包。
    """
    print("=== PyInstaller 打包 ===")

    main_script = ROOT / 'src' / 'main.py'
    spec_file = ROOT / 'DEVICE-LINK.spec'

    # 先创建 spec 文件
    cmd_spec = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--windowed',
        '--name=DEVICE-LINK',
        f'--add-data={ROOT / "config"}:config',
        f'--add-data={ROOT / "assets"}:assets',
        f'--distpath={DIST}',
        f'--workpath={BUILD}',
        f'--specpath={ROOT}',
        str(main_script),
    ]

    try:
        result = subprocess.run(cmd_spec, check=True, cwd=str(ROOT))
        print(f"\n✅ PyInstaller 打包成功! 输出: {DIST / 'DEVICE-LINK.exe'}")
        create_zip_package()
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n❌ PyInstaller 打包也失败了: {e}")
        return False


def create_zip_package():
    """
    创建免安装 zip 包。
    结构：
      DEVICE-LINK-v1.0.0/
      ├── DEVICE-LINK.exe
      ├── config/
      ├── data/
      ├── logs/
      └── 使用手册.md
    """
    import zipfile

    exe_path = DIST / 'DEVICE-LINK.exe'
    if not exe_path.exists():
        print("未找到 exe 文件，跳过 zip 打包")
        return

    # 版本号从默认配置读取，避免打包时手动同步
    version = "1.0.0"
    try:
        import yaml
        with open(ROOT / 'config' / 'default_config.yaml', encoding='utf-8') as f:
            _cfg = yaml.safe_load(f) or {}
        version = str(_cfg.get('app', {}).get('version', '1.0.0'))
    except Exception:
        pass
    zip_name = f"DEVICE-LINK-v{version}.zip"
    zip_path = DIST / zip_name

    print(f"正在创建免安装包: {zip_name}")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe_path, f'DEVICE-LINK-v{version}/DEVICE-LINK.exe')
        # 内置默认配置与资源（修复：原包只有空 .gitkeep，全新解压首启找不到 config.yaml 崩溃）
        default_cfg = ROOT / 'config' / 'default_config.yaml'
        if default_cfg.exists():
            zf.write(default_cfg, f'DEVICE-LINK-v{version}/config/default_config.yaml')
        assets_dir = ROOT / 'assets'
        if assets_dir.is_dir():
            for asset in assets_dir.iterdir():
                if asset.is_file():
                    zf.write(asset, f'DEVICE-LINK-v{version}/assets/{asset.name}')
        # 用户手册随包分发（修复：此前只复制到 dist/ 目录，zip 内没有）
        manual = ROOT / 'docs' / 'user_manual.md'
        if manual.exists():
            zf.write(manual, f'DEVICE-LINK-v{version}/使用手册.md')
        # 空目录占位
        zf.writestr(f'DEVICE-LINK-v{version}/config/.gitkeep', '')
        zf.writestr(f'DEVICE-LINK-v{version}/data/.gitkeep', '')
        zf.writestr(f'DEVICE-LINK-v{version}/logs/.gitkeep', '')

    # 复制文档
    docs_src = ROOT / 'docs' / 'user_manual.md'
    if docs_src.exists():
        shutil.copy(docs_src, DIST / '使用手册.md')

    print(f"✅ 免安装包已创建: {zip_path}")
    print(f"   大小: {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description='DEVICE LINK 打包脚本')
    parser.add_argument('--pyinstaller', action='store_true', help='使用 PyInstaller 打包')
    parser.add_argument('--clean', action='store_true', help='清理构建产物')
    args = parser.parse_args()

    if args.clean:
        clean()
        return

    if args.pyinstaller:
        build_with_pyinstaller()
    else:
        build_with_nuitka()


if __name__ == '__main__':
    main()
