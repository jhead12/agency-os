// Softphone: Call buttons (.voice-call, web/templates/_voice_call.html) place the call
// from the browser through Twilio Voice (docs/BROWSER_CALLING.md, V2).
// The page sends only the outreach id; the server picks the number and caller ID.
// After hang-up, the prospect page opens with the Log call form filled in.
// Test calls (a button with data-test-url, on the campaign admin page) ring the Owner's
// own phone instead: the page first asks the server to record the number it was given,
// then sends only that test call's id. Nothing is logged afterwards.
(() => {
    const PROVIDER = 'twilio';
    let device = null;
    let active = null;  // {call, button, bar, sid, voiceCallId, started, timer}

    const fetchToken = async () => {
        const response = await fetch('/voice/token', { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error(response.status === 404 ? "Calling isn't set up." : "Couldn't start calling.");
        return (await response.json()).token;
    };

    const getDevice = async () => {
        if (device) return device;
        if (!window.Twilio || !window.Twilio.Device) throw new Error("The calling library didn't load.");
        device = new window.Twilio.Device(await fetchToken(), { closeProtection: true });
        device.on('tokenWillExpire', async () => {
            try { device.updateToken(await fetchToken()); } catch (err) { /* the next call asks again */ }
        });
        return device;
    };

    const lookup = async (sid) => {
        const response = await fetch(`/voice/calls/${PROVIDER}/${encodeURIComponent(sid)}`,
                                     { headers: { Accept: 'application/json' } });
        return response.ok ? response.json() : null;
    };

    const lookupTest = async (id) => {
        const response = await fetch(`/voice/test-calls/${encodeURIComponent(id)}`,
                                     { headers: { Accept: 'application/json' } });
        return response.ok ? response.json() : null;
    };

    // Record the test call server-side; returns its id. The number never goes to the provider from here.
    const startTest = async (button) => {
        const input = document.querySelector(button.dataset.phoneInput);
        const body = new FormData();
        body.set('phone', input ? input.value : '');
        const response = await fetch(button.dataset.testUrl, { method: 'POST', body, headers: { Accept: 'application/json' } });
        const reply = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(reply.detail || "Couldn't start the test call.");
        return reply.test_call_id;
    };

    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    };

    const buildBar = (button) => {
        const bar = el('div', 'call-bar');
        bar.setAttribute('role', 'region');
        bar.setAttribute('aria-label', 'Call in progress');
        const top = el('div', 'call-bar-top');
        const who = el('strong', '', button.dataset.label || 'Call');
        const status = el('span', 'call-bar-status muted', 'Connecting…');
        const timer = el('span', 'call-bar-timer', '0:00');
        top.append(who, status, timer);

        const notice = el('div', 'call-bar-disclosure');
        notice.append(el('div', 'call-bar-state', button.dataset.stateLabel || ''),
                      el('p', '', button.dataset.disclosure || ''));
        const read = el('button', 'btn btn-sm', 'Disclosure read');
        read.type = 'button';
        read.disabled = true;
        notice.append(read);

        const controls = el('div', 'call-bar-controls');
        const mute = el('button', 'btn btn-sm btn-secondary', 'Mute');
        mute.type = 'button';
        const hangup = el('button', 'btn btn-sm btn-danger', 'Hang up');
        hangup.type = 'button';
        const close = el('button', 'btn btn-sm btn-secondary', 'Close');
        close.type = 'button';
        close.hidden = true;
        controls.append(mute, hangup, close);

        bar.append(top, notice, controls);
        document.body.append(bar);
        return { bar, status, timer, read, mute, hangup, close };
    };

    const setStatus = (text) => { if (active) active.ui.status.textContent = text; };

    const tick = () => {
        const seconds = Math.floor((Date.now() - active.started) / 1000);
        active.ui.timer.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    };

    const duration = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

    // A test call ends in the call bar: wait briefly for the provider's report, then say how it went.
    const finishTest = async (message) => {
        const { ui, testId } = active;
        setStatus(message || 'Call ended.');
        let placed = null;
        for (let attempt = 0; testId && attempt < 10; attempt++) {
            placed = await lookupTest(testId);
            if (placed && !['pending', 'initiated'].includes(placed.status)) break;
            await new Promise((resolve) => setTimeout(resolve, 800));
        }
        if (placed && !['pending', 'initiated'].includes(placed.status)) {
            setStatus(`Test call ended: ${placed.status.replace('-', ' ')}, ${duration(placed.duration_seconds || 0)}.`);
        }
        ui.close.hidden = false;
    };

    // Wait briefly for the provider's end-of-call report, then open the Log call form.
    const finish = async (message) => {
        if (!active || active.finished) return;
        active.finished = true;
        clearInterval(active.timer);
        const { ui, button, sid } = active;
        ui.mute.hidden = true;
        ui.hangup.hidden = true;
        ui.read.disabled = true;
        if (active.test) return finishTest(message);
        setStatus(message || 'Call ended. Opening the call log…');
        let placed = null;
        for (let attempt = 0; sid && attempt < 10; attempt++) {
            placed = await lookup(sid);
            if (placed && placed.status !== 'initiated') break;
            await new Promise((resolve) => setTimeout(resolve, 800));
        }
        if (!placed) {
            setStatus(message || "The call didn't go through.");
            ui.close.hidden = false;
            return;
        }
        const params = new URLSearchParams({ voice_call: placed.id });
        if (button.dataset.scriptKey) params.set('script', button.dataset.scriptKey);
        window.location.href = `/prospects/${placed.prospect_id}?${params}#log-call-${placed.outreach_id}`;
    };

    const start = async (button) => {
        if (active) return;
        const ui = buildBar(button);
        const test = Boolean(button.dataset.testUrl);
        active = { button, ui, test, testId: null, sid: null, voiceCallId: null, started: null, timer: null, finished: false };
        if (test) ui.read.hidden = true;  // nothing is recorded on a test call
        ui.close.addEventListener('click', () => { ui.bar.remove(); active = null; });
        ui.hangup.addEventListener('click', () => {
            if (active && active.call) active.call.disconnect(); else finish('Call canceled.');
        });
        ui.mute.addEventListener('click', () => {
            if (!active || !active.call) return;
            const muted = !active.call.isMuted();
            active.call.mute(muted);
            ui.mute.textContent = muted ? 'Unmute' : 'Mute';
        });
        ui.read.addEventListener('click', async () => {
            if (!active || !active.voiceCallId) return;
            ui.read.disabled = true;
            const response = await fetch(`/voice/calls/${active.voiceCallId}/disclosure`, { method: 'POST' });
            ui.read.textContent = response.ok ? 'Disclosure read ✓' : 'Not saved, try again';
            ui.read.disabled = response.ok;
        });

        try {
            let params = { outreach_id: button.dataset.outreachId };
            if (test) {
                active.testId = await startTest(button);
                params = { test_call_id: String(active.testId) };
            }
            const call = await (await getDevice()).connect({ params });
            active.call = call;
            setStatus('Ringing…');
            const rememberSid = () => {
                if (active && !active.sid && call.parameters) active.sid = call.parameters.CallSid || null;
            };
            call.on('ringing', rememberSid);
            call.on('accept', async () => {
                rememberSid();
                active.started = Date.now();
                active.timer = setInterval(tick, 1000);
                if (test) {
                    const placed = await lookupTest(active.testId);
                    if (!active || active.finished) return;
                    setStatus(placed && placed.status === 'initiated'
                        ? 'Connected to your phone. Read the disclosure as you would on a real call.'
                        : 'The test call was refused. Listen for the reason.');
                    return;
                }
                const placed = active.sid ? await lookup(active.sid) : null;
                if (!active || active.finished) return;
                if (!placed) {
                    setStatus('The call was refused. Listen for the reason.');
                    return;
                }
                active.voiceCallId = placed.id;
                ui.read.disabled = placed.disclosure_read;
                setStatus('Connected. Read the disclosure first.');
            });
            call.on('disconnect', () => { rememberSid(); finish(); });
            call.on('cancel', () => finish('Call canceled.'));
            call.on('reject', () => finish('Call rejected.'));
            call.on('error', (err) => finish(`Call error: ${err.message || err}`));
        } catch (err) {
            const denied = err && (err.name === 'NotAllowedError' || /permission/i.test(err.message || ''));
            setStatus(denied ? 'Allow microphone access in your browser to call from here.' : (err.message || String(err)));
            ui.mute.hidden = true;
            ui.hangup.hidden = true;
            ui.close.hidden = false;
        }
    };

    document.addEventListener('click', (event) => {
        const button = event.target.closest('button.voice-call');
        if (button && !button.disabled) start(button);
    });
})();
