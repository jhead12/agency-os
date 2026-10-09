// The workflow player: acts out a workflow (core/workflows.py) on the user's own
// screen, like a visible Playwright. A cursor moves to each element, fields are
// typed into, pages change, console commands run in the console window, and a
// caption explains every step. Play/Pause/Next/Stop sit in a bar at the bottom.
//
// Captions are spoken: a tutorial step's pre-rendered clip (core/narration.py),
// else the browser's own voice. In auto mode a step waits for its line to end.
// Voice on/off is remembered per browser (localStorage).
//
// It keeps its place across page loads (sessionStorage), never blocks the page
// (the overlay ignores the mouse), and asks before anything that changes data:
// a click that submits a POST form waits for "Do it", and a console command that
// makes a change asks "Run this? [y/N]" in the console as usual.
(() => {
    const KEY = 'aos-player';
    const VOICE_KEY = 'aos-player-voice';
    const FIND_TIMEOUT = 5000;
    const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY)); } catch (_) { return null; } };
    const save = (s) => { try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch (_) { /* unavailable */ } };
    const clear = () => { try { sessionStorage.removeItem(KEY); } catch (_) { /* unavailable */ } };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let state = null;      // {wf: {name, steps}, i, auto}
    let ui = null;
    let run = 0;           // bumps on stop/skip so a running step can tell it was cancelled
    let leaving = false;   // the page is unloading (a goto or a navigating click)
    let nextWaiter = null; // resolves when the user presses Next
    window.addEventListener('pagehide', () => { leaving = true; });

    // ── UI ──

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function build() {
        if (ui) return ui;
        const spot = el('div', 'player-spot');
        const cursor = el('div', 'player-cursor');
        cursor.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M4 2l16 9-7 2-3 7z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
        const caption = el('div', 'player-caption');
        caption.setAttribute('role', 'status');
        caption.setAttribute('aria-live', 'polite');
        const captionText = el('p', 'player-caption-text');
        const captionActions = el('div', 'player-caption-actions');
        caption.append(captionText, captionActions);

        const bar = el('div', 'player-bar');
        bar.setAttribute('role', 'region');
        bar.setAttribute('aria-label', 'Workflow player');
        const title = el('span', 'player-title');
        const count = el('span', 'player-count');
        const playBtn = el('button', 'player-btn');
        const nextBtn = el('button', 'player-btn', 'Next ⏭');
        const voiceBtn = el('button', 'player-btn');
        const stopBtn = el('button', 'player-btn', 'Stop ✕');
        [playBtn, nextBtn, voiceBtn, stopBtn].forEach((b) => { b.type = 'button'; });
        playBtn.addEventListener('click', () => { state.auto = !state.auto; save(state); renderBar(); if (state.auto) next(); });
        nextBtn.addEventListener('click', () => next());
        voiceBtn.addEventListener('click', () => {
            const on = !voiceOn();
            try { localStorage.setItem(VOICE_KEY, on ? 'on' : 'off'); } catch (_) { /* unavailable */ }
            if (!on) hush();
            renderBar();
        });
        stopBtn.addEventListener('click', () => stop());
        bar.append(title, count, playBtn, nextBtn, voiceBtn, stopBtn);

        document.body.append(spot, cursor, caption, bar);
        document.body.classList.add('player-active');
        ui = { spot, cursor, caption, captionText, captionActions, bar, title, count, playBtn, nextBtn, voiceBtn };
        document.addEventListener('keydown', onKey);
        return ui;
    }

    function onKey(event) {
        if (!state || event.target.closest('input, textarea, select')) return;
        if (event.key === 'Escape') { state.auto = false; save(state); renderBar(); }
    }

    function teardown() {
        if (!ui) return;
        Object.values(ui).forEach((node) => { if (node instanceof Element && node.parentNode === document.body) node.remove(); });
        document.removeEventListener('keydown', onKey);
        document.body.classList.remove('player-active');
        ui = null;
    }

    function renderBar() {
        const { title, count, playBtn, voiceBtn } = build();
        title.textContent = state.wf.name;
        count.textContent = `Step ${Math.min(state.i + 1, state.wf.steps.length)} of ${state.wf.steps.length}`;
        playBtn.textContent = state.auto ? 'Pause ⏸' : 'Play ▶';
        playBtn.setAttribute('aria-pressed', String(state.auto));
        voiceBtn.textContent = voiceOn() ? '🔊' : '🔇';
        voiceBtn.setAttribute('aria-label', 'Voice');
        voiceBtn.setAttribute('aria-pressed', String(voiceOn()));
        voiceBtn.title = voiceOn() ? 'Voice on: captions are read aloud' : 'Voice off';
    }

    // ── Voice ──

    const voiceOn = () => { try { return localStorage.getItem(VOICE_KEY) !== 'off'; } catch (_) { return true; } };
    let voice = null;  // {stop, done}: done resolves 'said', or 'silent' if nothing could be played

    // Starts saying a step's line: its clip, else the browser's voice.
    function speak(step) {
        hush();
        const text = step.pause || step.say || '';  // what the caption shows (core/narration.py spoken())
        if (!text || !voiceOn()) return;
        let end;
        const done = new Promise((r) => { end = r; });
        if (step.audio) {
            const audio = new Audio(step.audio);
            audio.addEventListener('ended', () => end('said'));
            audio.addEventListener('error', () => end('silent'));
            audio.play().catch(() => end('silent'));  // e.g. autoplay blocked before any click on this page
            voice = { stop: () => { audio.pause(); end('said'); }, done };
        } else if (window.speechSynthesis) {
            const line = new SpeechSynthesisUtterance(text);
            line.addEventListener('end', () => end('said'));
            line.addEventListener('error', () => end('silent'));
            window.speechSynthesis.speak(line);
            voice = { stop: () => { window.speechSynthesis.cancel(); end('said'); }, done };
        }
    }

    function hush() {
        if (voice) voice.stop();
        voice = null;
    }

    // Waits until the line has been said, or for long enough to read it when it isn't spoken.
    async function untilSaid(text, longest = Infinity) {
        const said = voice ? await voice.done : 'silent';
        await sleep(said === 'said' ? 400 : Math.min(readTime(text), longest));
    }

    let lit = null;        // the highlighted element
    let captionAt = null;  // the element the caption sits beside

    function spotlight(target) {
        const { spot } = build();
        lit = target;
        if (!target) { spot.style.display = 'none'; return; }
        const r = target.getBoundingClientRect();
        Object.assign(spot.style, {
            display: 'block', left: `${r.left - 6}px`, top: `${r.top - 6}px`,
            width: `${r.width + 12}px`, height: `${r.height + 12}px`,
        });
    }

    function say(text, target, actions = []) {
        const { caption, captionText, captionActions } = build();
        captionText.textContent = text || '';
        captionActions.replaceChildren(...actions);
        caption.hidden = !text && !actions.length;
        captionAt = target;
        placeCaption();
    }

    // Beside the target when there's room, else centred above the bar.
    function placeCaption() {
        if (!ui) return;
        const { caption } = ui;
        const target = captionAt;
        const phone = window.innerWidth <= 640;
        caption.classList.toggle('player-caption-floating', Boolean(target) && !phone);
        if (target && !phone) {
            const r = target.getBoundingClientRect();
            const w = Math.min(360, window.innerWidth - 32);
            const below = r.bottom + 14 + caption.offsetHeight < window.innerHeight - 70;
            caption.style.left = `${Math.max(16, Math.min(r.left, window.innerWidth - w - 16))}px`;
            caption.style.top = below ? `${r.bottom + 14}px` : `${Math.max(70, r.top - caption.offsetHeight - 14)}px`;
        } else {
            caption.style.left = '';
            caption.style.top = '';
        }
    }

    async function moveCursor(target) {
        const { cursor } = build();
        const r = target.getBoundingClientRect();
        cursor.style.display = 'block';
        cursor.style.transitionDuration = reduceMotion ? '0ms' : '600ms';
        cursor.style.left = `${r.left + Math.min(r.width / 2, 40)}px`;
        cursor.style.top = `${r.top + Math.min(r.height / 2, 20)}px`;
        await sleep(reduceMotion ? 50 : 650);
    }

    async function pulse() {
        ui.cursor.classList.add('player-cursor-press');
        await sleep(250);
        ui.cursor.classList.remove('player-cursor-press');
    }

    function button(label, primary, onClick) {
        const b = el('button', `btn btn-sm${primary ? '' : ' btn-secondary'}`, label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    // ── Finding things ──

    async function find(selector, timeout = FIND_TIMEOUT) {
        const end = Date.now() + timeout;
        while (Date.now() < end) {
            let node = null;
            try { node = document.querySelector(selector); } catch (_) { return null; }
            if (node && node.getClientRects().length) return node;
            await sleep(150);
        }
        return null;
    }

    async function reveal(selector) {
        const target = await find(selector);
        if (!target) return null;
        target.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' });
        await sleep(reduceMotion ? 50 : 450);
        spotlight(target);
        await moveCursor(target);
        return target;
    }

    // A click that sends a change (a POST form) waits for the user's go-ahead.
    function changesData(target) {
        const form = target.closest('form');
        const submits = target.matches('button:not([type]), button[type="submit"], input[type="submit"]');
        return Boolean(form && submits && (form.getAttribute('method') || 'get').toLowerCase() === 'post');
    }

    function choose(text, target, yesLabel) {
        return new Promise((resolve) => {
            say(text, target, [button(yesLabel, true, () => resolve(true)), button('Skip', false, () => resolve(false))]);
        });
    }

    // ── Steps ──

    const readTime = (text) => Math.min(7000, 1400 + (text || '').length * 35);

    // A goto step goes first and explains on arrival. `went` stops a loop when the page redirects elsewhere.
    const navigates = (step) => Boolean(step.goto) && location.pathname + location.search !== step.goto
        && state.went !== state.i;

    // Runs one step. Returns 'next' (carry on), 'wait' (wait for Next), or 'left' (the page is changing).
    async function perform(step, token) {
        const missing = (what) => `Couldn't find ${what} on this page. It may be hidden for your role or screen size.`;
        const caption = step.say || '';

        if (step.goto) {
            if (navigates(step)) {
                say(`Going to ${step.goto}…`);
                await sleep(500);
                if (token !== run) return 'next';
                state.went = state.i;
                save(state);
                location.assign(step.goto);
                return 'left';
            }
            state.went = null;
            spotlight(null);
            say(caption);
            return 'next';
        }
        if (step.highlight) {
            say(caption);
            const target = await reveal(step.highlight);
            say(target ? caption : `${caption}\n(${missing('this')})`.trim(), target);
            return 'next';
        }
        if (step.fill) {
            say(caption);
            const target = await reveal(step.fill.target);
            if (!target) { say(missing('the field to fill')); return 'next'; }
            say(caption, target);
            target.focus();
            if (target.tagName === 'SELECT') {
                target.value = step.fill.value;
            } else {
                target.value = '';
                for (const ch of step.fill.value) {
                    if (token !== run) return 'next';
                    target.value += ch;
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    await sleep(reduceMotion ? 0 : 60);
                }
            }
            target.dispatchEvent(new Event('change', { bubbles: true }));
            return 'next';
        }
        if (step.click) {
            say(caption);
            const target = await reveal(step.click);
            if (!target) { say(missing('what to click')); return 'next'; }
            if (changesData(target)) {
                const form = target.closest('form');
                const ok = await choose(`${caption ? caption + '\n' : ''}This click saves a change (${form.getAttribute('action') || 'this form'}). Do it?`, target, 'Do it');
                if (token !== run) return 'next';
                if (!ok) { say('Skipped.', target); return 'next'; }
            } else {
                say(caption, target);
                await (state.auto ? untilSaid(caption, 2500) : sleep(300));
            }
            if (token !== run) return 'next';
            await pulse();
            // Count the step as done first: the click may load another page.
            state.i += 1;
            save(state);
            target.click();
            await sleep(700);
            if (leaving) return 'left';
            state.i -= 1; // still here: the main loop advances as usual
            return 'next';
        }
        if (step.wait !== undefined) {
            if (typeof step.wait === 'number') { say(caption); await sleep(step.wait); return 'next'; }
            say(caption || 'Waiting…');
            const found = await find(step.wait.for, 10000);
            if (!found) say(missing('what this step waits for'));
            return 'next';
        }
        if (step.run) {
            say(caption);
            if (!window.aosConsole) {
                say(`${caption ? caption + '\n' : ''}This step runs "${step.run}" in the console, which your role doesn't include. Skipping.`);
                return 'next';
            }
            spotlight(null);
            ui.cursor.style.display = 'none';
            window.aosConsole.open();
            await sleep(300);
            const watch = setInterval(() => {
                if (window.aosConsole.waiting()) say('Answer y or n in the console window.');
            }, 300);
            try { await window.aosConsole.run(step.run); } finally { clearInterval(watch); }
            say(caption);
            return 'next';
        }
        if (step.pause !== undefined) {
            spotlight(null);
            say(step.pause || caption);
            return 'wait';
        }
        spotlight(null);
        say(caption);
        return 'next';
    }

    async function play() {
        const token = ++run;
        while (state && token === run) {
            if (state.i >= state.wf.steps.length) { finish(); return; }
            renderBar();
            const step = state.wf.steps[state.i];
            if (!navigates(step)) speak(step);
            const outcome = await perform(step, token);
            if (outcome === 'left' || token !== run || !state) return;
            if (outcome === 'wait' || !state.auto) {
                ui.nextBtn.focus({ preventScroll: true });
                await new Promise((r) => { nextWaiter = r; });
                if (token !== run || !state) return;
            } else {
                await untilSaid(step.say);
                if (token !== run || !state) return;
            }
            state.i += 1;
            save(state);
        }
    }

    function next() {
        if (!state) return;
        hush();
        if (nextWaiter) { const r = nextWaiter; nextWaiter = null; r(); return; }
        // Skip ahead from a running step.
        state.i += 1;
        save(state);
        play();
    }

    function finish() {
        spotlight(null);
        if (ui) ui.cursor.style.display = 'none';
        const name = state.wf.name;
        clear();
        state = null;
        say(`Finished: ${name}.`, null, [button('Close', true, teardown)]);
        if (ui) ui.bar.hidden = true;
    }

    function stop() {
        run += 1;
        hush();
        nextWaiter = null;
        state = null;
        clear();
        teardown();
    }

    function start(wf) {
        stop();
        state = { wf, i: 0, auto: true };
        save(state);
        build();
        ui.bar.hidden = false;
        play();
    }

    // Keep the spotlight and caption on their element as the page scrolls or resizes.
    const follow = () => { if (ui && lit) { spotlight(lit); placeCaption(); } };
    window.addEventListener('scroll', follow, { passive: true, capture: true });
    window.addEventListener('resize', follow);

    window.aosPlayer = { start, stop };

    // Play buttons: data-workflow-play="tutorial/<slug>" or "mine/<slug>".
    document.addEventListener('click', async (event) => {
        const playBtn = event.target.closest('[data-workflow-play]');
        const tryBtn = event.target.closest('[data-workflow-play-editor]');
        if (!playBtn && !tryBtn) return;
        event.preventDefault();
        try {
            const r = playBtn
                ? await fetch(`/api/workflows/${playBtn.dataset.workflowPlay}`)
                : await fetch('/api/workflows/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-AOS-Workflow': '1' },
                    body: JSON.stringify({ definition: document.querySelector('textarea[name="definition"]').value }),
                });
            const data = await r.json();
            if (!data.ok) { window.alert(data.error || 'That workflow could not be played.'); return; }
            start(data.workflow);
        } catch (_) {
            window.alert('Could not load that workflow.');
        }
    });

    // Carry on after a page load.
    state = load();
    if (state && state.wf && Array.isArray(state.wf.steps)) {
        build();
        setTimeout(play, 400);
    } else {
        state = null;
    }
})();
