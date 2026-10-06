// The command console (/console): sends each line to /api/console and prints
// the text it returns. Changes come back as needs_confirmation and are only
// re-sent, confirmed, after the user types y.
(() => {
    const root = document.getElementById('console');
    if (!root) return;
    const output = document.getElementById('console-output');
    const form = document.getElementById('console-form');
    const input = document.getElementById('console-input');
    const promptLabel = document.getElementById('console-prompt');
    const prompt = root.dataset.prompt;
    const HISTORY_KEY = 'aos-console-history';

    let history = [];
    try { history = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch (_) { history = []; }
    let cursor = history.length;
    let pending = null;  // a line waiting for y/N
    let busy = false;

    // A result holding a table (a "----  ----" rule line) keeps its columns and scrolls sideways on phones.
    const TABLE_RULE = /^-+( +-+)*$/m;

    const print = (text, cls) => {
        const span = document.createElement('span');
        span.className = [cls, TABLE_RULE.test(text) ? 'console-table' : ''].filter(Boolean).join(' ');
        span.textContent = text + '\n';
        output.appendChild(span);
        root.scrollTop = root.scrollHeight;
    };

    const remember = (line) => {
        if (!line || history[history.length - 1] === line) return;
        history = [...history, line].slice(-200);
        try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (_) { /* private mode */ }
    };

    const setPrompt = (text) => { promptLabel.textContent = text; };

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
            if (data.needs_confirmation) {
                print(data.output, 'console-muted');
                pending = line;
                setPrompt('Run this? [y/N]');
                return;
            }
            if (data.output) print(data.output, data.ok ? '' : 'console-error');
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
            setPrompt(prompt);
            if (yes) await send(confirmedLine, true); else print('Cancelled.', 'console-muted');
            return;
        }
        print(`${prompt} ${line}`, 'console-echo');
        remember(line.trim());
        cursor = history.length;
        if (line.trim() === 'clear') { output.textContent = ''; return; }
        if (line.trim()) await send(line, false);
    });

    input.addEventListener('keydown', (event) => {
        if (event.key === 'l' && event.ctrlKey) {
            event.preventDefault();
            output.textContent = '';
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
    print('agency-os console. Type help to list the commands you can run.', 'console-muted');
})();
