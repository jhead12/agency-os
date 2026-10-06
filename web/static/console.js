// The command console: the /console page and the floating console window
// (base.html, for users with cli.use). Each line goes to /api/console and the
// text it returns is printed. Changes come back as needs_confirmation and are
// only re-sent, confirmed, after the user types y.
//
// History (localStorage) and the screen (sessionStorage) are shared, so the
// window keeps its output as you move between pages. Storage may be missing
// (private mode): everything still works, it just isn't remembered.
(() => {
    const HISTORY_KEY = 'aos-console-history';
    const SCREEN_KEY = 'aos-console-screen';
    const DOCK_KEY = 'aos-console-dock';
    const MAX_SCREEN = 300;
    // A result holding a table (a "----  ----" rule line) keeps its columns and scrolls sideways.
    const TABLE_RULE = /^-+( +-+)*$/m;

    const load = (store, key, fallback) => {
        try { return JSON.parse(store.getItem(key)) ?? fallback; } catch (_) { return fallback; }
    };
    const save = (store, key, value) => {
        try { store.setItem(key, JSON.stringify(value)); } catch (_) { /* storage unavailable */ }
    };

    function mount(root) {
        const output = root.querySelector('.console-output');
        const form = root.querySelector('.console-line');
        const input = root.querySelector('.console-input');
        const promptLabel = root.querySelector('.console-prompt');
        const prompt = root.dataset.prompt;
        let history = load(localStorage, HISTORY_KEY, []);
        let cursor = history.length;
        let pending = null;  // a line waiting for y/N
        let busy = false;

        const show = (text, cls) => {
            const span = document.createElement('span');
            span.className = [cls, TABLE_RULE.test(text) ? 'console-table' : ''].filter(Boolean).join(' ');
            span.textContent = text + '\n';
            output.appendChild(span);
            root.scrollTop = root.scrollHeight;
        };
        const print = (text, cls) => {
            show(text, cls);
            save(sessionStorage, SCREEN_KEY, [...load(sessionStorage, SCREEN_KEY, []), [text, cls || '']].slice(-MAX_SCREEN));
        };
        const clear = () => { output.textContent = ''; save(sessionStorage, SCREEN_KEY, []); };
        const remember = (line) => {
            if (!line || history[history.length - 1] === line) return;
            history = [...history, line].slice(-200);
            save(localStorage, HISTORY_KEY, history);
        };

        async function send(line, confirmed) {
            busy = true;
            try {
                const r = await fetch('/api/console', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-AOS-Console': '1' },
                    body: JSON.stringify({ line, confirmed }),
                });
                if (r.status === 401) { print('Your session ended. Sign in again.', 'console-error'); return; }
                const data = await r.json();
                if (data.play && window.aosPlayer) window.aosPlayer.start(data.play);
                if (data.needs_confirmation) {
                    print(data.output, 'console-muted');
                    pending = line;
                    promptLabel.textContent = 'Run this? [y/N]';
                    return;
                }
                if (data.output && data.ok) print(data.output);
                else if (data.output) {
                    // The problem in red; the guidance under it (usage, example, a command to try) stays readable.
                    const [problem, ...help] = data.output.split('\n');
                    print(problem, 'console-error');
                    if (help.length) print(help.join('\n'), 'console-hint');
                }
            } catch (_) {
                print('Could not reach agency-os.', 'console-error');
            } finally {
                busy = false;
            }
        }

        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (busy) return;
            const line = input.value;
            input.value = '';
            if (pending !== null) {
                const yes = /^y(es)?$/i.test(line.trim());
                print(`Run this? [y/N] ${line}`, 'console-muted');
                const confirmedLine = pending;
                pending = null;
                promptLabel.textContent = prompt;
                if (yes) await send(confirmedLine, true); else print('Cancelled.', 'console-muted');
                return;
            }
            print(`${prompt} ${line}`, 'console-echo');
            remember(line.trim());
            cursor = history.length;
            if (line.trim() === 'clear') { clear(); return; }
            if (line.trim()) await send(line, false);
        });

        input.addEventListener('keydown', (event) => {
            if (event.key === 'l' && event.ctrlKey) {
                event.preventDefault();
                clear();
            } else if (event.key === 'ArrowUp' && cursor > 0) {
                event.preventDefault();
                input.value = history[--cursor];
            } else if (event.key === 'ArrowDown') {
                event.preventDefault();
                cursor = Math.min(cursor + 1, history.length);
                input.value = history[cursor] || '';
            }
        });

        root.addEventListener('click', () => { if (!window.getSelection().toString()) input.focus(); });

        const screen = load(sessionStorage, SCREEN_KEY, []);
        if (screen.length) screen.forEach(([text, cls]) => show(text, cls));
        else print('agency-os console. Type help to list the commands you can run.', 'console-muted');

        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        // For the workflow player: type a line visibly, run it, and resolve once it's finished,
        // including any "Run this? [y/N]" the user answers. Resolves {asked} (whether it asked).
        async function run(line, charDelay = 35) {
            while (busy || pending !== null) await sleep(100);
            input.focus();
            input.value = '';
            for (const ch of line) { input.value += ch; await sleep(charDelay); }
            await sleep(250);
            form.requestSubmit();
            await sleep(50);
            let asked = false;
            while (busy || pending !== null) { asked = asked || pending !== null; await sleep(100); }
            return { asked };
        }
        return { focus: () => input.focus(), run, waiting: () => pending !== null };
    }

    // ── The /console page ──
    const page = document.getElementById('console');
    if (page) {
        const term = mount(page);
        term.focus();
        window.aosConsole = { open: () => {}, run: term.run, waiting: term.waiting };
    }

    // ── The floating window ──
    const dock = document.getElementById('console-dock');
    const launcher = document.getElementById('console-launcher');
    if (!dock || !launcher) return;
    const term = mount(dock.querySelector('.console'));
    window.aosConsole = { open: () => { if (dock.hidden) setOpen(true, false); }, run: term.run, waiting: term.waiting };
    const bar = dock.querySelector('.console-dock-bar');
    const phone = window.matchMedia('(max-width: 640px)');
    let state = load(localStorage, DOCK_KEY, {});

    const remember = () => save(localStorage, DOCK_KEY, state);

    // Keep the window on screen and below the navbar, so its bar can always be grabbed.
    const place = () => {
        if (phone.matches) { dock.style.cssText = ''; return; }
        const minTop = (document.querySelector('.navbar')?.offsetHeight || 0) + 8;
        const width = Math.min(state.width || 560, window.innerWidth - 16);
        const height = Math.min(state.height || 340, window.innerHeight - minTop - 8);
        const left = state.left ?? window.innerWidth - width - 16;
        const top = state.top ?? window.innerHeight - height - 16;
        dock.style.width = `${width}px`;
        dock.style.height = `${height}px`;
        dock.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
        dock.style.top = `${Math.max(minTop, Math.min(top, window.innerHeight - 40))}px`;
    };

    const setOpen = (open, focus = true) => {
        dock.hidden = !open;
        launcher.hidden = open;
        launcher.setAttribute('aria-expanded', String(open));
        state.open = open;
        remember();
        if (open) { place(); if (focus) term.focus(); } else if (focus) launcher.focus();
    };

    launcher.addEventListener('click', () => setOpen(true));
    dock.querySelector('[data-console-close]').addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (event) => {
        if (event.ctrlKey && (event.key === '`' || event.code === 'Backquote')) {
            event.preventDefault();
            setOpen(dock.hidden);
        }
    });

    bar.addEventListener('pointerdown', (event) => {
        if (phone.matches || event.button !== 0 || event.target.closest('button, a')) return;
        const startX = event.clientX - dock.offsetLeft;
        const startY = event.clientY - dock.offsetTop;
        bar.setPointerCapture(event.pointerId);
        const move = (e) => {
            state.left = e.clientX - startX;
            state.top = e.clientY - startY;
            place();
        };
        const stop = () => {
            bar.removeEventListener('pointermove', move);
            bar.removeEventListener('pointerup', stop);
            remember();
        };
        bar.addEventListener('pointermove', move);
        bar.addEventListener('pointerup', stop);
    });

    // Remember a size changed with the resize handle.
    if ('ResizeObserver' in window) {
        new ResizeObserver(() => {
            if (dock.hidden || phone.matches) return;
            state.width = dock.offsetWidth;
            state.height = dock.offsetHeight;
            remember();
        }).observe(dock);
    }
    window.addEventListener('resize', () => { if (!dock.hidden) place(); });

    // Stay open across pages, without stealing focus from the page that just loaded.
    if (state.open) setOpen(true, false);
})();
