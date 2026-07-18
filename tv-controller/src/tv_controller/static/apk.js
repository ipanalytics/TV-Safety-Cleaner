(() => {
  const tasks = document.querySelector("#apk-tasks");
  const tableBody = document.querySelector("#apk-table-body");
  const toastRegion = document.querySelector("#apk-toast-region");
  const downloadForm = document.querySelector("#apk-download-form");
  const dialog = document.querySelector("#apk-confirm-dialog");
  if (!tasks || !tableBody || !toastRegion || !dialog) return;

  const csrf = tableBody.dataset.csrf || "";
  const dialogTitle = dialog.querySelector("#dialog-title");
  const dialogSummary = dialog.querySelector("#dialog-summary");
  const dialogPackage = dialog.querySelector("#dialog-package");
  const dialogTarget = dialog.querySelector("#dialog-target");
  const dialogCheckLabel = dialog.querySelector("#dialog-check-label");
  const dialogCheck = dialog.querySelector("#dialog-check");
  const dialogCheckCopy = dialog.querySelector("#dialog-check-copy");
  const dialogTypeLabel = dialog.querySelector("#dialog-type-label");
  const dialogType = dialog.querySelector("#dialog-type");
  const dialogSubmit = dialog.querySelector("#dialog-submit");
  const libraryCount = document.querySelector("#apk-library-count");
  const taskCount = document.querySelector("#apk-task-count");
  const taskBadge = document.querySelector("#task-count-badge");
  const clearTasks = document.querySelector("#clear-apk-tasks");
  const tvStatus = document.querySelector("#apk-tv-status");
  let selectedAction = null;
  let tableSignature = "";

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const formatBytes = (value) => {
    let size = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB"];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
  };

  const showToast = (message, level = "info") => {
    const toast = element("div", `toast ${level}`);
    toast.append(element("strong", "", level === "error" ? "Action failed" : "Controller"));
    toast.append(element("span", "", message));
    toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 6000);
  };

  const setTvStatus = (state, message) => {
    tvStatus.className = `connection-state ${state}`;
    tvStatus.textContent = message;
  };

  const postAction = async (url, values) => {
    const body = new FormData();
    body.append("csrf_token", csrf);
    Object.entries(values).forEach(([name, value]) => body.append(name, value));
    const response = await fetch(url, {
      method: "POST",
      body,
      credentials: "same-origin",
      headers: {Accept: "application/json", "X-Requested-With": "fetch"},
    });
    const payload = await response.json().catch(() => ({message: "Invalid server response"}));
    if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
    return payload;
  };

  const requireTv = async () => {
    setTvStatus("checking", "Checking...");
    try {
      const payload = await postAction("/apk/preflight", {});
      setTvStatus("online", "Online · authorized");
      return payload;
    } catch (error) {
      setTvStatus("offline", "Unavailable");
      throw error;
    }
  };

  const actionDefinitions = {
    install: {
      title: "Install APK on TV",
      summary: "Installs the selected local APK with replacement enabled. Android will reject an incompatible or invalid package.",
      url: "/apk/install",
      confirmation: "install-apk",
      check: "I verified the package and target TV.",
      submit: "Install on TV",
      submitClass: "approve",
    },
    update: {
      title: "Download and install update",
      summary: "Downloads the currently available APK and installs it over the third-party application on the configured TV.",
      url: "/apk/update",
      confirmation: "update-apk",
      check: "I understand this replaces the installed application version.",
      submit: "Update application",
      submitClass: "approve",
    },
    uninstall: {
      title: "Uninstall application from TV",
      summary: "Removes only a package Android reports as third-party. The downloaded APK remains in local storage.",
      url: "/apk/uninstall",
      confirmation: "uninstall-user-app",
      submit: "Uninstall from TV",
      submitClass: "danger",
      typed: true,
    },
    delete: {
      title: "Delete local APK",
      summary: "Deletes the APK and its metadata from Controller storage. Nothing is changed on the TV.",
      url: "/apk/delete",
      confirmation: "delete-local-apk",
      check: "I understand this deletes only the local file.",
      submit: "Delete local APK",
      submitClass: "danger",
    },
  };

  const updateDialogSubmit = () => {
    if (!selectedAction) return;
    const definition = actionDefinitions[selectedAction.kind];
    dialogSubmit.disabled = definition.typed
      ? dialogType.value.trim() !== selectedAction.package
      : !dialogCheck.checked;
  };

  const openConfirmation = async (button) => {
    const kind = button.dataset.kind;
    const definition = actionDefinitions[kind];
    if (!definition) return;
    if (kind !== "delete") {
      const originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = "Checking TV...";
      try {
        await requireTv();
      } catch (error) {
        setRowFeedback(button.dataset.package || "", error.message, "error");
        showToast(error.message, "error");
        return;
      } finally {
        button.disabled = false;
        button.textContent = originalLabel;
      }
    }
    selectedAction = {
      kind,
      package: button.dataset.package || "unknown",
      file: button.dataset.file || "",
    };
    dialogTitle.textContent = definition.title;
    dialogSummary.textContent = definition.summary;
    dialogPackage.textContent = selectedAction.package;
    dialogTarget.textContent = kind === "delete" ? "Controller local storage" : tableBody.dataset.adbHost;
    dialogSubmit.textContent = definition.submit;
    dialogSubmit.className = definition.submitClass;
    dialogCheck.checked = false;
    dialogType.value = "";
    dialogCheckLabel.hidden = Boolean(definition.typed);
    dialogTypeLabel.hidden = !definition.typed;
    dialogCheckCopy.textContent = definition.check || "";
    updateDialogSubmit();
    dialog.showModal();
    if (definition.typed) dialogType.focus();
  };

  const setRowFeedback = (packageName, text, level) => {
    const rows = tableBody.querySelectorAll("tr[data-package]");
    rows.forEach((row) => {
      if (row.dataset.package !== packageName) return;
      const feedback = row.querySelector(".row-feedback");
      feedback.textContent = text;
      feedback.className = `row-feedback ${level}`;
    });
  };

  const runCheck = async (button) => {
    const packageName = button.dataset.package || "";
    button.disabled = true;
    button.textContent = "Checking...";
    setRowFeedback(packageName, "Connecting to the TV and checking available version...", "running");
    try {
      await requireTv();
      const payload = await postAction("/apk/check-update", {package: packageName});
      showToast(payload.message);
      await refresh();
    } catch (error) {
      setRowFeedback(packageName, error.message, "error");
      showToast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Check update";
    }
  };

  const makeButton = (label, className, kind, apk) => {
    const button = element("button", className, label);
    button.type = "button";
    button.dataset.kind = kind;
    button.dataset.package = apk.package;
    button.dataset.file = apk.file;
    button.classList.add(kind === "check" ? "js-command" : "js-confirm");
    return button;
  };

  const renderTable = (items, sideloadEnabled, adbHost) => {
    tableBody.dataset.sideloadEnabled = sideloadEnabled ? "true" : "false";
    tableBody.dataset.adbHost = adbHost;
    tableBody.replaceChildren();
    if (!items.length) {
      const row = element("tr", "empty-table-row");
      const cell = element("td");
      cell.colSpan = 4;
      cell.append(element("strong", "", "No APK files yet"));
      cell.append(element("span", "", "Download an application above or place a correctly named APK in the private directory."));
      row.append(cell);
      tableBody.append(row);
      return;
    }
    items.forEach((apk) => {
      const row = element("tr");
      row.dataset.file = apk.file;
      row.dataset.package = apk.package;
      const identity = element("td");
      identity.append(element("strong", "", apk.package), element("code", "", apk.file));
      if (apk.package === "unknown") identity.append(element("span", "badge warning-badge", "Metadata incomplete"));
      const version = element("td");
      version.append(element("strong", "", String(apk.versionCode)), element("span", "muted", apk.versionSource));
      const storage = element("td");
      storage.append(element("strong", "", formatBytes(apk.size)), element("span", "muted", "Private Controller storage"));
      const actionCell = element("td");
      const actions = element("div", "apk-actions");
      if (apk.package !== "unknown") {
        actions.append(makeButton("Check update", "secondary", "check", apk));
        if (sideloadEnabled) {
          actions.append(makeButton("Install", "approve", "install", apk));
          actions.append(makeButton("Update", "approve-outline", "update", apk));
          actions.append(makeButton("Uninstall TV", "danger-outline", "uninstall", apk));
        } else {
          actions.append(element("span", "locked-copy", "TV actions locked"));
        }
      }
      actions.append(makeButton("Delete local APK", "danger-link", "delete", apk));
      actionCell.append(actions, element("div", "row-feedback"));
      row.append(identity, version, storage, actionCell);
      tableBody.append(row);
    });
  };

  const renderTasks = (items) => {
    tasks.replaceChildren();
    if (!items.length) {
      const state = element("div", "quiet-state");
      state.append(element("strong", "", "Nothing running"));
      state.append(element("span", "", "New downloads and update checks will appear here without reloading the page."));
      tasks.append(state);
      return;
    }
    items.forEach((item) => {
      const row = element("article", `task-row ${item.stage || "queued"}`);
      const copy = element("div");
      copy.append(element("strong", "", item.status || "Working"));
      copy.append(element("span", "", `${item.package || "unknown"}${item.detail ? ` | ${item.detail}` : ""}`));
      if (item.stage === "failed" || item.stage === "result") {
        row.append(copy, element("span", `task-outcome ${item.stage}`, item.status || "Finished"));
      } else {
        const bar = element("div", "progress");
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-valuenow", item.progress || 0);
        const fill = element("span");
        fill.style.width = `${Math.max(0, Math.min(100, item.progress || 0))}%`;
        bar.append(fill);
        row.append(copy, bar, element("b", "", `${item.progress || 0}%`));
      }
      tasks.append(row);
      const level = item.stage === "failed" ? "error" : item.stage === "result" ? "success" : "running";
      setRowFeedback(item.package, `${item.status}${item.detail ? `: ${item.detail}` : ""}`, level);
    });
  };

  const refresh = async () => {
    try {
      const response = await fetch("/api/live", {
        headers: {Accept: "application/json"},
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const data = await response.json();
      const apkItems = data.apk_table || [];
      const taskItems = data.apk_tasks || [];
      const signature = JSON.stringify({
        rows: apkItems.map((apk) => [apk.file, apk.size, apk.versionCode]),
        enabled: Boolean(data.sideload_enabled),
        host: data.adb_host || "",
      });
      if (signature !== tableSignature) {
        tableSignature = signature;
        renderTable(apkItems, Boolean(data.sideload_enabled), data.adb_host || "");
      }
      renderTasks(taskItems);
      libraryCount.textContent = `${apkItems.length} APK`;
      taskCount.textContent = `${taskItems.length} tasks`;
      taskBadge.textContent = String(taskItems.length);
      clearTasks.disabled = !taskItems.some((item) => item.stage === "failed" || item.stage === "result");
    } catch (_) {
      // The next poll retries without moving or replacing the current page.
    }
  };

  tableBody.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.classList.contains("js-command")) runCheck(button);
    if (button.classList.contains("js-confirm")) openConfirmation(button);
  });

  clearTasks.addEventListener("click", async () => {
    clearTasks.disabled = true;
    try {
      const payload = await postAction("/apk/tasks/clear", {});
      showToast(payload.message);
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
    }
  });

  downloadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = downloadForm.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Starting...";
    try {
      const values = Object.fromEntries(new FormData(downloadForm).entries());
      delete values.csrf_token;
      const payload = await postAction(downloadForm.action, values);
      showToast(payload.message);
      downloadForm.querySelector("textarea").value = "";
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Download APKs";
    }
  });

  dialogCheck.addEventListener("change", updateDialogSubmit);
  dialogType.addEventListener("input", updateDialogSubmit);
  dialogSubmit.addEventListener("click", async () => {
    if (!selectedAction || dialogSubmit.disabled) return;
    const definition = actionDefinitions[selectedAction.kind];
    const values = {confirmation: definition.confirmation};
    if (selectedAction.kind === "install" || selectedAction.kind === "delete") values.file = selectedAction.file;
    else values.package = selectedAction.package;
    dialogSubmit.disabled = true;
    dialogSubmit.textContent = "Starting...";
    try {
      const payload = await postAction(definition.url, values);
      dialog.close();
      showToast(payload.message);
      setRowFeedback(selectedAction.package, payload.message, "running");
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
      setRowFeedback(selectedAction.package, error.message, "error");
      dialogSubmit.textContent = definition.submit;
      updateDialogSubmit();
    }
  });

  refresh();
  window.setInterval(refresh, 2000);
})();
