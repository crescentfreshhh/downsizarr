// Encoder-specific preset options + small UI niceties for the browse page.
(function () {
  const PRESETS = {
    libx265: ["ultrafast", "superfast", "veryfast", "faster", "fast",
              "medium", "slow", "slower", "veryslow"],
    hevc_nvenc: ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    hevc_qsv: ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
    hevc_vaapi: ["default"],
  };
  const DEFAULT_PRESET = {
    libx265: "slow", hevc_nvenc: "p7", hevc_qsv: "slower", hevc_vaapi: "default",
  };

  function fillPresets() {
    const enc = document.getElementById("encoder");
    const preset = document.getElementById("preset");
    if (!enc || !preset) return;
    const opts = PRESETS[enc.value] || ["medium"];
    const wanted = preset.dataset.initial || DEFAULT_PRESET[enc.value];
    preset.innerHTML = "";
    opts.forEach((o) => {
      const el = document.createElement("option");
      el.value = o; el.textContent = o;
      if (o === wanted) el.selected = true;
      preset.appendChild(el);
    });
    preset.dataset.initial = "";
  }

  function syncCrf() {
    const num = document.getElementById("crf");
    const range = document.getElementById("crf-range");
    if (!num || !range) return;
    range.addEventListener("input", () => { num.value = range.value; });
    num.addEventListener("input", () => { range.value = num.value; });
  }

  function countSelected() {
    const out = document.getElementById("sel-count");
    if (!out) return;
    const update = () => {
      out.textContent = document.querySelectorAll(".file-cb:checked").length;
    };
    document.querySelectorAll(".file-cb").forEach((cb) =>
      cb.addEventListener("change", update));
    const all = document.getElementById("check-all");
    if (all) all.addEventListener("change", () => {
      document.querySelectorAll(".file-cb").forEach((cb) => { cb.checked = all.checked; });
      update();
    });
    update();
  }

  function init() {
    const enc = document.getElementById("encoder");
    if (enc) {
      // The preset <select> carries data-initial="<server default>" so the
      // first fill selects it; afterwards we fall back to per-encoder defaults.
      enc.addEventListener("change", fillPresets);
      fillPresets();
    }
    syncCrf();
    countSelected();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
