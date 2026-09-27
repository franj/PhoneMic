    // ========== 手机端日志（仅开发模式加载） ==========
    // 手机上（尤其微信内置浏览器）打不开 devtools，所以开发时自备查看入口：
    // 屏幕上可直接看/复制，另可转发到 PC 的 cmd。
    //
    // ⚠️ 本文件**只在从源码运行**时加载：服务端返回 mobile.html 时把 PHONEMIC_DEV_MODE
    // 占位符展开成一段指向本文件的 script 标签（见 phonemic/server/api.py 的
    // _serve_mobile）。打包版既不下发那个标签、
    // 也不把本文件打进产物 —— 「打包版没有调试日志」由**文件不存在**保证，
    // 而不是运行期判断。
    //
    // 页面侧只留一个空壳 window.PhoneLog（见 mobile.html 同名脚本块），本文件
    // 加载后接管它。依赖：全局 t()（i18n，仅在面板渲染时调用 ⇒ 晚绑定）。

    window.PhoneLog = (function () {
        const MAX = 300;         // 环形缓冲条数（面板与转发共用同一份）
        const MAX_LEN = 300;     // 单条截断长度，避免日志本身把回传请求撑爆
        const FLUSH_MS = 2000;   // 转发节流间隔
        // 级别门槛：低于它的记录既不进面板也不转发。
        // log 与 debug 归为最细一档——[SEC]/[Config]/[NET] 那类每次连接都重复、
        // 内容恒定的记录都在这一档，默认不显示；需要时在面板里切到 debug。
        const LEVELS = { log: 10, debug: 10, info: 20, warn: 30, error: 40 };
        let minLevel = LEVELS.info;
        const lines = [];        // {ts, lvl, text}：本地面板的数据源
        let queue = [];          // [ts, lvl, text]：待转发，失败会塞回队首重试
        let forward = false;     // 转发默认关闭：要排查时在面板里手动打开
        let opened = false;
        let el = {};             // DOM 引用，首次打开时构建

        const pad = (n, w = 2) => String(n).padStart(w, '0');

        function clock(ts) {
            const d = new Date(ts);
            return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
        }

        /** 单个参数压成有界字符串：二进制只报类型和长度，不做逐字节序列化 */
        function one(a) {
            let s;
            if (a instanceof Error) s = `${a.name}: ${a.message}`;
            else if (typeof a === 'string') s = a;
            else if (a instanceof ArrayBuffer || ArrayBuffer.isView(a)) {
                s = `<${a.constructor && a.constructor.name || 'binary'} ${a.byteLength}>`;
            } else {
                try { s = JSON.stringify(a); } catch (e) { s = String(a); }
            }
            if (s === undefined) s = String(a);
            return s.length > MAX_LEN ? s.slice(0, MAX_LEN) + '…' : s;
        }

        const fmt = (args) => args.map(one).join(' ').replace(/\s+/g, ' ').trim();

        /** 当前级别下可见的记录。
         * 缓冲始终全量保留、只在展示与转发时过滤——否则「出了问题才打开面板」
         * 的排查流程里，切到 debug 也回看不到连接当时发生了什么。 */
        const visible = () => lines.filter((l) => (LEVELS[l.lvl] || LEVELS.info) >= minLevel);

        /** 记一条：入环形缓冲 + 排入转发队列 + 刷新面板（若已打开） */
        function push(lvl, args) {
            const ts = Date.now();
            const text = fmt(args);
            if (!text) return;
            lines.push({ ts, lvl, text });
            while (lines.length > MAX) lines.shift();
            // 转发按门槛过滤：不够级别的直接不排队，省掉一趟无用的网络请求
            if (forward && (LEVELS[lvl] || LEVELS.info) >= minLevel) {
                queue.push([ts, lvl, text]);
                if (queue.length > MAX) queue.shift();
            }
            if (opened) render();
        }

        // 接管 console：既有日志（i18n / WS / SEC / FILE / NET）自动进缓冲，
        // 仍然调用原方法，接远程调试时行为不变
        ['log', 'info', 'warn', 'error', 'debug'].forEach((name) => {
            const orig = console[name].bind(console);
            console[name] = (...args) => { orig(...args); push(name, args); };
        });
        // 未捕获异常与未处理的 Promise 拒绝也要留痕：手机上这些平时完全不可见
        window.addEventListener('error', (e) => {
            push('error', ['[JS] ' + (e.message || 'error'), `${e.filename || ''}:${e.lineno || 0}`]);
        });
        window.addEventListener('unhandledrejection', (e) => {
            push('error', ['[JS] unhandledrejection', e.reason]);
        });

        // 网络与页面生命周期快照。
        // Android WebView（尤其 MIUI 一类厂商 ROM）会在切后台、熄屏、唤起文件选择器时
        // 把 WebSocket 掐掉，两端都收不到关闭帧，只剩一个 1006。
        // 断连现场只有这几条能区分「网络真的断了」与「页面被冻住/被回收」，必须留痕。
        // 注意：页面恢复时浏览器会把后台积压的事件一次性补发，事件时间戳 ≠ 真实发生时刻。
        const netSnap = () => {
            const c = navigator.connection || {};
            const extra = c.effectiveType ? ` eff=${c.effectiveType}` : (c.type ? ` net=${c.type}` : '');
            return `online=${navigator.onLine} vis=${document.visibilityState}${extra}`;
        };
        window.addEventListener('online', () => push('info', ['[LIFE] online', netSnap()]));
        window.addEventListener('offline', () => push('warn', ['[LIFE] offline', netSnap()]));
        // pagehide 要排在下面 sendBeacon 那条之前注册，否则这一行进不了最后一批
        ['visibilitychange', 'pageshow', 'pagehide'].forEach((name) => {
            document.addEventListener(name, () => push('info', [`[LIFE] ${name}`, netSnap()]));
        });
        ['freeze', 'resume'].forEach((name) => {
            document.addEventListener(name, () => push('warn', [`[LIFE] ${name}`, netSnap()]));
        });

        /** 转发一批到 PC 的 cmd；失败塞回队首，下轮再试，不丢日志 */
        async function flush() {
            if (!forward || queue.length === 0) return;
            const batch = queue.splice(0, 50);
            try {
                const res = await fetch('api/client-log', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ua: navigator.userAgent, entries: batch }),
                });
                // 4xx 是「这个地址根本不存在」——典型是旧会话的页面（secret_path 已经换了）。
                // 重试永远不会成功，直接丢弃；否则这个僵尸页面会一直往后端打无效请求。
                if (res.status >= 400 && res.status < 500) return;
                if (!res.ok) throw new Error('HTTP ' + res.status);
            } catch (e) {
                queue = batch.concat(queue).slice(-MAX);
            }
        }
        setInterval(flush, FLUSH_MS);
        // 页面被隐藏/关闭时尽力送出最后一批：sendBeacon 不受页面生命周期限制
        window.addEventListener('pagehide', () => {
            if (!forward || queue.length === 0) return;
            try {
                const body = JSON.stringify({ ua: navigator.userAgent, entries: queue });
                navigator.sendBeacon('api/client-log', new Blob([body], { type: 'application/json' }));
                queue = [];
            } catch (e) { /* 发不出去就算了，日志在本地面板里还在 */ }
        });

        const text = () => visible().map((l) => `${clock(l.ts)} ${l.lvl.toUpperCase()} ${l.text}`).join('\n');

        // ---------- 面板 UI（首次打开才创建，避免干扰正常使用的布局） ----------

        function render() {
            if (!el.body) return;
            const vis = visible();
            el.title.textContent = `${t('dbg_log_title')} ${vis.length}`;
            el.level.textContent = `${t('dbg_log_level')}:${minLevel === LEVELS.info ? 'info' : 'debug'}`;
            el.clear.textContent = t('dbg_log_clear');
            el.copy.textContent = t('dbg_log_copy');
            el.fwd.textContent = forward ? t('dbg_log_forward_on') : t('dbg_log_forward_off');
            el.close.textContent = t('dbg_log_close');
            // 只在已经贴底时才自动滚到底，翻看历史不被新日志打断
            const atBottom = el.body.scrollHeight - el.body.scrollTop - el.body.clientHeight < 40;
            el.body.textContent = '';
            if (vis.length === 0) {
                el.body.textContent = t('dbg_log_empty');
            } else {
                const frag = document.createDocumentFragment();
                vis.forEach((l) => {
                    const row = document.createElement('div');
                    row.className = 'pl-' + l.lvl;
                    row.textContent = `${clock(l.ts)} ${l.text}`;
                    frag.appendChild(row);
                });
                el.body.appendChild(frag);
            }
            if (atBottom) el.body.scrollTop = el.body.scrollHeight;
        }

        async function copyAll() {
            const data = text();
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(data);
                } else {
                    // 非安全上下文（http:// 局域网直连）没有 clipboard API，退回选中 + 复制
                    const ta = document.createElement('textarea');
                    ta.value = data;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    ta.setSelectionRange(0, data.length);
                    const ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                    if (!ok) throw new Error('execCommand copy failed');
                }
                push('info', [t('dbg_log_copied')]);
            } catch (e) {
                push('warn', [t('dbg_log_copy_failed')]);
            }
        }

        function setOpen(v) {
            if (!el.panel) return;
            opened = v;
            el.panel.classList.toggle('visible', v);
            // 不再重复记 env()：build() 时已记过一次，重复记只会在每次打开面板时
            // 白刷 4 条；之后网络/可见性的变化由 [LIFE] 事件覆盖
            if (v) render();
        }

        function build() {
            const btn = document.createElement('button');
            btn.id = 'phone-log-btn';
            btn.textContent = '🐞';
            btn.addEventListener('click', () => setOpen(true));
            document.body.appendChild(btn);

            const panel = document.createElement('div');
            panel.id = 'phone-log-panel';
            // 静态骨架，无任何外部数据，可安全用 innerHTML
            panel.innerHTML =
                '<div id="phone-log-head">' +
                '<span class="pl-title"></span>' +
                '<button data-act="level"></button>' +
                '<button data-act="clear"></button>' +
                '<button data-act="copy"></button>' +
                '<button data-act="forward"></button>' +
                '<button data-act="close"></button>' +
                '</div><div id="phone-log-body"></div>';
            document.body.appendChild(panel);

            el = {
                btn,
                panel,
                title: panel.querySelector('.pl-title'),
                body: panel.querySelector('#phone-log-body'),
                level: panel.querySelector('[data-act="level"]'),
                clear: panel.querySelector('[data-act="clear"]'),
                copy: panel.querySelector('[data-act="copy"]'),
                fwd: panel.querySelector('[data-act="forward"]'),
                close: panel.querySelector('[data-act="close"]'),
            };
            el.close.addEventListener('click', () => setOpen(false));
            el.clear.addEventListener('click', () => { lines.length = 0; queue = []; render(); });
            el.copy.addEventListener('click', copyAll);
            el.fwd.addEventListener('click', () => { forward = !forward; if (forward) flush(); render(); });
            // 级别两档切换：info（默认，只留状态机与动作轨迹）/ debug（连常量噪音一起看）
            el.level.addEventListener('click', () => {
                minLevel = minLevel === LEVELS.info ? LEVELS.debug : LEVELS.info;
                render();
            });
            // 走到这里必然是开发模式（非开发模式已在 IIFE 开头提前返回）
            btn.classList.add('visible');
            // 状态栏是最自然的「出问题了」指示器，点它即可展开日志
            const bar = document.getElementById('status-bar');
            if (bar) bar.addEventListener('click', () => setOpen(true));
            env();
        }

        /** 环境摘要：排查时先看这几行。刻意不打印完整 URL —— hash 里是 E2EE 密钥 */
        function env() {
            push('info', ['[ENV] origin=' + location.origin]);
            push('info', [`[ENV] win=${window.innerWidth}x${window.innerHeight} dpr=${window.devicePixelRatio}`]);
            push('info', [`[ENV] online=${navigator.onLine} vis=${document.visibilityState} hash=${location.hash ? 'yes' : 'no'}`]);
            push('info', ['[ENV] ua=' + navigator.userAgent]);
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', build);
        } else {
            build();
        }

        return {
            push: (lvl, ...args) => push(lvl, args),
            open: () => setOpen(true),
            text,
            snap: netSnap,
            // 级别：接受 'info'/'debug'/'warn' 或数值；同时供面板按钮与测试使用
            setLevel: (lv) => {
                minLevel = (typeof lv === 'string' ? LEVELS[lv] : lv) || LEVELS.info;
                render();
            },
            getLevel: () => (minLevel === LEVELS.info ? 'info' : 'debug'),
        };
    })();
