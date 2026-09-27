/**
 * 构建 phonemic/resources/sodium.js —— 手机端加密库（IIFE，全局名 sodium）。
 *
 * 本目录是一个**自包含的 npm 工程**，用法：
 *   npm ci                     首次 / 换机器：严格按 package-lock.json 装依赖
 *   npm run build              产物写回 phonemic/resources/sodium.js
 *   node build.mjs --out <路径>  试跑：写到别处，绝不碰签入的产物
 *
 * 版本锁定**全部交给 npm**，脚本里没有任何版本号：
 *   package.json        依赖的精确版本（不带 `^` / `~`）
 *   package-lock.json   传递依赖 + 完整性哈希（**必须签入**）
 *   npm ci              按 lock 安装；与 package.json 不一致时直接失败
 * 所以「改了版本却沿用旧 node_modules 构建」这种漂移卡在 `npm ci` 那一步，
 * 不需要脚本再校验一遍 —— 装完就一定是 lock 里那一套。
 *
 * 可复现性是硬指标：entry.ts + 下面的参数 → 与签入的 sodium.js **逐字节相同**
 * （62,625 B，sha256 30068694da50f57e…）。参数动一个就等于换了产物，务必
 * 跑 `pytest tests/test_js_crypto.py`（与 PC 端 PyNaCl 双向对拉）再签入。
 *
 * 升级依赖：
 *   1. 改 package.json 的版本号
 *   2. npm install               刷新 package-lock.json
 *   3. npm run build -- --out %TEMP%\try.js     先看 sha256/diff，别直接覆盖
 *   4. pytest tests/test_js_crypto.py
 *   5. 与旧产物逐字节核对后，再签入 sodium.js + package-lock.json
 */
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const IS_WIN = process.platform === 'win32';

const argv = process.argv.slice(2);
const outArg = argv.indexOf('--out');
const OUT = outArg >= 0
  ? path.resolve(argv[outArg + 1])
  : path.join(REPO, 'phonemic', 'resources', 'sodium.js');

const ENTRY = path.join(HERE, 'entry.ts');
const ESBUILD = path.join(HERE, 'node_modules', '.bin', IS_WIN ? 'esbuild.cmd' : 'esbuild');

/** 跑一个命令。
 *
 * Windows 上必须 `shell: true`：Node 20+ 出于 CVE-2024-27980 的修复，不再允许
 * 直接 spawn 批处理文件（`esbuild.cmd` 会报 `spawnSync ... EINVAL`）。
 * 走 shell 后参数要自己加引号，否则路径里的空格会被拆开。
 */
function run(cmd, args, cwd) {
  const q = IS_WIN ? args.map((a) => (a.includes(' ') ? `"${a}"` : a)) : args;
  console.log(`> ${cmd} ${q.join(' ')}`);
  return execFileSync(cmd, q, { cwd, stdio: 'inherit', shell: IS_WIN });
}

// 只检查依赖装没装 —— 装什么由 package-lock.json 说了算，脚本不管版本。
if (!fs.existsSync(ESBUILD)) {
  console.error(
    `\n[FAIL] 没找到 ${path.relative(REPO, ESBUILD) || ESBUILD}\n` +
    '\n依赖由 npm 管理，请先在本目录按 lock 安装：\n' +
    `  cd ${path.relative(process.cwd(), HERE) || '.'}\n` +
    '  npm ci\n',
  );
  process.exit(1);
}

fs.mkdirSync(path.dirname(OUT), { recursive: true });
run(ESBUILD, [
  ENTRY,
  '--bundle',
  '--format=iife',
  '--global-name=sodium',   // 与 libsodium.js 一致，调用方无需改动
  '--target=es2020',        // 手机端浏览器：避开 es2022 私有无障碍语法
  '--minify',
  '--legal-comments=eof',   // 保留 @noble 的 MIT 版权声明，NOTICE 里要能对上
  `--outfile=${OUT}`,
], HERE);

const buf = fs.readFileSync(OUT);
const sha = createHash('sha256').update(buf).digest('hex');
console.log(`\nOK  ${path.relative(REPO, OUT)}  ${buf.length} bytes  sha256=${sha}`);
console.log('    （与签入版本是否一致：git diff --stat phonemic/resources/sodium.js）');
