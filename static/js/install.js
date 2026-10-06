"use strict";

const installButton = document.getElementById("install-android-app");
const installStatus = document.getElementById("install-status");
let installPrompt;

if (window.matchMedia("(display-mode: standalone)").matches) {
    installStatus.textContent = "Finanzas ya está instalada en este dispositivo.";
}

window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    installButton.hidden = false;
    installStatus.textContent = "La aplicación está lista para instalarse.";
});

installButton.addEventListener("click", async () => {
    if (!installPrompt) return;
    await installPrompt.prompt();
    const {outcome} = await installPrompt.userChoice;
    installStatus.textContent = outcome === "accepted" ? "Instalación iniciada." : "Instalación cancelada.";
    installPrompt = undefined;
    installButton.hidden = true;
});

window.addEventListener("appinstalled", () => {
    installStatus.textContent = "Finanzas quedó instalada.";
    installButton.hidden = true;
});
