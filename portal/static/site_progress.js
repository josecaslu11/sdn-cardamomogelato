const panel = document.getElementById("site-progress");
const bar = document.getElementById("progress-bar");
const summary = document.getElementById("progress-summary");
const form = document.getElementById("simulate-progress");
const errorMessage = document.getElementById("progress-error");
const notice = document.getElementById("simulation-notice");
const labels = JSON.parse(panel.dataset.labels);
let polling;
let pollController;
let sending = false;

function render(progress) {
    panel.dataset.state = progress.estado;
    bar.value = progress.porcentaje;
    const text = `${progress.porcentaje} % · ${progress.hechos} de ${progress.total} etapas`;
    if (summary.textContent !== text) summary.textContent = text;
    notice.hidden = !(progress.simulado || (form && progress.id === null));
    for (const step of progress.pasos) {
        const row = panel.querySelector(`[data-step="${step.orden}"]`);
        row.dataset.state = step.estado;
        row.querySelector("[data-label]").textContent = labels[step.estado];
        const failure = row.querySelector("[data-failure]");
        failure.hidden = step.estado !== "error" || !step.detalle;
        failure.textContent = failure.hidden ? "" : step.detalle;
    }
    if (form) {
        form.hidden = progress.estado === "ok" || progress.estado === "error" ||
            (progress.id !== null && !progress.simulado);
    }
}

async function request(url, options) {
    const response = await fetch(url, { cache: "no-store", ...options });
    if (response.redirected) throw new Error("La sesión terminó. Vuelve a iniciar sesión.");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    errorMessage.hidden = true;
    return data;
}

function showError(error) {
    errorMessage.textContent = error.message;
    errorMessage.hidden = false;
}

function schedulePoll() {
    clearTimeout(polling);
    if (panel.dataset.state !== "en_curso") return;
    polling = setTimeout(async () => {
        const controller = new AbortController();
        pollController = controller;
        try {
            const data = await request(panel.dataset.url, { signal: controller.signal });
            if (!sending) render(data);
        } catch (error) {
            if (error.name !== "AbortError" && !sending) showError(error);
        } finally {
            if (pollController === controller) {
                pollController = null;
                if (!sending) schedulePoll();
            }
        }
    }, 2000);
}

if (form) {
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (sending) return;
        const body = new FormData(form);
        body.set("accion", event.submitter?.value ?? "avanzar");
        sending = true;
        clearTimeout(polling);
        pollController?.abort();
        pollController = null;
        const buttons = form.querySelectorAll("button");
        buttons.forEach((button) => { button.disabled = true; });
        try {
            render(await request(form.action, { method: "POST", body }));
        } catch (error) {
            showError(error);
        } finally {
            buttons.forEach((button) => { button.disabled = false; });
            sending = false;
            schedulePoll();
        }
    });
}
schedulePoll();
