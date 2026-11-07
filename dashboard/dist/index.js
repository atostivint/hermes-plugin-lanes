/* Lanes — dashboard tab. Plain IIFE, no build step: React and the UI kit come
   from window.__HERMES_PLUGIN_SDK__, so this file stays a few KB. */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useCallback = hooks.useCallback;

  var C = SDK.components || {};
  var Card = C.Card || "div";
  var CardHeader = C.CardHeader || "div";
  var CardTitle = C.CardTitle || "div";
  var CardContent = C.CardContent || "div";
  var Button = C.Button || "button";
  var Badge = C.Badge;

  var fetchJSON = SDK.fetchJSON;
  var API = "/api/plugins/lanes";

  function badge(text) {
    if (Badge) return h(Badge, { variant: "secondary" }, text);
    return h("span", { className: "lanes-chip" }, text);
  }

  function dur(seconds) {
    var s = Math.max(0, Math.round(seconds || 0));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  }

  function LanesPage() {
    var [data, setData] = useState(null);
    var [err, setErr] = useState(null);
    var [busy, setBusy] = useState(false);
    var [notice, setNotice] = useState(null);

    var load = useCallback(function () {
      return fetchJSON(API + "/state?probe=1")
        .then(function (d) {
          setData(d);
          setErr(null);
        })
        .catch(function (e) {
          setErr(String((e && e.message) || e));
        });
    }, []);

    useEffect(function () {
      load();
      var timer = setInterval(load, 15000);
      return function () {
        clearInterval(timer);
      };
    }, [load]);

    var runTick = useCallback(function () {
      setBusy(true);
      setNotice(null);
      fetchJSON(API + "/tick", { method: "POST" })
        .then(function (r) {
          setNotice(r);
          return load();
        })
        .catch(function (e) {
          setErr(String((e && e.message) || e));
        })
        .then(function () {
          setBusy(false);
        });
    }, [load]);

    var toggleLane = useCallback(function (lane) {
      fetchJSON(API + "/lanes/" + encodeURIComponent(lane.id) + "/enabled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !lane.enabled }),
      })
        .then(load)
        .catch(function (e) {
          setErr(String((e && e.message) || e));
        });
    }, [load]);

    var cancelTask = useCallback(function (job) {
      if (!window.confirm("Stop agent " + job.handle + " and park card " + job.task_id + "?")) return;
      fetchJSON(API + "/cards/" + encodeURIComponent(job.task_id) + "/cancel", { method: "POST" })
        .then(load)
        .catch(function (e) {
          setErr(String((e && e.message) || e));
        });
    }, [load]);

    if (!data && !err) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Lanes")),
        h(CardContent, null, h("div", { className: "lanes-empty" }, "Loading lanes…")));
    }

    var lanes = (data && data.lanes) || [];
    var jobs = (data && data.jobs) || [];
    var last = (data && data.last_tick) || {};
    var counts = (data && data.counts) || {};

    var laneChips = lanes.map(function (lane) {
      var state = lane.enabled === false ? "warn" : (lane.adapter_ok === false ? "bad" : "ok");
      var label = lane.id + " · " + lane.adapter + " · " + lane.board + "/" + lane.assignee;
      return h("button", {
        key: lane.id,
        className: "lanes-chip",
        title: lane.enabled === false
          ? "Disabled — click to enable"
          : "Click to disable this lane",
        onClick: function () { toggleLane(lane); },
        style: { cursor: "pointer" },
      },
        h("span", { className: "lanes-dot", "data-state": state }),
        h("strong", null, lane.id),
        h("span", null, lane.adapter),
        h("span", null, lane.board + "/" + lane.assignee),
        h("span", null, "ready " + lane.ready + " · running " + lane.running + "/" + lane.max_concurrent),
        lane.adapter_detail ? h("span", { className: lane.adapter_ok ? "lanes-mono" : "lanes-mono lanes-err" },
          String(lane.adapter_detail).slice(0, 80)) : null
      );
    });

    var boardCounts = Object.keys(counts).map(function (board) {
      var c = counts[board] || {};
      var parts = Object.keys(c).sort().map(function (k) { return k + " " + c[k]; });
      return h("div", { key: board, className: "lanes-mono" }, board + ": " + parts.join(" · "));
    });

    var rows = jobs.length
      ? jobs.map(function (job) {
          return h("tr", { key: job.task_id },
            h("td", null,
              h("div", null, job.title || job.task_id),
              h("div", { className: "lanes-mono" }, job.task_id + " · " + job.board)),
            h("td", null, job.lane),
            h("td", { className: "lanes-mono" }, job.adapter),
            h("td", { className: "lanes-mono" }, String(job.handle).slice(0, 18)),
            h("td", { className: "lanes-mono" }, job.run ? "run " + job.run : "—"),
            h("td", null, dur(job.elapsed_s)),
            h("td", null,
              h(Button, {
                variant: "outline",
                size: "sm",
                onClick: function () { cancelTask(job); },
              }, "Cancel"))
          );
        })
      : null;

    var logLines = (last.actions || []).map(function (line) { return "· " + line; });
    (last.errors || []).forEach(function (line) { logLines.push("! " + line); });
    if (!logLines.length) logLines.push("nothing to do on the last tick");

    return h("div", { className: "space-y-4" },
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "lanes-bar" },
            h("div", null,
              h(CardTitle, null, "External lanes"),
              h("p", { className: "text-xs text-muted-foreground" },
                "Cards whose assignee is not a Hermes profile. A lane claims the card, ",
                "starts one agent through its adapter, then closes the run on the board.")),
            h("div", { className: "lanes-actions" },
              badge("auto-dispatch " + (data && data.auto_dispatch ? "on" : "off")),
              badge("tick " + ((data && data.tick_seconds) || 60) + "s"),
              h(Button, { variant: "outline", size: "sm", onClick: load }, "Refresh"),
              h(Button, { size: "sm", onClick: runTick, disabled: busy },
                busy ? "Ticking…" : "Run tick")))),
        h(CardContent, null,
          h("div", { className: "lanes-chips", style: { marginBottom: "0.75rem" } }, laneChips),
          boardCounts,
          data && data.auto_dispatch === false
            ? h("p", { className: "text-xs text-muted-foreground", style: { marginTop: "0.5rem" } },
                "Auto-dispatch is off: lanes only observe. Turn it on in config.json to let them claim cards.")
            : null,
          err ? h("p", { className: "text-xs lanes-err", style: { marginTop: "0.5rem" } }, err) : null)),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Running outside (" + jobs.length + ")")),
        h(CardContent, null,
          rows
            ? h("table", { className: "lanes-table" },
                h("thead", null,
                  h("tr", null,
                    h("th", null, "Card"),
                    h("th", null, "Lane"),
                    h("th", null, "Adapter"),
                    h("th", null, "Agent"),
                    h("th", null, "Run"),
                    h("th", null, "Elapsed"),
                    h("th", null, ""))),
                h("tbody", null, rows))
            : h("div", { className: "lanes-empty" },
                "No external agent is running on a card right now."))),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Last tick")),
        h(CardContent, null,
          h("pre", { className: "lanes-log" }, logLines.join("\n")),
          notice ? h("p", { className: "text-xs text-muted-foreground", style: { marginTop: "0.5rem" } },
            "Manual tick: " + (notice.dispatched || 0) + " dispatched, " +
            (notice.closed || 0) + " closed, " + (notice.blocked || 0) + " blocked.") : null)));
  }

  registry.register("lanes", LanesPage);
})();
