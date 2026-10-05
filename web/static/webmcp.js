// agency-os tools for the user's own browser AI (WebMCP).
// Loaded only for users who turned AI features on and may connect an assistant.
// Every call runs as the signed-in user, with their permissions, and is audited.
// Changes (write tools) ask the user first, showing exactly what will change.
(async () => {
    // Browser AIs that are extensions (sidebars) often add modelContext a moment after the
    // page loads, so wait for it briefly instead of checking only once.
    const find = () => {
        const mc = document.modelContext ?? navigator.modelContext;  // the spec moved the getter to document
        return mc && typeof mc.registerTool === 'function' ? mc : null;
    };
    let mc = find();
    for (let waited = 0; !mc && waited < 15000; waited += 250) {
        await new Promise(resolve => setTimeout(resolve, 250));
        mc = find();
    }
    if (!mc) return;

    const call = async (name, args, confirmed = false) => {
        const response = await fetch(`/api/tools/${encodeURIComponent(name)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-AOS-Tool': '1' },
            body: JSON.stringify({ args, confirmed }),
        });
        return response.json();
    };
    const asContent = (result) => ({ content: [{ type: 'text', text: JSON.stringify(result) }] });

    const confirmChange = async (tool, args, agent) => {
        const summary = `${tool.description}\n\n${Object.entries(args).map(([k, v]) => `${k}: ${v}`).join('\n')}`;
        const ask = () => window.confirm(`Your AI assistant wants to make this change in agency-os:\n\n${summary}\n\nAllow it?`);
        if (agent && typeof agent.requestUserInteraction === 'function') {
            return agent.requestUserInteraction(async () => ask());
        }
        return ask();
    };

    let listing;
    try {
        listing = await (await fetch('/api/tools', { headers: { Accept: 'application/json' } })).json();
    } catch {
        return;
    }
    for (const tool of listing.tools || []) {
        mc.registerTool({
            name: `agency_os_${tool.name}`,
            description: tool.description,
            inputSchema: tool.inputSchema,
            annotations: { readOnlyHint: tool.kind !== 'write' },
            async execute(args, agent) {
                if (tool.kind === 'write' && !(await confirmChange(tool, args || {}, agent))) {
                    return asContent({ ok: false, error: 'The user declined this change' });
                }
                return asContent(await call(tool.name, args || {}, tool.kind === 'write'));
            },
        });
    }

    // On a prospect page, "this prospect" needs no id.
    const match = location.pathname.match(/^\/prospects\/(\d+)$/);
    if (match && (listing.tools || []).some(t => t.name === 'get_prospect')) {
        mc.registerTool({
            name: 'agency_os_get_current_prospect',
            description: 'Everything known about the prospect on the page the user is looking at.',
            inputSchema: { type: 'object', properties: {}, additionalProperties: false },
            annotations: { readOnlyHint: true },
            async execute() {
                return asContent(await call('get_prospect', { prospect_id: Number(match[1]) }));
            },
        });
    }
})();
