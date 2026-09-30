(function () {
  function openModal(id) {
    var modal = document.getElementById(id);
    if (!modal) return;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeModal(modal) {
    if (!modal) return;
    modal.hidden = true;
    if (!document.querySelector("[data-gc2-modal]:not([hidden])")) {
      document.body.style.overflow = "";
    }
  }

  document.addEventListener("click", function (event) {
    var openBtn = event.target.closest("[data-gc2-open-modal]");
    if (openBtn) {
      event.preventDefault();
      openModal(openBtn.getAttribute("data-gc2-open-modal"));
      return;
    }
    var closeBtn = event.target.closest("[data-gc2-close-modal]");
    if (closeBtn) {
      closeModal(closeBtn.closest("[data-gc2-modal]"));
      return;
    }
    var modal = event.target.closest("[data-gc2-modal]");
    if (modal && event.target === modal) {
      closeModal(modal);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll("[data-gc2-modal]:not([hidden])").forEach(closeModal);
  });
})();
