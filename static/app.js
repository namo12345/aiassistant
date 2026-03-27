const STORAGE_KEY = "autopilot-browser-history";
const THEME_KEY = "autopilot-theme";
const USER_META_LABELS = {
    count: "Emails",
    source_count: "Sources",
    filename: "File",
    recipient: "To",
};

const messagesNode = document.getElementById("messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const statusPill = document.getElementById("status-pill");
const themeToggle = document.getElementById("theme-toggle");
const voiceButton = document.getElementById("voice-button");
const quickActions = document.querySelectorAll(".quick-action");
const toolForms = document.querySelectorAll(".tool-form");

let messages = [];
let recognition = null;

function setStatus(text) {
    statusPill.textContent = text;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function saveMessages() {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
}

function loadMessages() {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    try { return JSON.parse(raw); }
    catch { sessionStorage.removeItem(STORAGE_KEY); return []; }
}

function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
}

function getExportText(entry) {
    if (entry.role === "user") return entry.text;
    return entry.payload?.response?.export_text || entry.payload?.response?.text || "";
}

function downloadText(filename, text) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
}

function buildErrorPayload(title, text) {
    return {
        ok: false,
        response: { title, text, items: [], meta: {}, sources: [], export_text: `${title}\n\n${text}` },
    };
}

function createMetaChips(meta) {
    const keys = Object.keys(meta || {}).filter((key) => USER_META_LABELS[key] && meta[key]);
    if (!keys.length) return "";
    return `<div class="message-meta">${keys.map((key) => `<span class="meta-chip">${escapeHtml(USER_META_LABELS[key])}: ${escapeHtml(meta[key])}</span>`).join("")}</div>`;
}

function createItemMarkup(item) {
    const attachments = item.attachments?.length
        ? `<div class="attachment-list">${item.attachments.map((a) => `<article class="attachment-card"><h5>${escapeHtml(a.filename)}</h5><p>${escapeHtml(a.summary)}</p></article>`).join("")}</div>`
        : "";
    const note = item.note ? `<p class="message-note">${escapeHtml(item.note)}</p>` : "";
    return `
        <article class="message-item">
            ${item.title ? `<h4>${escapeHtml(item.title)}</h4>` : ""}
            ${item.subtitle ? `<p class="message-note">${escapeHtml(item.subtitle)}</p>` : ""}
            ${item.body ? `<p>${escapeHtml(item.body)}</p>` : ""}
            ${note}${attachments}
        </article>`;
}

function createSourceMarkup(sources) {
    if (!sources?.length) return "";
    return `<div class="source-list">${sources.map((s) => `<article class="source-card"><strong>${escapeHtml(s.title || "Source")}</strong>${s.url ? ` <a href="${escapeHtml(s.url)}" target="_blank" rel="noreferrer">Open</a>` : ""}</article>`).join("")}</div>`;
}

function renderAssistantMarkup(entry) {
    const r = entry.payload.response;
    const items = r.items?.length ? `<div class="message-grid">${r.items.map(createItemMarkup).join("")}</div>` : "";
    const sources = createSourceMarkup(r.sources);
    const meta = createMetaChips(r.meta);
    const traceId = r.meta?.trace_id || "";
    const feedback = traceId && !entry.feedbackSubmitted
        ? `<button type="button" data-feedback="1" data-trace-id="${escapeHtml(traceId)}">Helpful</button><button type="button" data-feedback="0" data-trace-id="${escapeHtml(traceId)}">Needs work</button>`
        : (entry.feedbackSubmitted ? `<span class="meta-chip">Feedback saved</span>` : "");
    const speak = window.speechSynthesis ? `<button type="button" data-speak="${escapeHtml(getExportText(entry))}">Speak</button>` : "";

    return `
        <div class="message-header">
            <div class="message-role">Assistant</div>
            <h3 class="message-title">${escapeHtml(r.title)}</h3>
        </div>
        <p class="message-text">${escapeHtml(r.text)}</p>
        ${meta}${items}${sources}
        <div class="message-actions">
            ${feedback}${speak}
            <button type="button" data-copy="${escapeHtml(getExportText(entry))}">Copy</button>
            <button type="button" data-download="${escapeHtml(getExportText(entry))}">Download</button>
        </div>`;
}

function renderUserMarkup(entry) {
    return `<div class="message-role">You</div><p class="message-text">${escapeHtml(entry.text)}</p>`;
}

function renderMessages() {
    if (!messages.length) {
        messagesNode.innerHTML = `<div class="empty-state"><p>No messages yet. Chat or use the tools on the left.</p></div>`;
        return;
    }
    messagesNode.innerHTML = messages.map((e) => `<article class="message ${escapeHtml(e.role)}">${e.role === "assistant" ? renderAssistantMarkup(e) : renderUserMarkup(e)}</article>`).join("");
    messagesNode.scrollTop = messagesNode.scrollHeight;
}

function pushMessage(entry) {
    messages.push(entry);
    saveMessages();
    renderMessages();
}

async function copyText(text) {
    await navigator.clipboard.writeText(text);
    setStatus("Copied");
}

function speakText(text, button) {
    if (!window.speechSynthesis || !text) return;
    if (window.speechSynthesis.speaking) {
        window.speechSynthesis.cancel();
        setStatus("Ready");
        if (button) button.textContent = "Speak";
        return;
    }
    const u = new SpeechSynthesisUtterance(text);
    u.onstart = () => { setStatus("Speaking..."); if (button) button.textContent = "Stop"; };
    u.onend = () => { setStatus("Ready"); if (button) button.textContent = "Speak"; };
    u.onerror = () => { setStatus("Ready"); if (button) button.textContent = "Speak"; };
    window.speechSynthesis.speak(u);
}

async function fetchJson(url, options = {}) {
    return (await fetch(url, options)).json();
}

async function submitFeedback(traceId, reward) {
    const payload = await fetchJson("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trace_id: traceId, reward, label: reward >= 0.5 ? "positive" : "negative" }),
    });
    if (!payload.ok) { setStatus("Feedback failed"); return; }
    const entry = messages.find((m) => m.payload?.response?.meta?.trace_id === traceId);
    if (entry) { entry.feedbackSubmitted = true; saveMessages(); renderMessages(); }
    setStatus("Feedback saved");
}

async function runCommand(text) {
    pushMessage({ role: "user", text });
    setStatus("Working...");
    try {
        const payload = await fetchJson("/api/command", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text }),
        });
        pushMessage({ role: "assistant", payload });
        setStatus(payload.ok ? "Ready" : "Needs attention");
    } catch (error) {
        pushMessage({ role: "assistant", payload: buildErrorPayload("Request failed", error.message) });
        setStatus("Request failed");
    }
}

async function runQuickAction(action) {
    if (action !== "mail-summary") return;
    pushMessage({ role: "user", text: "Summarize my latest emails." });
    setStatus("Working...");
    try {
        const payload = await fetchJson("/api/mail/summary");
        pushMessage({ role: "assistant", payload });
        setStatus(payload.ok ? "Ready" : "Needs attention");
    } catch (error) {
        pushMessage({ role: "assistant", payload: buildErrorPayload("Quick action failed", error.message) });
        setStatus("Request failed");
    }
}

async function submitToolForm(form) {
    const endpoint = form.dataset.endpoint;
    const userLabel = form.dataset.userLabel || "Tool request";
    const isUpload = form.id === "upload-tool-form";
    const formData = new FormData(form);
    pushMessage({ role: "user", text: userLabel });
    setStatus("Working...");
    try {
        let payload;
        if (isUpload) {
            payload = await fetchJson(endpoint, { method: "POST", body: formData });
        } else {
            const body = {};
            formData.forEach((v, k) => { body[k] = v; });
            body.polish = formData.get("polish") === "on";
            payload = await fetchJson(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
        }
        pushMessage({ role: "assistant", payload });
        setStatus(payload.ok ? "Ready" : "Needs attention");
        form.reset();
    } catch (error) {
        pushMessage({ role: "assistant", payload: buildErrorPayload("Tool request failed", error.message) });
        setStatus("Request failed");
    }
}

function restoreSession() {
    messages = loadMessages();
    if (!messages.length) {
        pushMessage({
            role: "assistant",
            payload: {
                ok: true,
                response: {
                    title: "Welcome",
                    text: "Hi! I'm AutoPilot AI. Chat with me or use the tools on the left to get started.",
                    items: [], meta: {}, sources: [],
                    export_text: "Welcome\n\nHi! I'm AutoPilot AI. Chat with me or use the tools on the left to get started.",
                },
            },
        });
        return;
    }
    renderMessages();
}

function initVoiceInput() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) { voiceButton.disabled = true; return; }

    recognition = new Recognition();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;

    recognition.addEventListener("start", () => {
        setStatus("Listening...");
        voiceButton.classList.add("recording");
    });

    recognition.addEventListener("result", (event) => {
        let finalTranscript = "";
        let interimTranscript = "";
        for (let i = 0; i < event.results.length; i++) {
            if (event.results[i].isFinal) finalTranscript += event.results[i][0].transcript;
            else interimTranscript += event.results[i][0].transcript;
        }
        chatInput.value = finalTranscript || interimTranscript;
        if (finalTranscript) {
            setStatus("Voice captured");
            setTimeout(() => {
                chatInput.value = finalTranscript.trim();
                runCommand(finalTranscript.trim());
                chatInput.value = "";
            }, 300);
        }
    });

    recognition.addEventListener("end", () => {
        voiceButton.classList.remove("recording");
        if (statusPill.textContent === "Listening...") setStatus("Ready");
    });

    recognition.addEventListener("error", (event) => {
        voiceButton.classList.remove("recording");
        if (event.error === "not-allowed") setStatus("Mic access denied");
        else if (event.error === "no-speech") setStatus("No speech detected");
        else setStatus("Voice failed");
    });

    voiceButton.addEventListener("click", () => {
        if (voiceButton.classList.contains("recording")) { recognition.stop(); return; }
        chatInput.value = "";
        recognition.start();
    });
}

// Event listeners
themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    await runCommand(text);
});

quickActions.forEach((btn) => {
    btn.addEventListener("click", () => {
        const openFormId = btn.dataset.openForm;
        if (openFormId) {
            const panel = document.getElementById(openFormId);
            if (panel) { panel.open = true; panel.scrollIntoView({ behavior: "smooth", block: "center" }); }
            return;
        }
        runQuickAction(btn.dataset.action);
    });
});

toolForms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await submitToolForm(form);
    });
});

messagesNode.addEventListener("click", async (event) => {
    const copyTarget = event.target.closest("[data-copy]");
    if (copyTarget) { await copyText(copyTarget.dataset.copy); return; }

    const feedbackTarget = event.target.closest("[data-feedback]");
    if (feedbackTarget) { await submitFeedback(feedbackTarget.dataset.traceId, Number.parseFloat(feedbackTarget.dataset.feedback)); return; }

    const speakTarget = event.target.closest("[data-speak]");
    if (speakTarget) { speakText(speakTarget.dataset.speak, speakTarget); return; }

    const downloadTarget = event.target.closest("[data-download]");
    if (downloadTarget) { downloadText("autopilot-response.txt", downloadTarget.dataset.download); }
});

applyTheme(localStorage.getItem(THEME_KEY) || "light");
restoreSession();
initVoiceInput();
