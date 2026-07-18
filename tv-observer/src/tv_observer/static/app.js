document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const button = target.closest("[data-copy-url]");
  if (!(button instanceof HTMLElement)) return;
  const status = document.querySelector("#copy-status");
  try {
    const response = await fetch(button.dataset.copyUrl, { credentials: "same-origin" });
    if (!response.ok) throw new Error("report unavailable");
    await navigator.clipboard.writeText(await response.text());
    if (status) status.textContent = "Copied";
  } catch (_error) {
    if (status) status.textContent = "Copy failed; use Download text";
  }
});
