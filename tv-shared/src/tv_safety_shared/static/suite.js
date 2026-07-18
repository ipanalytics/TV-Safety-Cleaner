document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-sidebar-toggle]");
  if (toggle) document.body.classList.toggle("sidebar-open");
});
