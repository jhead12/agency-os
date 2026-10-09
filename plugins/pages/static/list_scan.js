// Scan a mailing list (plugins/pages/list_scan.py): reads the chosen file in the
// browser and puts its text in the form. Like u9itus's OCR import: a PDF's text
// layer first, then Tesseract OCR for scanned pages and images. The libraries
// load from the CDN only when a file needs them.
(function () {
    const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/";
    const TESSERACT = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
    const STORE = "list-scan";
    const IMAGE = /\.(png|jpe?g|tiff?|bmp|webp)$/i;

    const form = document.getElementById("list-scan-form");
    if (!form) return;
    const fileInput = document.getElementById("list-scan-file");
    const text = document.getElementById("list-scan-text");
    const campaign = document.getElementById("list-scan-campaign");
    const filename = document.getElementById("list-scan-filename");
    const status = document.getElementById("list-scan-status");

    // Keep the text and campaign across Check, which reloads the page.
    function save() {
        try {
            sessionStorage.setItem(STORE, JSON.stringify(
                { text: text.value, campaign: campaign.value, filename: filename.value }));
        } catch (e) { /* storage blocked: the text just isn't kept */ }
    }
    try {
        const saved = JSON.parse(sessionStorage.getItem(STORE) || "null");
        if (saved) {
            text.value = saved.text || "";
            campaign.value = saved.campaign || "";
            filename.value = saved.filename || "";
        }
    } catch (e) { /* nothing saved */ }
    form.addEventListener("submit", save);
    document.getElementById("list-scan-clear").addEventListener("click", function () {
        text.value = filename.value = fileInput.value = "";
        try { sessionStorage.removeItem(STORE); } catch (e) { /* ignore */ }
    });

    const loaded = {};
    function load(src) {
        loaded[src] = loaded[src] || new Promise(function (resolve, reject) {
            const s = document.createElement("script");
            s.src = src;
            s.onload = resolve;
            s.onerror = function () { reject(new Error("Couldn't load " + src)); };
            document.head.appendChild(s);
        });
        return loaded[src];
    }

    let ocrWorker = null;
    async function ocr(image, label) {
        await load(TESSERACT);
        if (!ocrWorker) {
            status.textContent = "Loading the text recognizer…";
            ocrWorker = await Tesseract.createWorker("eng", 1, {
                logger: function (m) {
                    if (m.status === "recognizing text") {
                        status.textContent = "Reading " + label + "… " + Math.round(m.progress * 100) + "%";
                    }
                },
            });
        }
        const result = await ocrWorker.recognize(image);
        return result.data.text;
    }

    // A page's text layer as lines, with a blank line where the gap is bigger than a line.
    function pageText(content) {
        let out = "", lastY = null, lastHeight = 0;
        for (const item of content.items) {
            if (!("str" in item)) continue;
            const y = item.transform[5], height = item.height || lastHeight || 10;
            if (lastY !== null && Math.abs(y - lastY) > height / 2) {
                out += Math.abs(y - lastY) > height * 1.8 ? "\n\n" : "\n";
            } else if (out && !/\s$/.test(out) && item.str && !/^\s/.test(item.str)) {
                out += " ";
            }
            out += item.str;
            if (item.str.trim()) { lastY = y; lastHeight = height; }
        }
        return out;
    }

    async function readPdf(file) {
        await load(PDFJS + "pdf.min.js");
        pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.js";
        const pdf = await pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise;
        const pages = [];
        for (let n = 1; n <= pdf.numPages; n++) {
            status.textContent = "Reading page " + n + " of " + pdf.numPages + "…";
            const page = await pdf.getPage(n);
            let pageOut = pageText(await page.getTextContent());
            if (pageOut.replace(/\s/g, "").length < 20) {
                // No text layer: a scan. Render the page and OCR it.
                const viewport = page.getViewport({ scale: 2 });
                const canvas = document.createElement("canvas");
                canvas.width = viewport.width;
                canvas.height = viewport.height;
                await page.render({ canvasContext: canvas.getContext("2d"), viewport: viewport }).promise;
                pageOut = await ocr(canvas, "page " + n + " of " + pdf.numPages);
            }
            pages.push(pageOut.trim());
        }
        return pages.join("\n\n");
    }

    fileInput.addEventListener("change", async function () {
        const file = fileInput.files[0];
        if (!file) return;
        fileInput.disabled = true;
        try {
            let out;
            if (/\.pdf$/i.test(file.name)) out = await readPdf(file);
            else if (IMAGE.test(file.name)) out = await ocr(file, file.name);
            else out = await file.text();
            text.value = out.trim();
            filename.value = file.name;
            save();
            status.textContent = "Read " + file.name + ". Check the text, then press Check or Import.";
        } catch (e) {
            status.textContent = "Couldn't read " + file.name + ": " + e.message;
        } finally {
            fileInput.disabled = false;
            if (ocrWorker) { ocrWorker.terminate(); ocrWorker = null; }
        }
    });
})();
