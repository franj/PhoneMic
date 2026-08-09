#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhoneMic 统一构建脚本
用法:
    python build.py --mode standalone              # 编译 + 打包 (默认)
    python build.py --mode standalone --action compile   # 只编译
    python build.py --mode standalone --action package   # 只打包 (需已有编译结果)
    python build.py --mode onefile --action compile      # 只编译单文件
    python build.py --mode onefile                       # 编译单文件并复制到 dist
"""

import subprocess
import sys
import argparse
import shutil
from datetime import datetime
from pathlib import Path
import json
import tomllib

# ---------- 项目配置 ----------
PROJECT_ROOT = Path(__file__).parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "phonemic_nuitka"

# Nuitka 配置
ICON_PATH = PROJECT_ROOT / "phonemic" / "resources" / "favicon.ico"
MAIN_SCRIPT = PROJECT_ROOT / "phonemic" / "PhoneMic.py"

# NSIS 配置
NSIS_SCRIPT = PROJECT_ROOT / "makesetup.nsi"

# ---------- 辅助函数 ----------
def read_version_from_pyproject():
    """从 pyproject.toml 读取版本号（PEP 621）"""
    with open(PYPROJECT_PATH, "rb") as f:
        data = tomllib.load(f)
    try:
        version = data["project"]["version"]
        print(f"从 pyproject.toml 读取版本: {version}")
        return version
    except KeyError:
        print("错误: pyproject.toml 中缺少 [project] 下的 version 字段", file=sys.stderr)
        sys.exit(1)

def get_git_commit_short():
    """获取当前 Git 短哈希"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, encoding="utf-8"
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("警告: 无法获取 Git commit，使用 'nogit'", file=sys.stderr)
        return "nogit"

def get_current_date():
    """返回当前日期字符串 YYYYMMDD"""
    return datetime.now().strftime("%Y%m%d")

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def clean_build_output():
    """清理旧的 Nuitka 构建产物（可选）"""
    if BUILD_DIR.exists():
        print(f"清理旧构建目录: {BUILD_DIR}")
        shutil.rmtree(BUILD_DIR)

def run_nuitka(onefile: bool, version: str, commit: str):
    """
    调用 Nuitka 编译
    :param onefile: True -> 单文件模式，False -> standalone 目录模式
    :param version: 版本号，用于嵌入可执行文件
    :param commit: git commit，用于 build_info.json
    """
    # 生成 build_info.json 到 BUILD_DIR
    build_info = {
        "version": version,
        "commit": commit,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    build_info_path = BUILD_DIR / "build_info.json"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    with open(build_info_path, "w", encoding="utf-8") as f:
        json.dump(build_info, f, indent=2)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile" if onefile else "--standalone",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON_PATH}",
        f"--output-dir={BUILD_DIR}",
        "--lto=yes",
        "--enable-plugin=pyside6",
        "--include-data-dir=./phonemic/resources=phonemic/resources",
        f"--include-data-files={build_info_path}=build_info.json",
        "--include-data-files=./LICENSE=LICENSE",
        "--include-data-files=./LICENSE.LGPL.txt=LICENSE.LGPL.txt",
        "--include-data-files=./NOTICE.txt=NOTICE.txt",
        "--include-data-files=./README.md=README.md",
        "--include-data-files=./USER_GUIDE.md=USER_GUIDE.md",
        "--noinclude-default-mode=error",
        "--nofollow-import-to=tkinter,unittest,pydoc,test,distutils,setuptools,pdb",
        "--nofollow-import-to=PIL",
        "--nofollow-import-to=PySide6.QtWebEngineWidgets",
        "--nofollow-import-to=PySide6.QtNetworkAuth",
        "--nofollow-import-to=PySide6.QtQml",
        "--nofollow-import-to=PySide6.QtQuick",
        "--nofollow-import-to=PySide6.QtTest",
        "--nofollow-import-to=PySide6.QtXml",
        "--jobs=4",
        "--experimental=debug-report-traceback",
        "--product-name=PhoneMic",
        '--company-name="PhoneMic Team"',
        f"--file-version={version}",
        f"--product-version={version}",
        '--file-description="PhoneMic - 手机语音输入电脑端"',
        '--copyright="Copyright (c) 2026 PhoneMic Team. Licensed under Apache 2.0."',
        str(MAIN_SCRIPT)
    ]
    print("Running Nuitka command:")
    print(" ".join(cmd))
    subprocess.check_call(cmd)

def check_makensis():
    """检查 makensis 是否在 PATH 中，若不存在则退出"""
    if shutil.which("makensis") is None:
        print("错误: 找不到 makensis，请安装 NSIS 并将其添加到 PATH 环境变量中", file=sys.stderr)
        sys.exit(1)

def copy_to_dist_onefile(version: str, date_str: str, commit: str):
    """将生成的 single exe 复制到 dist/ 目录并重命名"""
    src_exe = BUILD_DIR / "PhoneMic.exe"
    if not src_exe.exists():
        print(f"错误: 找不到生成的 exe 文件 {src_exe}", file=sys.stderr)
        sys.exit(1)
    target_name = f"PhoneMic_{version}_{date_str}_{commit}.exe"
    target_path = DIST_DIR / target_name
    shutil.copy2(src_exe, target_path)
    print(f"✅ OneFile 已复制到: {target_path}")

def compile_standalone(version: str, commit: str):
    """只编译 standalone 模式，不打包"""
    run_nuitka(onefile=False, version=version, commit=commit)
    standalone_dist = BUILD_DIR / "PhoneMic.dist"
    if not standalone_dist.exists():
        print(f"错误: Nuitka standalone 输出目录不存在: {standalone_dist}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Standalone 编译完成，输出目录: {standalone_dist}")

def package_standalone(version: str, date_str: str, commit: str):
    """只打包 NSIS 安装包（假设 standalone 目录已存在）"""
    check_makensis()
    standalone_dist = BUILD_DIR / "PhoneMic.dist"
    if not standalone_dist.exists():
        print(f"错误: standalone 输出目录不存在，请先运行编译: {standalone_dist}", file=sys.stderr)
        sys.exit(1)
    if not NSIS_SCRIPT.exists():
        print(f"错误: 找不到 NSIS 脚本 {NSIS_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    build_suffix = f"{date_str}_{version}_{commit}"
    cmd = [
        "makensis",
        f"/DVERSION={version}",
        f"/DBUILD_DATE={date_str}",
        f"/DBUILD_COMMIT={commit}",
        f"/DBUILD_SUFFIX={build_suffix}",
        str(NSIS_SCRIPT)
    ]
    print("Running makensis:")
    print(" ".join(cmd))
    subprocess.check_call(cmd)
    print(f"✅ 安装包已生成到 {DIST_DIR}/PhoneMic_Setup_{build_suffix}.exe")

def build_standalone_installer(version: str, date_str: str, commit: str, action: str):
    """
    根据 action 执行 standalone 构建流程
    """
    if action == "package":
        print("⚠️  package 模式将跳过编译，仅打包现有 standalone 目录")
    if action in ("all", "compile"):
        compile_standalone(version, commit)
    if action in ("all", "package"):
        package_standalone(version, date_str, commit)

def build_onefile(version: str, date_str: str, commit: str, action: str):
    """
    根据 action 执行 onefile 构建流程
    """
    if action == "package":
        print("错误: onefile 模式不支持 package 动作（无 NSIS 打包）", file=sys.stderr)
        sys.exit(1)
    # compile 或 all 都执行编译
    run_nuitka(onefile=True, version=version, commit=commit)
    # 复制到 dist
    copy_to_dist_onefile(version, date_str, commit)
    print("✅ OneFile 构建完成")

# ---------- 主入口 ----------
def main():
    parser = argparse.ArgumentParser(description="PhoneMic 构建脚本")
    parser.add_argument(
        "--mode", choices=["standalone", "onefile"], default="standalone",
        help="构建模式: standalone (默认：生成安装包) 或 onefile (单文件)"
    )
    parser.add_argument(
        "--action", choices=["all", "compile", "package"], default="all",
        help="执行动作: all (默认，编译+打包), compile (只编译), package (只打包，仅 standalone 有效)"
    )
    args = parser.parse_args()

    # 前置检查
    if not ICON_PATH.exists():
        print(f"错误: 图标文件不存在: {ICON_PATH}", file=sys.stderr)
        sys.exit(1)
    if not MAIN_SCRIPT.exists():
        print(f"错误: 主程序入口不存在: {MAIN_SCRIPT}", file=sys.stderr)
        sys.exit(1)

    # 读取版本号
    version = read_version_from_pyproject()
    date_str = get_current_date()
    commit = get_git_commit_short()

    # 确保输出目录存在
    ensure_dir(DIST_DIR)

    if args.mode == "onefile":
        print("🔧 构建模式: OneFile 单文件")
        build_onefile(version, date_str, commit, args.action)
    else:  # standalone
        print("🔧 构建模式: Standalone + NSIS 安装包")
        build_standalone_installer(version, date_str, commit, args.action)

if __name__ == "__main__":
    main()