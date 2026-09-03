/* Open a figure full screen, and let it be zoomed and moved.
 *
 * A chart is the one thing in an article that a reading measure is wrong for:
 * 36rem of column is right for prose and too narrow to read an axis in. Rather
 * than let the picture break out of the column and unsettle the page, it opens
 * over it.
 *
 * Its own file, and not part of player.js, because a figure is there whether
 * or not the article has audio and player.js only loads when it does.
 */
(function () {
  "use strict";

  var doc = document.getElementById("doc");
  if (!doc) return;

  var overlay = null;
  var image = null;
  var scale = 1;
  var x = 0;
  var y = 0;
  var dragging = null;

  var MIN = 1;
  var MAX = 6;

  function apply() {
    image.style.transform = "translate(" + x + "px," + y + "px) scale(" + scale + ")";
    image.style.cursor = scale > 1 ? "grab" : "zoom-in";
    overlay.querySelector(".lb-out").disabled = scale <= MIN;
    overlay.querySelector(".lb-in").disabled = scale >= MAX;
  }

  function zoom(factor, originX, originY) {
    var next = Math.min(MAX, Math.max(MIN, scale * factor));
    if (next === scale) return;
    if (originX !== undefined) {
      /* Keep whatever is under the pointer under the pointer, or zooming
         walks the part you were reading off the screen. */
      var box = image.getBoundingClientRect();
      var dx = originX - (box.left + box.width / 2);
      var dy = originY - (box.top + box.height / 2);
      var ratio = next / scale - 1;
      x -= dx * ratio;
      y -= dy * ratio;
    }
    scale = next;
    if (scale === MIN) { x = 0; y = 0; }
    apply();
  }

  function close() {
    if (!overlay) return;
    overlay.remove();
    document.body.classList.remove("lb-open");
    overlay = null;
    image = null;
  }

  function open(src, caption) {
    close();
    scale = 1; x = 0; y = 0;

    overlay = document.createElement("div");
    overlay.className = "lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", caption || "Picture");
    overlay.innerHTML =
      '<div class="lb-bar">' +
      '<button type="button" class="lb-out" aria-label="Zoom out">&minus;</button>' +
      '<button type="button" class="lb-in" aria-label="Zoom in">+</button>' +
      '<button type="button" class="lb-close" aria-label="Close">&times;</button>' +
      "</div>" +
      '<div class="lb-stage"><img alt=""></div>' +
      (caption ? '<p class="lb-cap"></p>' : "");

    image = overlay.querySelector("img");
    image.src = src;
    image.alt = caption || "";
    if (caption) overlay.querySelector(".lb-cap").textContent = caption;

    overlay.querySelector(".lb-close").addEventListener("click", close);
    overlay.querySelector(".lb-in").addEventListener("click", function () { zoom(1.5); });
    overlay.querySelector(".lb-out").addEventListener("click", function () { zoom(1 / 1.5); });

    /* A click on the backdrop closes; a click on the picture zooms. */
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay || event.target.classList.contains("lb-stage")) close();
    });
    image.addEventListener("click", function (event) {
      event.stopPropagation();
      if (dragging && dragging.moved) return;
      zoom(scale >= MAX ? MIN / scale : 2, event.clientX, event.clientY);
    });
    overlay.addEventListener("wheel", function (event) {
      event.preventDefault();
      zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY);
    }, { passive: false });

    image.addEventListener("pointerdown", function (event) {
      if (scale <= MIN) return;
      dragging = { id: event.pointerId, fromX: event.clientX - x, fromY: event.clientY - y, moved: false };
      image.setPointerCapture(event.pointerId);
      image.style.cursor = "grabbing";
    });
    image.addEventListener("pointermove", function (event) {
      if (!dragging || dragging.id !== event.pointerId) return;
      x = event.clientX - dragging.fromX;
      y = event.clientY - dragging.fromY;
      dragging.moved = true;
      apply();
    });
    function release(event) {
      if (!dragging || dragging.id !== event.pointerId) return;
      /* Cleared on the next tick so the click that follows the drag can see
         that it was a drag and not open the zoom. */
      var finished = dragging;
      setTimeout(function () { if (dragging === finished) dragging = null; }, 0);
      apply();
    }
    image.addEventListener("pointerup", release);
    image.addEventListener("pointercancel", release);

    document.body.classList.add("lb-open");
    document.body.appendChild(overlay);
    apply();
    overlay.querySelector(".lb-close").focus();
  }

  doc.addEventListener("click", function (event) {
    var picture = event.target.closest("figure.b.figure img");
    if (!picture) return;
    var figure = picture.closest("figure");
    var caption = figure.querySelector("figcaption");
    open(picture.currentSrc || picture.src, caption ? caption.textContent.trim() : "");
  });

  document.addEventListener("keydown", function (event) {
    if (!overlay) return;
    if (event.key === "Escape") close();
    else if (event.key === "+" || event.key === "=") zoom(1.5);
    else if (event.key === "-") zoom(1 / 1.5);
  });
})();
