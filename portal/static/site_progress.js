const panel = document.getElementById("site-progress");
const bar = document.getElementById("progress-bar");
const summary = document.getElementById("progress-summary");
const form = document.getElementById("simulate-progress");
const provision = document.getElementById("provision-site");
const siteState = document.getElementById("site-state");
const recap = document.getElementById("ansible-recap");
const result = document.getElementById("run-result");
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
    notice.hidden = !(progress.simulado || (progress.id === null && !notice.hidden));
    if (progress.automatico) {
        const state = progress.estado_sitio;
        siteState.className = `status status-${state}`;
        siteState.textContent = state[0].toUpperCase() + state.slice(1) +
            (progress.simulado ? " · Simulado" : "");
        const counts = progress.resumen;
        recap.hidden = counts.ok_count === null;
        if (!recap.hidden) {
            recap.textContent = `Correctas: ${counts.ok_count} · Cambios: ${counts.changed_count} · Fallidas: ${counts.failed_count}`;
        }
        const message = progress.salida.at(-1) ?? "";
        if (result.textContent !== message) result.textContent = message;
    }
    for (const step of progress.pasos) {
        const row = panel.querySelector(`[data-step="${step.orden}"]`);
        row.dataset.state = step.estado;
        row.querySelector("[data-label]").textContent = labels[step.estado];
        const failure = row.querySelector("[data-failure]");
        failure.hidden = step.estado !== "error" || !step.detalle;
        failure.textContent = failure.hidden ? "" : step.detalle;
    }
    if (form) {
        form.hidden = progress.automatico || progress.estado === "ok" || progress.estado === "error" ||
            (progress.id !== null && !progress.simulado);
    }
    if (provision) {
        provision.hidden = progress.estado_sitio === "activo" ||
            (progress.automatico && progress.estado === "en_curso");
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

for (const action of [form, provision].filter(Boolean)) {
    action.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (sending) return;
        const body = new FormData(action);
        if (action === form) body.set("accion", event.submitter?.value ?? "avanzar");
        sending = true;
        clearTimeout(polling);
        pollController?.abort();
        pollController = null;
        const buttons = panel.querySelectorAll("button");
        buttons.forEach((button) => { button.disabled = true; });
        try {
            render(await request(action.action, { method: "POST", body }));
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
