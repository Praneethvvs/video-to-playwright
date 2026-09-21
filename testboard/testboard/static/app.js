// No framework, no bundler, no vendored library. EventSource is native and, per spec, resends
// Last-Event-ID on reconnect — which is the whole reason the server numbers every line.

(function () {
  "use strict";

  // --- confirmation for anything that mutates the shared environment ---------------------
  // Deliberately on the button rather than the form, so the wording can name the specific test
  // and the environment it is about to change.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-confirm]");
    if (!button) return;
    var message = button.getAttribute("data-confirm");
    if (!window.confirm(message + "\n\nRun it anyway?")) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    // The server refuses a destructive run unless it is told the person was asked. Saying so is
    // the client's job and only happens after they actually agreed, so a direct POST that skips
    // this dialog is still rejected.
    var form = button.form;
    if (form && !form.querySelector('input[name="confirmed"]')) {
      var field = document.createElement("input");
      field.type = "hidden";
      field.name = "confirmed";
      field.value = "yes";
      form.appendChild(field);
    }
  });

  // --- timestamps -------------------------------------------------------------------------
  // The server stores and sends UTC because a container's clock is not the reader's clock.
  // Turning it into something human is the browser's job, since only it knows the timezone.
  var UNITS = [
    [60, "s", 1],
    [3600, "min", 60],
    [86400, "h", 3600],
    [2592000, "d", 86400],
  ];

  function relative(then, now) {
    var seconds = Math.round((now - then) / 1000);
    if (seconds < 0) seconds = 0;          // clock skew reads as "just now", never as the future
    if (seconds < 45) return "just now";
    for (var i = 0; i < UNITS.length; i++) {
      if (seconds < UNITS[i][0]) {
        return Math.round(seconds / UNITS[i][2]) + " " + UNITS[i][1] + " ago";
      }
    }
    return Math.round(seconds / 2592000) + " mo ago";
  }

  function paintTimes() {
    var now = Date.now();
    document.querySelectorAll("time.t").forEach(function (el) {
      var parsed = Date.parse(el.getAttribute("datetime"));
      if (isNaN(parsed)) return;
      el.textContent = relative(parsed, now);
      el.title = new Date(parsed).toLocaleString();
    });
  }

  paintTimes();
  setInterval(paintTimes, 30000);

  // --- live log ---------------------------------------------------------------------------
  var log = document.getElementById("log");
  if (!log) return;

  var runId = log.dataset.run;
  var live = log.dataset.live === "yes";
  var pinned = true;

  log.addEventListener("scroll", function () {
    // Stop yanking the view back to the bottom once someone has scrolled up to read something.
    pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  });

  function append(text) {
    log.appendChild(document.createTextNode(text + "\n"));
    if (pinned) log.scrollTop = log.scrollHeight;
  }

  if (!live) {
    fetch("/runs/" + runId + "/log")
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (t) { log.textContent = t || "(no output was captured)"; })
      .catch(function () { log.textContent = "(could not load the log)"; });
    return;
  }

  var source = new EventSource("/runs/" + runId + "/stream");
  var finished = false;

  source.onmessage = function (event) { append(event.data); };

  source.addEventListener("done", function () {
    finished = true;
    source.close();
    // The status, totals and artifacts are rendered server-side, so one reload is both the
    // simplest and the most accurate way to show the finished state.
    setTimeout(function () { window.location.reload(); }, 600);
  });

  source.onerror = function () {
    if (finished) return;
    // EventSource reconnects on its own and replays from Last-Event-ID. Nothing to do but say so.
    append("… connection interrupted, reconnecting");
  };
})();
