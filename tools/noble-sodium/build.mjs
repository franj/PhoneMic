/**
 * 构建 phonemic/resources/noble-sodium.min.js。
 *
 * 用法：node tools/noble-sodium/build.mjs [--out <路径>]
 *
 * 为什么依赖不装在仓库里：产物是 vendor 进 phonemic/resources/ 的单个文件，仓库
 * 不该为它多背一个 node_modules。所以脚本把 entry.mjs 复制到一个临时工作目录，
 * 在那里 npm install + esbuild，产物直接写回 resources/。
 *
 * 产物形态：IIFE，全局名 **sodium**（与 libsodium.js 一致，调用方无需改动）。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const WORK = path.join(os.tmpdir(), 'noble-sodium-build');

const argv = process.argv.slice(2);
const outArg = argv.indexOf('--out');
const OUT = outArg >= 0
  ? path.resolve(argv[outArg + 1])
  : path.join(REPO, 'phonemic', 'resources', 'noble-sodium.min.js');

const DEPS = ['@noble/ciphers', '@noble/curves', '@noble/hashes'];
const ESBUILD = ['esbuild'];

const IS_WIN = process.platform === 'win32';

/** 跑一个命令。
 *
 * Windows 上必须 `shell: true`：Node 20+ 出于 CVE-2024-27980 的修复，不再允许
 * 直接 spawn 批处理文件（`npm.cmd` / `esbuild.cmd` 会报 `spawnSync ... EINVAL`）。
 * 走 shell 后参数要自己加引号，否则路径里的空格会被拆开。
 */
function run(cmd, args, cwd) {
  const argv = IS_WIN ? args.map((a) => (a.includes(' ') ? `"${a}"` : a)) : args;
  console.log(`> ${cmd} ${argv.join(' ')}`);
  return execFileSync(cmd, argv, { cwd, stdio: 'inherit', shell: IS_WIN });
}

function npmCmd() {
  return IS_WIN ? 'npm.cmd' : 'npm';
}

// 1. 准备临时工作目录（含 package.json）
fs.mkdirSync(WORK, { recursive: true });
const pkgPath = path.join(WORK, 'package.json');
if (!fs.existsSync(pkgPath)) {
  fs.writeFileSync(pkgPath, JSON.stringify(
    { name: 'noble-sodium-build', private: true, version: '0.0.0' }, null, 2,
  ));
}

// 2. 装依赖（已装则跳过：产物可复现，不必每次都联网）
if (!fs.existsSync(path.join(WORK, 'node_modules', '@noble'))) {
  run(npmCmd(), ['install', '--no-audit', '--no-fund', ...DEPS, ...ESBUILD], WORK);
}

// 3. entry 必须在有 node_modules 的目录下，esbuild 才解析得到 @noble/*
const entry = path.join(WORK, 'entry.mjs');
fs.copyFileSync(path.join(HERE, 'entry.mjs'), entry);

// 4. 打包
fs.mkdirSync(path.dirname(OUT), { recursive: true });
run(path.join(WORK, 'node_modules', '.bin', IS_WIN ? 'esbuild.cmd' : 'esbuild'), [
  entry,
  '--bundle',
  '--format=iife',
  '--global-name=sodium',
  '--target=es2020',        // 手机端浏览器：避开 es2022 私有无障碍语法
  '--minify',
  '--legal-comments=eof',   // 保留 @noble 的 MIT 版权声明，NOTICE 里要能对上
  `--outfile=${OUT}`,
], WORK);

const size = fs.statSync(OUT).size;
console.log(`\nOK  ${path.relative(REPO, OUT)}  ${size} bytes`);
