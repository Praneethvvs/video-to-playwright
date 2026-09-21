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
