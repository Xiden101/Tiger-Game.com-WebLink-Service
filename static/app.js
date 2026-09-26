const portSelect   = document.getElementById("port-select");
const connectBtn   = document.getElementById("connect-btn");
const statusRow    = document.getElementById("status-row");
const led          = document.getElementById("led");
const statusText   = document.getElementById("status-text");
const results      = document.getElementById("results");
const scoreBody    = document.getElementById("score-body");
const cheatsBody   = document.getElementById("cheats-body");
const downloadCheatBtn = document.getElementById("download-cheat-btn");
const cheatStatus      = document.getElementById("cheat-status");
const cheatLed         = document.getElementById("cheat-led");
const cheatStatusText  = document.getElementById("cheat-status-text");

const tabHighscores = document.getElementById("tab-highscores");
const tabCheats     = document.getElementById("tab-cheats");
const panelHighscores = document.getElementById("panel-highscores");
const panelCheats     = document.getElementById("panel-cheats");

const helpBtn      = document.getElementById("help-btn");
const helpOverlay  = document.getElementById("help-overlay");
const helpCloseBtn = document.getElementById("help-close-btn");
const quitBtn      = document.getElementById("quit-btn");

const submitScoresBtn = document.getElementById("submit-scores-btn");
const submitOverlay   = document.getElementById("submit-overlay");
const submitCloseBtn  = document.getElementById("submit-close-btn");
const submitCodeInput = document.getElementById("submit-code-input");
const submitCodeBtn   = document.getElementById("submit-code-btn");
const submitCodeError = document.getElementById("submit-code-error");

let errorBanner = null;
let connectedPort = null;
let selectedCheatGameId = null;
let lastGames = [];

function setStatus(state, text) {
  statusRow.hidden = false;
  led.className = "led" + (state ? ` ${state}` : "");
  statusText.textContent = text;
}

function clearErrorBanner() {
  if (errorBanner) {
    errorBanner.remove();
    errorBanner = null;
  }
}

function showErrorBanner(message) {
  clearErrorBanner();
  errorBanner = document.createElement("div");
  errorBanner.className = "error-banner";
  errorBanner.textContent = message;
  document.querySelector(".link-panel").appendChild(errorBanner);
}

async function loadPorts() {
  portSelect.innerHTML = "";
  try {
    const res = await fetch("/api/ports");
    const data = await res.json();
    if (!data.ports || data.ports.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "No ports found";
      opt.disabled = true;
      opt.selected = true;
      portSelect.appendChild(opt);
      portSelect.disabled = true;
      connectBtn.disabled = true;
      return;
    }
    portSelect.disabled = false;
    connectBtn.disabled = false;
    for (const p of data.ports) {
      const opt = document.createElement("option");
      opt.value = p.device;
      opt.textContent = p.description && p.description !== "n/a"
        ? `${p.device} — ${p.description}`
        : p.device;
      portSelect.appendChild(opt);
    }
  } catch (err) {
    portSelect.disabled = true;
    connectBtn.disabled = true;
  }
}

function renderScores(games) {
  scoreBody.innerHTML = "";
  if (!games.length) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    tr.innerHTML = `<td colspan="2">No eligible games found.</td>`;
    scoreBody.appendChild(tr);
    return;
  }
  for (const g of games) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = g.game;
    const scoreTd = document.createElement("td");
    scoreTd.className = "score-cell";
    scoreTd.textContent = g.score;
    tr.appendChild(nameTd);
    tr.appendChild(scoreTd);
    scoreBody.appendChild(tr);
  }
}

function renderCheats(cheats) {
  cheatsBody.innerHTML = "";
  selectedCheatGameId = null;
  downloadCheatBtn.disabled = true;
  cheatStatus.hidden = true;

  if (!cheats.length) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    tr.innerHTML = `<td colspan="3">No eligible games found.</td>`;
    cheatsBody.appendChild(tr);
    return;
  }

  for (const c of cheats) {
    const tr = document.createElement("tr");
    tr.className = "cheat-row";

    const radioTd = document.createElement("td");
    radioTd.className = "radio-cell";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "cheat-select";
    radio.className = "cheat-radio";
    radio.value = c.gameId;
    radio.addEventListener("change", () => {
      selectedCheatGameId = c.gameId;
      downloadCheatBtn.disabled = false;
    });
    radioTd.appendChild(radio);

    const nameTd = document.createElement("td");
    nameTd.textContent = c.game;
    const cheatTd = document.createElement("td");
    cheatTd.textContent = c.cheat;

    tr.appendChild(radioTd);
    tr.appendChild(nameTd);
    tr.appendChild(cheatTd);

    // clicking anywhere in the row selects its radio
    tr.addEventListener("click", (e) => {
      if (e.target !== radio) radio.click();
    });

    cheatsBody.appendChild(tr);
  }
}

async function handleConnect() {
  const port = portSelect.value;
  if (!port) return;

  clearErrorBanner();
  results.hidden = true;
  connectBtn.disabled = true;
  connectBtn.textContent = "Connecting";
  setStatus("connecting", `Connecting on ${port}...`);

  try {
    const res = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port }),
    });
    const data = await res.json();

    if (!data.success) {
      setStatus("error", "Not connected");
      showErrorBanner(data.error || "Failed to connect to game.com.");
      connectBtn.textContent = "Connect";
      return;
    }

    setStatus("connected", `Connected on ${port}`);
    connectedPort = port;
    lastGames = data.games || [];
    renderScores(data.games || []);
    renderCheats(data.cheats || []);
    results.hidden = false;

    portSelect.disabled = true;
    connectBtn.textContent = "Disconnect";
    connectBtn.className = "btn btn-secondary";
  } catch (err) {
    setStatus("error", "Not connected");
    showErrorBanner("Failed to connect to game.com.");
    connectBtn.textContent = "Connect";
  } finally {
    connectBtn.disabled = false;
  }
}

async function handleDisconnect() {
  connectBtn.disabled = true;
  connectBtn.textContent = "Disconnecting";

  try {
    await fetch("/api/disconnect", { method: "POST" });
  } catch (err) {
    // best-effort -- reset the UI regardless
  }

  connectedPort = null;
  lastGames = [];
  results.hidden = true;
  portSelect.disabled = false;
  connectBtn.textContent = "Connect";
  connectBtn.className = "btn btn-primary";
  connectBtn.disabled = false;
  setStatus(null, "Disconnected");
}

function handleConnectClick() {
  if (connectedPort) {
    handleDisconnect();
  } else {
    handleConnect();
  }
}

function setCheatStatus(state, text) {
  cheatStatus.hidden = false;
  cheatLed.className = "led" + (state ? ` ${state}` : "");
  cheatStatusText.textContent = text;
}

async function handleDownloadCheat() {
  if (!selectedCheatGameId || !connectedPort) return;

  downloadCheatBtn.disabled = true;
  downloadCheatBtn.textContent = "Downloading";
  setCheatStatus("connecting", "Applying cheat...");

  try {
    const res = await fetch("/api/download-cheat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gameId: selectedCheatGameId }),
    });
    const data = await res.json();

    if (!data.success) {
      setCheatStatus("error", data.error || "Failed to download cheat.");
      return;
    }
    setCheatStatus("connected", "Cheat applied successfully.");
  } catch (err) {
    setCheatStatus("error", "Failed to download cheat.");
  } finally {
    downloadCheatBtn.disabled = false;
    downloadCheatBtn.textContent = "Download Cheat";
  }
}

function activateTab(tab) {
  const isHighscores = tab === "highscores";
  tabHighscores.classList.toggle("active", isHighscores);
  tabCheats.classList.toggle("active", !isHighscores);
  tabHighscores.setAttribute("aria-selected", String(isHighscores));
  tabCheats.setAttribute("aria-selected", String(!isHighscores));
  panelHighscores.hidden = !isHighscores;
  panelCheats.hidden = isHighscores;
}

async function handleQuit() {
  quitBtn.disabled = true;
  try {
    await fetch("/api/shutdown", { method: "POST" });
  } catch (err) {
    // the server may already be gone -- carry on and close up anyway
  }

  // Browsers usually refuse to let a page close a tab it didn't open itself,
  // so if the tab is still here a moment later, show a closed message.
  window.close();
  setTimeout(() => {
    document.body.innerHTML = `
      <div class="closed-screen">
        <h1>Web Link has been closed.</h1>
        <p>You can close this tab now.</p>
      </div>`;
  }, 300);
}

function openHelp() {
  helpOverlay.hidden = false;
}

function closeHelp() {
  helpOverlay.hidden = true;
}

function openSubmitModal() {
  submitCodeInput.value = "";
  submitCodeError.hidden = true;
  submitOverlay.hidden = false;
  submitCodeInput.focus();
}

function closeSubmitModal() {
  submitOverlay.hidden = true;
}

function showSubmitError(message) {
  showSubmitMessage(message, false);
}

function showSubmitMessage(message, isSuccess) {
  submitCodeError.textContent = message;
  submitCodeError.style.borderColor = isSuccess ? "rgba(56,214,133,0.5)" : "";
  submitCodeError.style.background  = isSuccess ? "rgba(56,214,133,0.08)" : "";
  submitCodeError.style.color       = isSuccess ? "#7fe8b0" : "";
  submitCodeError.hidden = false;
}

async function handleSubmitCode() {
  const code = submitCodeInput.value.trim();
  if (!/^\d{6}$/.test(code)) {
    showSubmitError("Enter the 6-digit code from the website.");
    return;
  }
  if (!lastGames.length) {
    showSubmitError("No scores to submit.");
    return;
  }

  submitCodeBtn.disabled = true;
  submitCodeBtn.textContent = "Sending";
  submitCodeError.hidden = true;

  try {
    const res = await fetch("/api/submit-scores", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await res.json();
    if (!data.success) {
      showSubmitMessage(data.error || "Failed to submit scores.", false);
      return;
    }
    showSubmitMessage(
      "Scores sent! The website tab showing your code will switch to your results.",
      true
    );
  } catch (err) {
    showSubmitMessage("Failed to submit scores.", false);
  } finally {
    submitCodeBtn.disabled = false;
    submitCodeBtn.textContent = "Submit";
  }
}

connectBtn.addEventListener("click", handleConnectClick);
downloadCheatBtn.addEventListener("click", handleDownloadCheat);
tabHighscores.addEventListener("click", () => activateTab("highscores"));
tabCheats.addEventListener("click", () => activateTab("cheats"));

helpBtn.addEventListener("click", openHelp);
quitBtn.addEventListener("click", handleQuit);
helpCloseBtn.addEventListener("click", closeHelp);
helpOverlay.addEventListener("click", (e) => {
  if (e.target === helpOverlay) closeHelp(); // clicking the backdrop, not the dialog
});

submitScoresBtn.addEventListener("click", openSubmitModal);
submitCloseBtn.addEventListener("click", closeSubmitModal);
submitCodeBtn.addEventListener("click", handleSubmitCode);
submitCodeInput.addEventListener("input", () => {
  // digits only, 6 max
  submitCodeInput.value = submitCodeInput.value.replace(/\D/g, "").slice(0, 6);
});
submitCodeInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") handleSubmitCode();
});
submitOverlay.addEventListener("click", (e) => {
  if (e.target === submitOverlay) closeSubmitModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!helpOverlay.hidden) closeHelp();
  if (!submitOverlay.hidden) closeSubmitModal();
});

loadPorts();
