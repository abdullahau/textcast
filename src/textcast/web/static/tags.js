/* Tag input: chips plus a hidden comma-separated field, so the form still
   posts as a plain form and works without JavaScript beyond this file. */
(function () {
  "use strict";

  document.querySelectorAll("[data-tag-input]").forEach(function (box) {
    var entry = box.querySelector("[data-tag-entry]");
    var hidden = box.parentElement.querySelector("[data-tag-values]");
    var suggestions = box.parentElement.querySelector(".tag-suggestions");
    var tags = [];

    function sync() {
      hidden.value = tags.join(",");
      box.querySelectorAll(".tag-chip").forEach(function (c) { c.remove(); });
      tags.forEach(function (name) {
        var chip = document.createElement("span");
        chip.className = "tag-chip";
        chip.textContent = name;

        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", "Remove tag " + name);
        remove.addEventListener("click", function () {
          tags = tags.filter(function (t) { return t !== name; });
          sync();
        });

        chip.appendChild(remove);
        box.insertBefore(chip, entry);
      });
      if (suggestions) {
        suggestions.querySelectorAll("[data-tag-suggest]").forEach(function (b) {
          b.classList.toggle("on", tags.indexOf(b.dataset.tagSuggest) !== -1);
        });
      }
    }

    function add(raw) {
      var name = (raw || "").trim().replace(/,+$/, "");
      if (name && tags.indexOf(name) === -1) tags.push(name);
      entry.value = "";
      sync();
    }

    entry.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        add(entry.value);
      } else if (event.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        sync();
      }
    });
    entry.addEventListener("blur", function () { if (entry.value.trim()) add(entry.value); });

    if (suggestions) {
      suggestions.querySelectorAll("[data-tag-suggest]").forEach(function (button) {
        button.addEventListener("click", function () {
          var name = button.dataset.tagSuggest;
          if (tags.indexOf(name) === -1) add(name);
          else { tags = tags.filter(function (t) { return t !== name; }); sync(); }
        });
      });
    }

    // Pre-fill from an existing value, for the article page.
    if (hidden.value) { tags = hidden.value.split(",").filter(Boolean); sync(); }
  });
})();
