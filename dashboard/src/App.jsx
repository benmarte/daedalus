/**
 * Daedalus Dashboard — project grid + per-project config modal.
 *
 * Compiled to dist/index.js by `npm run build` (esbuild → single IIFE).
 * NEVER hand-edit dist/index.js — it is generated and your changes will be lost.
 *
 * Hermes dashboard-plugin contract:
 *   - React + UI primitives come from window.__HERMES_PLUGIN_SDK__ (React is
 *     NEVER bundled; esbuild's JSX factory resolves to the in-scope `React`).
 *   - The tab is registered via window.__HERMES_PLUGINS__.register(name, App)
 *     where `name` MUST match manifest.json "name" ("daedalus").
 *   - Backend routes live in plugin_api.py at /api/plugins/daedalus/.
 *
 * Two views:
 *   1. Project grid: GET /api/plugins/daedalus/projects — cards with
 *      kanban counts, PRs, CI badges, cron, needs-attention, tracking mode.
 *   2. Config modal: GET/POST /api/plugins/daedalus/project/{name}/config —
 *      edit tracking, vcs, sources, cron; repo/workdir is read-only.
 */

var SDK = window.__HERMES_PLUGIN_SDK__;
if (!SDK) throw new Error("Hermes Plugin SDK not loaded");
var plugins = window.__HERMES_PLUGINS__;
var React = SDK.React;
var useState = SDK.hooks.useState;
var useEffect = SDK.hooks.useEffect;
var useCallback = SDK.hooks.useCallback;

// Discover available SDK components at runtime (fall back to raw HTML/JSX).
var SdkComponents = (SDK.components || {});
var SdkButton = SdkComponents.Button || null;
var SdkCheckbox = SdkComponents.Checkbox || null;
// Input, Select, SelectOption are known to break React 19 — always use raw HTML.
// Separator works visually; use if available, else plain <hr/>.

// Cron schedule parse/build helpers (own module for unit-testing).
var cronSchedule = require("./cronSchedule");
var parseSchedule = cronSchedule.parseSchedule;
var buildSchedule = cronSchedule.buildSchedule;

// Provider-aware VCS field metadata + notification events (own module).
var providerFields = require("./providerFields");
var PROVIDERS = providerFields.PROVIDERS;
var PROVIDER_LABELS = providerFields.PROVIDER_LABELS;
var NOTIFY_EVENTS = providerFields.NOTIFY_EVENTS;
var repoLabelForProvider = providerFields.repoLabelForProvider;
var repoPlaceholderForProvider = providerFields.repoPlaceholderForProvider;

// Coding-agent metadata + pure helpers (own module for unit-testing).
var codingAgent = require("./codingAgent");

// Pure dirty-state helper for the config modal (own module for unit-testing).
var configDirty = require("./configDirty");

// Resolve a JSON-returning fetch from whatever the SDK exposes.
var fetchJSON = SDK.fetchJSON;
if (!fetchJSON && SDK.authedFetch) {
  fetchJSON = function (url, opts) { return SDK.authedFetch(url, opts).then(function (r) { return r.json(); }); };
}
if (!fetchJSON && window.__HERMES_SESSION_TOKEN__) {
  var _st = window.__HERMES_SESSION_TOKEN__;
  fetchJSON = function (url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["Authorization"] = "Bearer " + _st;
    if (opts.body && typeof opts.body === "object" && !opts.headers["Content-Type"]) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    return fetch(url, opts).then(function (r) { return r.json(); });
  };
}
if (!fetchJSON) throw new Error("No fetch implementation available");

// Normalize all fetchJSON calls: serialize plain-object bodies and set Content-Type.
// This fixes the modal save bug where POST /config sent a raw object without JSON.stringify.
var _rawFetchJSON = fetchJSON;
fetchJSON = function (url, opts) {
  opts = opts || {};
  if (opts.body && typeof opts.body === "object") {
    opts.headers = opts.headers || {};
    if (!opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  return _rawFetchJSON(url, opts);
};

var API_PROJECTS = "/api/plugins/daedalus/projects";
var API_PROJECT_CREATE = "/api/plugins/daedalus/project/create";
var API_UNINSTALL = "/api/plugins/daedalus/meta/uninstall";
var apiProjectConfig = function (name) { return "/api/plugins/daedalus/project/" + encodeURIComponent(name) + "/config"; };
var apiProject = function (name) { return "/api/plugins/daedalus/project/" + encodeURIComponent(name); };
var apiMetaUrl = function (name, endpoint) { return "/api/plugins/daedalus/meta/" + endpoint + "?project=" + encodeURIComponent(name); };

// ── helpers ────────────────────────────────────────────────────────────────
function getIn(obj, path, fallback) {
  var cur = obj;
  for (var i = 0; i < path.length; i++) {
    if (cur == null) return fallback;
    cur = cur[path[i]];
  }
  return cur == null ? fallback : cur;
}

function formatRelativeTime(iso) {
  if (!iso) return null;
  try {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    var now = Date.now();
    var diff = now - d.getTime();
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + "h ago";
    return Math.floor(hrs / 24) + "d ago";
  } catch (e) { return null; }
}

// ── styles ─────────────────────────────────────────────────────────────────
var S = {
  wrap: { padding: "20px", maxWidth: "1200px", margin: "0 auto", fontFamily: "system-ui, sans-serif" },
  h1: { fontSize: "20px", fontWeight: 600, margin: "0 0 4px" },
  subtitle: { color: "#888", fontSize: "12px", margin: "0 0 20px" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: "14px", marginBottom: "20px" },
  card: { border: "1px solid #333", borderRadius: "8px", padding: "16px", background: "rgba(255,255,255,0.02)", cursor: "pointer", transition: "border-color 0.15s", position: "relative", overflow: "hidden" },
  cardHeader: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "10px" },
  cardName: { fontSize: "16px", fontWeight: 600, color: "#eee", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  cardRepo: { fontSize: "12px", color: "#888", fontFamily: "ui-monospace, monospace", marginBottom: "4px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  cardWorkdir: { fontSize: "11px", color: "#666", fontFamily: "ui-monospace, monospace", wordBreak: "break-all" },
  cardSection: { marginTop: "10px", paddingTop: "10px", borderTop: "1px solid #2a2a2a" },
  cardRow: { display: "flex", gap: "12px", flexWrap: "wrap", fontSize: "12px", color: "#aaa" },
  cardRowItem: { display: "flex", alignItems: "center", gap: "4px" },
  cardLabel: { color: "#666", fontSize: "11px" },
  badge: { fontSize: "11px", padding: "1px 6px", borderRadius: "10px", fontWeight: 500, display: "inline-flex", alignItems: "center", gap: "3px" },
  badgeGreen: { background: "rgba(74,222,128,0.15)", color: "#4ade80" },
  badgeRed: { background: "rgba(248,113,113,0.15)", color: "#f87171" },
  badgeYellow: { background: "rgba(250,204,21,0.15)", color: "#facc15" },
  badgeNeutral: { background: "rgba(255,255,255,0.05)", color: "#888" },
  dot: { width: "6px", height: "6px", borderRadius: "50%", display: "inline-block", flexShrink: 0 },
  dotGreen: { background: "#4ade80" },
  dotRed: { background: "#f87171" },
  dotYellow: { background: "#facc15" },
  dotGray: { background: "#555" },
  btn: { padding: "7px 12px", borderRadius: "6px", border: "1px solid #555", background: "#2a2a2a", color: "#eee", cursor: "pointer", fontSize: "13px" },
  btnPrimary: { padding: "9px 18px", borderRadius: "6px", border: "none", background: "#3b82f6", color: "#fff", cursor: "pointer", fontSize: "14px", fontWeight: 600 },
  btnDanger: { padding: "9px 18px", borderRadius: "6px", border: "none", background: "#dc2626", color: "#fff", cursor: "pointer", fontSize: "14px", fontWeight: 600 },
  btnSmall: { padding: "4px 8px", borderRadius: "4px", border: "1px solid #444", background: "transparent", color: "#aaa", cursor: "pointer", fontSize: "11px" },
  err: { color: "#f87171", fontSize: "13px", margin: "4px 0" },
  ok: { color: "#4ade80", fontSize: "13px" },
  section: { fontSize: "13px", fontWeight: 600, color: "#ccc", textTransform: "uppercase", letterSpacing: "0.5px", margin: "20px 0 10px" },

  // Modal
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" },
  modal: { background: "#1a1a1a", borderRadius: "12px", padding: "24px", maxWidth: "600px", width: "90%", maxHeight: "85vh", overflowY: "auto", border: "1px solid #333", position: "relative" },
  modalHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" },
  modalTitle: { fontSize: "18px", fontWeight: 600 },
  fieldRow: { display: "flex", gap: "12px", flexWrap: "wrap", marginBottom: "12px" },
  field: { display: "flex", flexDirection: "column", flex: "1 1 200px", minWidth: "160px" },
  fieldLabel: { fontSize: "12px", color: "#aaa", marginBottom: "4px" },
  input: { padding: "7px 9px", borderRadius: "6px", border: "1px solid #444", background: "#1b1b1b", color: "#eee", fontSize: "13px" },
  inputDisabled: { padding: "7px 9px", borderRadius: "6px", border: "1px solid #333", background: "#111", color: "#666", fontSize: "13px", cursor: "not-allowed" },
  readOnlyText: { padding: "7px 9px", borderRadius: "6px", border: "1px solid #2a2a2a", background: "rgba(255,255,255,0.03)", color: "#999", fontSize: "13px", fontFamily: "ui-monospace, monospace", wordBreak: "break-all", minHeight: "33px", display: "flex", alignItems: "center" },
  select: { padding: "7px 9px", borderRadius: "6px", border: "1px solid #444", background: "#1b1b1b", color: "#eee", fontSize: "13px" },
  toggleRow: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px", fontSize: "13px", color: "#ccc" },
  modalBar: { display: "flex", gap: "12px", alignItems: "center", padding: "16px 0 0", marginTop: "16px", borderTop: "1px solid #2a2a2a" },
  fieldErr: { color: "#f87171", fontSize: "11px", marginTop: "2px" },
  // TagMultiSelect chips
  chipWrap: { display: "flex", flexWrap: "wrap", gap: "6px", alignItems: "center", width: "100%" },
  chip: { display: "inline-flex", alignItems: "center", gap: "5px", padding: "4px 4px 4px 10px", borderRadius: "16px", border: "1px solid #444", background: "rgba(255,255,255,0.04)", fontSize: "12px", color: "#ddd", lineHeight: "1.4" },
  chipDot: { width: "8px", height: "8px", borderRadius: "50%", flexShrink: 0 },
  chipLabel: { whiteSpace: "nowrap" },
  chipRemove: { padding: "0 5px", border: "none", background: "transparent", color: "#999", cursor: "pointer", fontSize: "14px", fontWeight: 700, lineHeight: "1", borderRadius: "50%" },
  chipEmptyHint: { fontSize: "12px", color: "#666", fontStyle: "italic", padding: "4px 0" },
};

// ── helpers ─────────────────────────────────────────────────────────────────
function Button(props) {
  if (SdkButton) {
    return React.createElement(SdkButton, props, props.label || props.children);
  }
  return React.createElement("button", {
    style: props.variant === "primary" ? S.btnPrimary : props.variant === "danger" ? S.btnDanger : props.variant === "small" ? S.btnSmall : S.btn,
    disabled: props.disabled || false,
    onClick: props.onClick,
    type: props.type || "button",
  }, props.label || props.children);
}

function Checkbox(props) {
  var labelSpan = React.createElement(
    "span",
    { style: { fontSize: "13px", color: "#ccc" } },
    props.label || ""
  );
  if (SdkCheckbox) {
    // The SDK Checkbox does not render a `label` prop, so render the label
    // ourselves alongside the control — otherwise the toggle shows with no text.
    return React.createElement("div", { style: S.toggleRow },
      React.createElement(SdkCheckbox, {
        checked: props.checked || false,
        onCheckedChange: function (v) { if (props.onChange) props.onChange(!!v); },
      }),
      labelSpan
    );
  }
  return React.createElement("label", { style: S.toggleRow },
    React.createElement("input", {
      type: "checkbox",
      checked: props.checked || false,
      onChange: function (e) { if (props.onChange) props.onChange(e.target.checked); },
      style: { margin: 0 },
    }),
    labelSpan
  );
}

function Select(props) {
  var opts = props.options || [];
  return React.createElement("select", {
    style: S.select,
    value: props.value == null ? "" : props.value,
    onChange: function (e) { props.onChange(e.target.value); },
  },
    React.createElement("option", { value: "" }, props.empty || "— none —"),
    opts.map(function (o) { return React.createElement("option", { key: o, value: o }, o); })
  );
}

// ── TagMultiSelect ──────────────────────────────────────────────────────────
function TagMultiSelect(props) {
  var selected = props.selected || [];
  var options = props.options || [];
  var placeholder = props.placeholder || "+ add\u2026";
  var emptyHint = props.emptyHint || "no options found";

  var availableOptions = options.filter(function (opt) {
    return selected.indexOf(opt.value) === -1;
  });

  function remove(val) {
    if (props.onChange) {
      props.onChange(selected.filter(function (v) { return v !== val; }));
    }
  }

  function handleAdd(e) {
    var val = e.target.value;
    if (!val) return;
    if (props.onChange) {
      props.onChange(selected.concat([val]));
    }
    // Reset the select back to placeholder
    e.target.value = "";
  }

  return React.createElement("div", null,
    // Chips row
    React.createElement("div", { style: S.chipWrap },
      selected.map(function (val) {
        var opt = null;
        for (var i = 0; i < options.length; i++) {
          if (options[i].value === val) { opt = options[i]; break; }
        }
        var labelText = opt ? opt.label : val;
        var colorDot = opt && opt.color ? React.createElement("span", {
          style: Object.assign({}, S.chipDot, { background: "#" + opt.color })
        }) : null;
        return React.createElement("span", { key: val, style: S.chip },
          colorDot,
          React.createElement("span", { style: S.chipLabel }, labelText),
          React.createElement("button", {
            style: S.chipRemove,
            onClick: function (e) { e.preventDefault(); remove(val); },
            title: "Remove " + labelText,
            type: "button"
          }, "\u00d7")
        );
      })
    ),
    // Add dropdown
    React.createElement("select", {
      style: Object.assign({}, S.select, { marginTop: selected.length > 0 ? "8px" : "2px", width: "100%" }),
      value: "",
      onChange: handleAdd,
      disabled: options.length === 0
    },
      React.createElement("option", { value: "", disabled: true }, placeholder),
      options.length === 0
        ? React.createElement("option", { value: "", disabled: true }, emptyHint)
        : availableOptions.length === 0
          ? React.createElement("option", { value: "", disabled: true }, "all selected")
          : availableOptions.map(function (opt) {
              return React.createElement("option", { key: opt.value, value: opt.value }, opt.label);
            })
    )
  );
}

// ── project card ────────────────────────────────────────────────────────────
function ProjectCard(props) {
  var p = props.project;
  var hasAttention = p.needs_attention && p.needs_attention.length > 0;
  var openPrs = p.open_prs;
  var hasRedCI = openPrs && openPrs.prs && openPrs.prs.some(function (pr) { return pr.ci_status === "red"; });
  var cardBorder = hasAttention ? "1px solid #f87171" : hasRedCI ? "1px solid #dc2626" : S.card.border;

  var kanbanCounts = p.kanban_summary;
  var cronInfo = p.cron;
  var trackingMode = p.tracking_mode;
  var sources = p.sources;

  var attentionCount = hasAttention ? p.needs_attention.length : 0;
  var prCount = openPrs ? openPrs.count : 0;

  return React.createElement("div", {
    style: Object.assign({}, S.card, { border: cardBorder }),
    onClick: function () { props.onSelect(p.name); },
  },
    // Header row: name + badges
    React.createElement("div", { style: S.cardHeader },
      React.createElement("div", { style: { minWidth: 0, flex: "1 1 auto" } },
        React.createElement("div", { style: S.cardName }, p.name || "(unnamed)"),
        React.createElement("div", { style: S.cardRepo }, p.repo || "—")
      ),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: "4px", alignItems: "flex-end", flexShrink: 0 } },
        hasAttention ? React.createElement("span", { style: Object.assign({}, S.badge, S.badgeRed), onClick: function (e) { e.stopPropagation(); } },
          "⚠ " + attentionCount + " needing attention") : null,
        hasRedCI ? React.createElement("span", { style: Object.assign({}, S.badge, S.badgeRed) },
          "● CI failing") : null,
        React.createElement("span", { style: Object.assign({}, S.badge, S.badgeNeutral) },
          trackingMode || "kanban")
      )
    ),

    // Stats row
    React.createElement("div", { style: S.cardSection },
      React.createElement("div", { style: S.cardRow },
        // Kanban counts — null means board unavailable, {} means board exists but empty
        kanbanCounts !== null && kanbanCounts !== undefined
          ? (Object.keys(kanbanCounts).length > 0
              ? Object.keys(kanbanCounts).sort().map(function (status) {
                  var dotColor = {};
                  if (status === "done") dotColor = S.dotGreen;
                  else if (status === "in_progress") dotColor = S.dotYellow;
                  else if (status === "blocked" || status === "gave_up") dotColor = S.dotRed;
                  else dotColor = S.dotGray;
                  return React.createElement("div", { key: status, style: S.cardRowItem },
                    React.createElement("span", { style: Object.assign({}, S.dot, dotColor) }),
                    status + ": " + kanbanCounts[status]
                  );
                })
              : React.createElement("span", { style: S.cardLabel }, "board ready"))
          : React.createElement("span", { style: S.cardLabel }, "no kanban data"),

        // PRs
        openPrs ? React.createElement("div", { style: S.cardRowItem },
          React.createElement("span", { style: Object.assign({}, S.dot, openPrs.prs && openPrs.prs.every(function (p2) { return p2.ci_status === "green"; }) ? S.dotGreen : S.dotYellow) }),
          prCount + " open PR" + (prCount !== 1 ? "s" : "")
        ) : null,

        // Cron
        cronInfo && cronInfo.name ? React.createElement("div", { style: S.cardRowItem },
          (function () {
            var h = cronInfo.health;
            if (!h) return null;
            var badgeStyle = S.badgeNeutral;
            var badgeText = "no cron";
            if (h.found && h.state === "active" && h.last_status === "ok") {
              badgeStyle = S.badgeGreen;
              badgeText = "cron ok";
            } else if (h.found && h.last_status && h.last_status !== "ok") {
              badgeStyle = S.badgeRed;
              badgeText = "cron error";
            } else if (h.found && h.state === "paused") {
              badgeStyle = S.badgeYellow;
              badgeText = "cron paused";
            }
            return React.createElement("span", {
              style: Object.assign({}, S.badge, badgeStyle, { cursor: "default" }),
              onClick: function (e) { e.stopPropagation(); }
            }, badgeText);
          })()
        ) : null,
        cronInfo && cronInfo.schedule ? React.createElement("div", { style: S.cardRowItem },
          React.createElement("span", { style: Object.assign({}, S.dot, S.dotGray) }),
          cronInfo.schedule
        ) : null,

        cronInfo && cronInfo.last_run ? React.createElement("div", { style: S.cardRowItem },
          React.createElement("span", { style: Object.assign({}, S.dot, S.dotGray) }),
          "last run " + formatRelativeTime(cronInfo.last_run)
        ) : null
      )
    )
  );
}

// ── CronSchedule component ──────────────────────────────────────────────────
// Replaces the free-text cron schedule input with structured dropdowns that
// only emit schedules Hermes cron accepts.
var CRON_UNITS = ["Minutes", "Hours", "Days", "Weekly", "Monthly", "Custom (cron)"];

var MINUTE_VALUES = ["1", "2", "3", "5", "10", "15", "20", "30", "45", "60"];
var HOUR_VALUES = ["1", "2", "3", "4", "6", "8", "12"];
var DAY_VALUES = ["1", "2", "3", "5", "7"];

var HOUR_OPTIONS = [];
for (var hi = 0; hi < 24; hi++) { HOUR_OPTIONS.push(String(hi)); }
var DOM_OPTIONS = [];
for (var di = 1; di <= 28; di++) { DOM_OPTIONS.push(String(di)); }

var DOW_LABELS = {
  "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
  "4": "Thursday", "5": "Friday", "6": "Saturday"
};

function CronSchedule(props) {
  var savedValue = props.value || "";
  var parsed = parseSchedule(savedValue);

  // Local state so the dropdowns feel responsive before emitting.
  var u = useState(parsed.unit || "Minutes");
  var unit = u[0], setUnit = u[1];
  var nv = useState(parsed.n || "60");
  var n = nv[0], setN = nv[1];
  var dv = useState(parsed.dow || "1");
  var dow = dv[0], setDow = dv[1];
  var hv = useState(parsed.hour || "9");
  var hour = hv[0], setHour = hv[1];
  var mv = useState(parsed.minute || "0");
  var minute = mv[0], setMinute = mv[1];
  var domv = useState(parsed.dom || "1");
  var dom = domv[0], setDom = domv[1];
  var rv = useState(parsed.raw || "");
  var customRaw = rv[0], setCustomRaw = rv[1];

  // Sync local state when saved value changes from outside (e.g. config reload).
  useEffect(function () {
    var p = parseSchedule(savedValue);
    setUnit(p.unit || "Minutes");
    setN(p.n || "60");
    setDow(p.dow || "1");
    setHour(p.hour || "9");
    setMinute(p.minute || "0");
    setDom(p.dom || "1");
    setCustomRaw(p.raw || "");
  }, [savedValue]);

  // Emit the built schedule whenever any dropdown changes.
  function emit(u, n, dow, hour, minute, dom, raw) {
    var state = { unit: u, n: n, dow: dow, hour: hour, minute: minute, dom: dom, raw: raw };
    var schedule = buildSchedule(state);
    if (props.onChange) props.onChange(schedule);
  }

  function onUnitChange(nextUnit) {
    setUnit(nextUnit);
    emit(nextUnit, n, dow, hour, minute, dom, customRaw);
  }

  function onNChange(nextN) {
    setN(nextN);
    emit(unit, nextN, dow, hour, minute, dom, customRaw);
  }

  function onDowChange(nextDow) {
    setDow(nextDow);
    emit(unit, n, nextDow, hour, minute, dom, customRaw);
  }

  function onHourChange(nextHour) {
    setHour(nextHour);
    emit(unit, n, dow, nextHour, minute, dom, customRaw);
  }

  function onMinuteChange(nextMinute) {
    setMinute(nextMinute);
    emit(unit, n, dow, hour, nextMinute, dom, customRaw);
  }

  function onDomChange(nextDom) {
    setDom(nextDom);
    emit(unit, n, dow, hour, minute, nextDom, customRaw);
  }

  function onCustomChange(val) {
    setCustomRaw(val);
    // For Custom, emit the raw text verbatim.
    if (props.onChange) props.onChange(val);
  }

  // Build the second control row based on unit selection.
  var secondRow = null;

  if (unit === "Minutes" || unit === "Hours" || unit === "Days") {
    var values;
    if (unit === "Minutes") values = MINUTE_VALUES;
    else if (unit === "Hours") values = HOUR_VALUES;
    else values = DAY_VALUES;

    secondRow = React.createElement("label", { style: S.field },
      React.createElement("span", { style: S.fieldLabel }, "Every"),
      React.createElement("select", {
        style: S.select,
        value: n,
        onChange: function (e) { onNChange(e.target.value); }
      },
        values.map(function (v) {
          return React.createElement("option", { key: v, value: v }, v);
        })
      )
    );
  } else if (unit === "Weekly") {
    secondRow = [
      React.createElement("label", { key: "dow", style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Day"),
        React.createElement("select", {
          style: S.select,
          value: dow,
          onChange: function (e) { onDowChange(e.target.value); }
        },
          ["0","1","2","3","4","5","6"].map(function (d) {
            return React.createElement("option", { key: d, value: d }, DOW_LABELS[d]);
          })
        )
      ),
      React.createElement("label", { key: "hour", style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Time (hour)"),
        React.createElement("select", {
          style: S.select,
          value: hour,
          onChange: function (e) { onHourChange(e.target.value); }
        },
          HOUR_OPTIONS.map(function (h) {
            var padded = h.length === 1 ? "0" + h : h;
            return React.createElement("option", { key: h, value: h }, padded + ":00");
          })
        )
      ),
      React.createElement("label", { key: "minute", style: { display: "flex", flexDirection: "column", flex: "0 0 80px", minWidth: "80px" } },
        React.createElement("span", { style: S.fieldLabel }, "Minute"),
        React.createElement("select", {
          style: S.select,
          value: minute,
          onChange: function (e) { onMinuteChange(e.target.value); }
        },
          ["0", "15", "30", "45"].map(function (m) {
            var paddedM = m.length === 1 ? "0" + m : m;
            return React.createElement("option", { key: m, value: m }, ":" + paddedM);
          })
        )
      )
    ];
  } else if (unit === "Monthly") {
    secondRow = [
      React.createElement("label", { key: "dom", style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Day of Month"),
        React.createElement("select", {
          style: S.select,
          value: dom,
          onChange: function (e) { onDomChange(e.target.value); }
        },
          DOM_OPTIONS.map(function (d) {
            return React.createElement("option", { key: d, value: d }, d);
          })
        )
      ),
      React.createElement("label", { key: "hour", style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Time (hour)"),
        React.createElement("select", {
          style: S.select,
          value: hour,
          onChange: function (e) { onHourChange(e.target.value); }
        },
          HOUR_OPTIONS.map(function (h) {
            var padded = h.length === 1 ? "0" + h : h;
            return React.createElement("option", { key: h, value: h }, padded + ":00");
          })
        )
      ),
      React.createElement("label", { key: "minute", style: { display: "flex", flexDirection: "column", flex: "0 0 80px", minWidth: "80px" } },
        React.createElement("span", { style: S.fieldLabel }, "Minute"),
        React.createElement("select", {
          style: S.select,
          value: minute,
          onChange: function (e) { onMinuteChange(e.target.value); }
        },
          ["0", "15", "30", "45"].map(function (m) {
            var paddedM = m.length === 1 ? "0" + m : m;
            return React.createElement("option", { key: m, value: m }, ":" + paddedM);
          })
        )
      )
    ];
  } else if (unit === "Custom (cron)") {
    secondRow = React.createElement("label", { style: S.field },
      React.createElement("span", { style: S.fieldLabel }, "Cron Expression"),
      React.createElement("input", {
        style: S.input,
        value: customRaw,
        placeholder: "e.g. */5 * * * * or every 2h",
        onChange: function (e) { onCustomChange(e.target.value); }
      })
    );
  }

  return React.createElement("div", { style: S.fieldRow },
    React.createElement("label", { style: S.field },
      React.createElement("span", { style: S.fieldLabel }, "Frequency"),
      React.createElement("select", {
        style: S.select,
        value: unit,
        onChange: function (e) { onUnitChange(e.target.value); }
      },
        CRON_UNITS.map(function (u) {
          return React.createElement("option", { key: u, value: u }, u);
        })
      )
    ),
    secondRow
  );
}

// ── MethodChannelPicker ─────────────────────────────────────────────────────
// Reusable platform → channel cascade built from /meta/notifications data.
// props: methods (map method → channel entries), method, target, onMethod(m),
//        onTarget(t)
function MethodChannelPicker(props) {
  var methods = props.methods || {};
  var methodNames = Object.keys(methods).sort();
  var rawChannelOpts = props.method && methods[props.method] ? methods[props.method] : [];
  var channelOpts = rawChannelOpts.map(function (entry) {
    if (typeof entry === "string") return { value: entry, label: entry };
    return entry;
  });
  var target = props.target || "";
  // Only preselect the target if it exists in the channel list; else keep raw.
  var inList = channelOpts.some(function (ch) { return ch.value === target; });

  if (methodNames.length === 0) {
    return React.createElement("input", {
      style: S.input,
      value: target,
      placeholder: "e.g. slack:C123 / discord:#general",
      onChange: function (e) { props.onTarget(e.target.value); },
    });
  }
  return React.createElement("div", { style: { display: "flex", gap: "8px", flex: "1 1 auto" } },
    React.createElement("select", {
      style: Object.assign({}, S.select, { flex: "0 0 130px" }),
      value: props.method || "",
      onChange: function (e) { props.onMethod(e.target.value); },
    },
      React.createElement("option", { value: "" }, "— platform —"),
      methodNames.map(function (m) {
        return React.createElement("option", { key: m, value: m }, m);
      })
    ),
    props.method ? (channelOpts.length > 0 ? React.createElement("select", {
      style: Object.assign({}, S.select, { flex: "1 1 auto" }),
      value: inList ? target : "",
      onChange: function (e) { props.onTarget(e.target.value); },
    },
      React.createElement("option", { value: "" }, "— channel —"),
      channelOpts.map(function (ch) {
        return React.createElement("option", { key: ch.value, value: ch.value }, ch.label);
      })
    ) : React.createElement("input", {
      style: Object.assign({}, S.input, { flex: "1 1 auto" }),
      value: target,
      placeholder: "channel id, e.g. " + props.method.toLowerCase() + ":...",
      onChange: function (e) { props.onTarget(e.target.value); },
    })) : null
  );
}

// ── NotificationsEditor ─────────────────────────────────────────────────────
// Multi-target notifications: each row = platform + channel + event filters.
// props: targets (cron.notifications array), methods (meta/notifications map),
//        onChange(nextArray)
function NotificationsEditor(props) {
  var targets = props.targets || [];
  var methods = props.methods || {};
  var ts = useState({}); var testStatuses = ts[0], setTestStatuses = ts[1];

  function update(i, patch) {
    var next = targets.map(function (t, j) {
      return j === i ? Object.assign({}, t, patch) : t;
    });
    setTestStatuses(function (prev) { var n = Object.assign({}, prev); delete n[i]; return n; });
    props.onChange(next);
  }

  function remove(i) {
    setTestStatuses(function (prev) {
      var n = {};
      Object.keys(prev).forEach(function (k) {
        var ki = parseInt(k, 10);
        if (ki < i) n[k] = prev[k];
        else if (ki > i) n[String(ki - 1)] = prev[k];
      });
      return n;
    });
    props.onChange(targets.filter(function (_, j) { return j !== i; }));
  }

  function add() {
    props.onChange(targets.concat([{ platform: "", target: "", events: [] }]));
  }

  function testRow(i, target) {
    setTestStatuses(function (prev) { return Object.assign({}, prev, { [i]: { ok: null, msg: "Sending…" } }); });
    fetchJSON("/api/plugins/daedalus/meta/test-deliver", {
      method: "POST",
      body: { deliver: target },
    }).then(function (r) {
      setTestStatuses(function (prev) {
        return Object.assign({}, prev, { [i]: r && r.ok
          ? { ok: true, msg: "✓ Sent" }
          : { ok: false, msg: "✗ " + ((r && r.error) || "send failed") }
        });
      });
    }).catch(function (err) {
      setTestStatuses(function (prev) {
        return Object.assign({}, prev, { [i]: { ok: false, msg: "✗ " + String(err && err.message || err) } });
      });
    });
  }

  var eventOptions = NOTIFY_EVENTS.map(function (ev) { return { value: ev, label: ev }; });

  return React.createElement("div", { style: { marginBottom: "12px" } },
    targets.length === 0 ? React.createElement("div", { style: S.chipEmptyHint },
      "No multi-target notifications — the single \"Notify Via\" target above is used."
    ) : null,
    targets.map(function (entry, i) {
      var testStatus = testStatuses[String(i)] || null;
      var isTesting = testStatus && testStatus.ok === null;
      var hasTarget = !!entry.target;
      return React.createElement("div", {
        key: i,
        style: { border: "1px solid #2a2a2a", borderRadius: "8px", padding: "10px", marginBottom: "8px" },
      },
        React.createElement("div", { style: { display: "flex", gap: "8px", alignItems: "center", marginBottom: "4px" } },
          React.createElement(MethodChannelPicker, {
            methods: methods,
            method: entry.platform || "",
            target: entry.target || "",
            onMethod: function (m) { update(i, { platform: m, target: "" }); },
            onTarget: function (t) { update(i, { target: t }); },
          }),
          React.createElement("button", {
            style: Object.assign({}, S.btnSmall, { opacity: hasTarget && !isTesting ? 1 : 0.4 }),
            type: "button",
            disabled: !hasTarget || !!isTesting,
            onClick: function () { testRow(i, entry.target); },
          }, isTesting ? "Sending…" : "Test"),
          React.createElement("button", {
            style: S.chipRemove,
            title: "Remove notification target",
            type: "button",
            onClick: function () { remove(i); },
          }, "×")
        ),
        testStatus ? React.createElement("div", {
          style: {
            fontSize: "11px", marginBottom: "6px",
            color: testStatus.ok === true ? "#4ade80" : testStatus.ok === null ? "#888" : "#f87171",
          },
        }, testStatus.msg) : null,
        React.createElement("span", { style: S.fieldLabel }, "Events (empty = all)"),
        React.createElement(TagMultiSelect, {
          selected: entry.events || [],
          options: eventOptions,
          onChange: function (arr) { update(i, { events: arr }); },
          placeholder: "+ add event filter…",
          emptyHint: "no events",
        })
      );
    }),
    React.createElement("button", {
      style: S.btnSmall, type: "button", onClick: add,
    }, "+ Add notification target"),
    targets.length > 0 ? React.createElement("div", { style: { fontSize: "11px", color: "#666", marginTop: "6px" } },
      "Multi-target notifications override the single \"Notify Via\" target."
    ) : null
  );
}

// ── provider-specific VCS fields ────────────────────────────────────────────
// Extra inputs per provider. getVal(path[])→value, setVal(dottedPath, value).
function providerExtraFields(provider, getVal, setVal) {
  if (provider === "gitlab") {
    return React.createElement("div", { style: S.fieldRow },
      React.createElement("label", { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "GitLab Base URL (self-hosted)"),
        React.createElement("input", {
          style: S.input,
          value: getVal(["vcs", "base_url"], ""),
          placeholder: "https://gitlab.com",
          onChange: function (e) { setVal("vcs.base_url", e.target.value); },
        })
      ),
      React.createElement("label", { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Project Path (defaults to repo)"),
        React.createElement("input", {
          style: S.input,
          value: getVal(["vcs", "project_path"], ""),
          placeholder: "group/project",
          onChange: function (e) { setVal("vcs.project_path", e.target.value); },
        })
      )
    );
  }
  if (provider === "azuredevops") {
    return React.createElement("div", { style: S.fieldRow },
      React.createElement("label", { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Azure Organization"),
        React.createElement("input", {
          style: S.input,
          value: getVal(["vcs", "org"], ""),
          placeholder: "my-org",
          onChange: function (e) { setVal("vcs.org", e.target.value); },
        })
      ),
      React.createElement("label", { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Azure Project"),
        React.createElement("input", {
          style: S.input,
          value: getVal(["vcs", "project"], ""),
          placeholder: "MyProject",
          onChange: function (e) { setVal("vcs.project", e.target.value); },
        })
      ),
      React.createElement("label", { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Azure Repo"),
        React.createElement("input", {
          style: S.input,
          value: getVal(["vcs", "repo"], ""),
          placeholder: "my-repo",
          onChange: function (e) { setVal("vcs.repo", e.target.value); },
        })
      )
    );
  }
  return null;
}

// ── RemoveProjectModal ──────────────────────────────────────────────────────
function RemoveProjectModal(props) {
  var rm = useState(false); var removing = rm[0], setRemoving = rm[1];
  var er = useState(null); var error = er[0], setError = er[1];

  function doRemove() {
    setRemoving(true); setError(null);
    fetchJSON(apiProject(props.name), { method: "DELETE" })
      .then(function () { props.onRemoved(); })
      .catch(function (err) {
        setRemoving(false);
        setError(String(err && err.message || err));
      });
  }

  return React.createElement("div", { style: Object.assign({}, S.overlay, { zIndex: 1100 }) },
    React.createElement("div", { style: Object.assign({}, S.modal, { maxWidth: "400px" }) },
      React.createElement("div", { style: S.modalHeader },
        React.createElement("span", { style: S.modalTitle }, "Remove Project"),
        React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "×")
      ),
      React.createElement("p", { style: { fontSize: "14px", lineHeight: "1.5", color: "#ccc", margin: "0 0 8px" } },
        "Remove ", React.createElement("strong", null, props.name), " from the dashboard?"
      ),
      React.createElement("p", { style: { fontSize: "13px", color: "#888", margin: "0 0 4px" } },
        "This will:"
      ),
      React.createElement("ul", { style: { fontSize: "13px", color: "#aaa", margin: "0 0 12px", paddingLeft: "20px", lineHeight: "1.8" } },
        React.createElement("li", null, "Delete the cron job"),
        React.createElement("li", null, "Archive the kanban board"),
        React.createElement("li", null, "Remove from the project registry")
      ),
      React.createElement("p", { style: { fontSize: "13px", color: "#4ade80", margin: "0 0 16px" } },
        "✓ .hermes/daedalus.yaml is not touched — you can re-add this project at any time."
      ),
      error ? React.createElement("div", { style: Object.assign({}, S.err, { marginBottom: "12px" }) }, error) : null,
      React.createElement("div", { style: S.modalBar },
        React.createElement(Button, { label: removing ? "Removing…" : "Remove", variant: "danger", disabled: removing, onClick: doRemove }),
        React.createElement(Button, { label: "Cancel", disabled: removing, onClick: props.onClose })
      )
    )
  );
}

// ── config modal ────────────────────────────────────────────────────────────
// Human-friendly field label map: raw key → display title
var FIELD_LABELS = {
  repo: "Repository",
  workdir: "Working Directory",
  github_project_number: "Project Board",
  ready_statuses: "Statuses to Process",
  target_branch: "Target Branch",
  branch_prefix: "Branch Prefix",
  pr_title_prefix: "PR Title Prefix",
  schedule: "Cron Schedule",
  deliver: "Notify Via",
  channel: "Channel",
  labels: "Issue Labels",
  max_issues_per_run: "Max Issues per Run",
  max_open_prs: "Max Open PRs",
};

function ConfigModal(props) {
  var name = props.name;
  var s = useState(null); var config = s[0], setConfig = s[1];
  var l = useState(true); var loading = l[0], setLoading = l[1];
  var e = useState(null); var loadErr = e[0], setLoadErr = e[1];
  var sv = useState(false); var saving = sv[0], setSaving = sv[1];
  var r = useState(null); var result = r[0], setResult = r[1];
  var fe = useState(null); var fieldErrors = fe[0], setFieldErrors = fe[1];
  var ns = useState({}); var notifications = ns[0], setNotifications = ns[1];
  var sr = useState(false); var showRemoveModal = sr[0], setShowRemoveModal = sr[1];
  // Snapshot of the last-saved config, for the unsaved-changes indicator (#66).
  var pr = useState(null); var pristine = pr[0], setPristine = pr[1];

  // Meta data for data-driven fields (branches, labels, statuses, projects)
  var br = useState([]); var branches = br[0], setBranches = br[1];
  var la = useState([]); var labels = la[0], setLabels = la[1];
  var ll = useState(false); var labelsLoaded = ll[0], setLabelsLoaded = ll[1];
  var st = useState([]); var statuses = st[0], setStatuses = st[1];
  var gp = useState([]); var ghProjects = gp[0], setGhProjects = gp[1];

  var load = useCallback(function () {
    setLoading(true); setLoadErr(null); setFieldErrors(null);
    fetchJSON(apiProjectConfig(name)).then(function (data) {
      // Seed sensible default values for editable fields that are empty/missing
      if (!data.cron) data.cron = {};
      if (!data.cron.schedule) data.cron.schedule = "every 60m";
      // cron.deliver is intentionally NOT seeded — empty means no notification
      if (!data.vcs) data.vcs = {};
      if (!data.vcs.target_branch) data.vcs.target_branch = "main";
      if (!data.vcs.branch_prefix) data.vcs.branch_prefix = "fix";
      if (!data.vcs.pr_title_prefix) data.vcs.pr_title_prefix = "fix:";
      if (!data.issues) data.issues = {};
      if (!data.issues.processing) data.issues.processing = {};
      if (data.issues.processing.max_issues_per_run == null) data.issues.processing.max_issues_per_run = 20;
      if (data.issues.processing.max_open_prs == null) data.issues.processing.max_open_prs = 5;
      // tracking.github_project_number intentionally NOT seeded — empty means no board
      // issues.filters.labels intentionally NOT seeded — defaults handled by TagMultiSelect
      // tracking.ready_statuses intentionally NOT seeded — defaults to ["Ready"] via getIn
      setConfig(data);
      // Snapshot the loaded config so we can detect unsaved edits (#66).
      setPristine(JSON.parse(JSON.stringify(data)));
      setLoading(false);
    }).catch(function (err) {
      setLoadErr(String(err && err.message || err));
      setLoading(false);
    });
  }, [name]);
  useEffect(function () { load(); }, [load]);

  // Fetch notification methods from backend on mount.
  useEffect(function () {
    fetchJSON("/api/plugins/daedalus/meta/notifications").then(function (data) {
      setNotifications(data || {});
    }).catch(function () {
      setNotifications({});
    });
  }, []);

  // Fetch branches for target_branch dropdown.
  useEffect(function () {
    fetchJSON(apiMetaUrl(name, "branches")).then(function (data) {
      setBranches((data && data.branches) ? data.branches.sort() : []);
    }).catch(function () { setBranches([]); });
  }, [name]);

  // Fetch labels for issues filter checkboxes.
  useEffect(function () {
    setLabelsLoaded(false);
    fetchJSON(apiMetaUrl(name, "labels")).then(function (data) {
      setLabels((data && data.labels) ? data.labels : []);
      setLabelsLoaded(true);
    }).catch(function () { setLabels([]); setLabelsLoaded(true); });
  }, [name]);

  // Fetch GitHub Projects for board select.
  useEffect(function () {
    fetchJSON(apiMetaUrl(name, "projects")).then(function (data) {
      setGhProjects((data && data.projects) ? data.projects : []);
    }).catch(function () { setGhProjects([]); });
  }, [name]);

  function updateField(path, value) {
    setConfig(function (prev) {
      var parts = path.split(".");
      var next = JSON.parse(JSON.stringify(prev));
      var cur = next;
      for (var i = 0; i < parts.length - 1; i++) {
        if (!cur[parts[i]] || typeof cur[parts[i]] !== "object") cur[parts[i]] = {};
        cur = cur[parts[i]];
      }
      cur[parts[parts.length - 1]] = value;
      return next;
    });
  }

  function toggleSource(key) {
    setConfig(function (prev) {
      var next = JSON.parse(JSON.stringify(prev));
      var sources = next.sources || {};
      var entry = sources[key] || {};
      entry.enabled = !entry.enabled;
      sources[key] = entry;
      next.sources = sources;
      return next;
    });
  }

  function save() {
    setSaving(true); setResult(null); setFieldErrors(null);
    var body = {};
    if (config.name !== undefined) {
      body.name = config.name;
    }
    if (config.tracking) {
      body.tracking = config.tracking;
    }
    if (config.vcs) body.vcs = config.vcs;
    if (config.cron) {
      var cronBody = Object.assign({}, config.cron);
      delete cronBody.deliver; // removed in favour of multi-target notifications
      body.cron = cronBody;
    }
    if (config.sources) body.sources = config.sources;
    if (config.issues) body.issues = config.issues;
    if (config.execution) body.execution = config.execution;

    fetchJSON(apiProjectConfig(name), { method: "POST", body: body }).then(function (res) {
      setSaving(false);
      if (res && res.status === "saved") {
        // The current config is now the saved baseline — clear dirty state (#66).
        setPristine(JSON.parse(JSON.stringify(config)));
        // Surface the cron reconciliation result if present
        if (res.cron) {
          var cr = res.cron;
          var cronMsg = cr.name || "";
          if (cr.error) {
            cronMsg += " \u00b7 \u26a0\ufe0f " + cr.error;
          } else if (cr.cron && cr.cron !== "skipped") {
            cronMsg += " \u00b7 cron " + cr.cron;
          }
          setResult({ ok: true, msg: "Saved \u00b7 " + cronMsg });
        } else {
          setResult({ ok: true, msg: "Saved" });
        }
        // Close modal after a brief delay so the user can see the result
        setTimeout(function () { props.onClose(); }, 1200);
      } else {
        setResult({ ok: false, errors: ["Unexpected response"] });
      }
    }).catch(function (err) {
      setSaving(false);
      if (err && err.detail) {
        var msg = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
        // Parse 422 detail — could be a string or object with errors array
        try {
          var parsed = typeof err.detail === "string" ? JSON.parse(err.detail) : err.detail;
          if (parsed && parsed.errors) {
            setFieldErrors(parsed.errors);
          } else if (typeof parsed === "string") {
            setFieldErrors([parsed]);
          }
        } catch (_) {
          setFieldErrors([msg]);
        }
      } else {
        setResult({ ok: false, errors: [String(err && err.message || err)] });
      }
    });
  }

  if (loading) return React.createElement("div", { style: S.overlay, onClick: props.onClose },
    React.createElement("div", { style: S.modal, onClick: function (e) { e.stopPropagation(); } },
      React.createElement("div", { style: { textAlign: "center", padding: "40px", color: "#888" } }, "Loading config…")
    )
  );

  if (loadErr) return React.createElement("div", { style: S.overlay, onClick: props.onClose },
    React.createElement("div", { style: S.modal, onClick: function (e) { e.stopPropagation(); } },
      React.createElement("div", { style: S.modalHeader },
        React.createElement("span", { style: S.modalTitle }, name),
        React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "×")
      ),
      React.createElement("div", { style: S.err }, "Failed to load config: ", loadErr),
      React.createElement("button", { style: S.btn, onClick: load }, "Retry")
    )
  );

  var sources = config && config.sources ? Object.keys(config.sources).filter(function (k) { return k !== "secret"; }) : [];
  var dirty = configDirty.isDirty(pristine, config);

  return React.createElement(React.Fragment, null,
  React.createElement("div", { style: S.overlay, onClick: props.onClose },
    React.createElement("div", { style: S.modal, onClick: function (e) { e.stopPropagation(); } },
      // Header
      React.createElement("div", { style: S.modalHeader },
        React.createElement("span", { style: S.modalTitle },
          props.setupMode ? "Configure: " + name + " · Step 2 of 2" : "Edit: " + name
        ),
        React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "×")
      ),

      // Read-only identity (full-width, stacked, label + bare value)
      React.createElement("div", { style: { marginBottom: "12px" } },
        React.createElement("div", { style: S.fieldLabel }, FIELD_LABELS.repo),
        React.createElement("span", { style: Object.assign({}, S.readOnlyText, { display: "block", width: "100%" }) }, config.repo || "\u2014")
      ),
      React.createElement("div", { style: { marginBottom: "12px" } },
        React.createElement("div", { style: S.fieldLabel }, FIELD_LABELS.workdir),
        React.createElement("span", { style: Object.assign({}, S.readOnlyText, { display: "block", width: "100%" }) }, config.workdir || "\u2014")
      ),

      // ── VCS (provider-aware) — drives target_branch, board, and labels ───────────
      React.createElement("div", { style: S.section }, "VCS"),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Provider"),
          React.createElement("select", {
            style: S.select,
            value: getIn(config, ["vcs", "provider"], "github"),
            onChange: function (e) { updateField("vcs.provider", e.target.value); }
          },
            PROVIDERS.map(function (p) {
              return React.createElement("option", { key: p, value: p }, PROVIDER_LABELS[p] || p);
            })
          ),
          React.createElement("span", { style: { fontSize: "11px", color: "#666", marginTop: "2px" } },
            repoLabelForProvider(getIn(config, ["vcs", "provider"], "github"))
          )
        )
      ),
      providerExtraFields(
        getIn(config, ["vcs", "provider"], "github"),
        function (path, fb) {
          var val = getIn(config, path, fb);
          // Fall back to top-level repo for GitLab project_path when not explicitly set.
          if (!val && path[0] === "vcs" && path[1] === "project_path") {
            return getIn(config, ["repo"], fb || "");
          }
          return val;
        },
        updateField
      ),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.target_branch),
          React.createElement("select", {
            style: S.select,
            value: getIn(config, ["vcs", "target_branch"], ""),
            onChange: function (e) { updateField("vcs.target_branch", e.target.value); }
          },
            React.createElement("option", { value: "" }, branches.length === 0 ? "— loading branches… —" : "— none —"),
            (function () {
              var saved = getIn(config, ["vcs", "target_branch"], "");
              var opts = branches.map(function (b) {
                return React.createElement("option", { key: b, value: b }, b);
              });
              if (saved && branches.indexOf(saved) === -1) {
                opts.unshift(React.createElement("option", { key: saved, value: saved }, saved));
              }
              return opts;
            })()
          ),
          branches.length === 0 ? React.createElement("span", { style: { fontSize: "11px", color: "#888", marginTop: "2px" } },
            "Requires 'repo' scope on your GITHUB_TOKEN to load branches."
          ) : null
        ),
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.branch_prefix),
          React.createElement("input", {
            style: S.input,
            value: getIn(config, ["vcs", "branch_prefix"], ""),
            placeholder: "fix",
            onChange: function (e) { updateField("vcs.branch_prefix", e.target.value); }
          })
        ),
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.pr_title_prefix),
          React.createElement("input", {
            style: S.input,
            value: getIn(config, ["vcs", "pr_title_prefix"], ""),
            placeholder: "fix:",
            onChange: function (e) { updateField("vcs.pr_title_prefix", e.target.value); }
          })
        )
      ),

      // ── GitHub Project Board (GitHub only) ───────────────────────────
      (getIn(config, ["vcs", "provider"], "github") || "github").toLowerCase() === "github"
        ? (function () {
            var boardNum = getIn(config, ["tracking", "github_project_number"], null);
            var hasStatuses = statuses && statuses.length > 0;
            return [
              React.createElement("div", { key: "tracking-hdr", style: S.section }, "GitHub Project Board"),
              React.createElement("div", { key: "track-board", style: S.fieldRow },
                React.createElement("label", { style: S.field },
                  React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.github_project_number),
                  React.createElement("select", {
                    style: S.select,
                    value: boardNum != null ? String(boardNum) : "",
                    onChange: function (e) {
                      var v = e.target.value.trim();
                      if (v === "") {
                        updateField("tracking.github_project_number", undefined);
                        setStatuses([]);
                      } else {
                        var n = parseInt(v, 10);
                        if (!isNaN(n)) {
                          updateField("tracking.github_project_number", n);
                          fetchJSON(apiMetaUrl(name, "statuses") + "&github_project_number=" + n).then(function (data) {
                            setStatuses((data && data.statuses) ? data.statuses : []);
                          }).catch(function () { setStatuses([]); });
                        }
                      }
                    }
                  },
                    React.createElement("option", { value: "" }, ghProjects.length === 0 ? "— no boards found —" : "— none —"),
                    ghProjects.map(function (p) {
                      return React.createElement("option", { key: p.number, value: String(p.number) }, "#" + p.number + " " + (p.title || ""));
                    })
                  ),
                  ghProjects.length === 0 ? React.createElement("span", { style: { fontSize: "11px", color: "#888", display: "block", marginTop: "2px" } },
                    "Requires 'project' scope on your GITHUB_TOKEN. Add it and reload."
                  ) : null
                )
              ),
              boardNum && hasStatuses ? React.createElement("div", { key: "track-statuses", style: { marginBottom: "12px" } },
                React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.ready_statuses),
                React.createElement(TagMultiSelect, {
                  selected: getIn(config, ["tracking", "ready_statuses"], ["Ready"]),
                  options: statuses.map(function (s) { return { value: s, label: s }; }),
                  onChange: function (arr) { updateField("tracking.ready_statuses", arr); },
                  placeholder: "+ add status\u2026",
                  emptyHint: "no statuses found"
                })
              ) : null
            ];
          })()
        : null,

      // ── Sources ───────────────────────────────────────────────────────────────
      sources.length > 0 ? React.createElement("div", { style: S.section }, "Sources") : null,
      sources.length > 0 ? React.createElement("div", { style: { marginBottom: "12px" } },
        sources.map(function (key) {
          var enabled = !!(config.sources[key] && config.sources[key].enabled);
          var labelMap = { github_issues: "VCS Issues (GitHub/GitLab/Azure)", local_specs: "Local Specs", kanban_triage: "Kanban Triage" };
          var humanLabel = labelMap[key] || key;
          var statusSuffix = enabled ? " (enabled)" : " (disabled)";
          return React.createElement(Checkbox, { key: key, label: humanLabel + statusSuffix, checked: enabled, onChange: function () { toggleSource(key); } });
        })
      ) : null,

      // ── Issue Labels ──────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Issue Labels"),
      React.createElement("div", { style: { marginBottom: "12px" } },
        React.createElement(TagMultiSelect, {
          selected: getIn(config, ["issues", "filters", "labels"], []),
          options: (labels || []).map(function (l) { return { value: l.name, label: l.name, color: l.color }; }),
          onChange: function (arr) { updateField("issues.filters.labels", arr); },
          placeholder: !labelsLoaded ? "— loading labels… —" : (labels.length === 0 ? "— no labels found —" : "└ select a label to filter"),
          emptyHint: "No labels found — check that your VCS token has the correct scopes and the project path is set"
        })
      ),

      // ── Throughput ────────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Throughput"),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.max_issues_per_run),
          React.createElement("input", {
            style: S.input,
            type: "number",
            value: getIn(config, ["issues", "processing", "max_issues_per_run"], ""),
            placeholder: "20",
            onChange: function (e) {
              var v = e.target.value.trim();
              if (v === "") {
                updateField("issues.processing.max_issues_per_run", undefined);
              } else {
                var n = parseInt(v, 10);
                if (!isNaN(n)) updateField("issues.processing.max_issues_per_run", n);
              }
            }
          })
        ),
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.max_open_prs),
          React.createElement("input", {
            style: S.input,
            type: "number",
            value: getIn(config, ["issues", "processing", "max_open_prs"], ""),
            placeholder: "5",
            onChange: function (e) {
              var v = e.target.value.trim();
              if (v === "") {
                setConfig(function (prev) {
                  var next = JSON.parse(JSON.stringify(prev));
                  var proc = (next.issues || {}).processing;
                  if (proc) delete proc.max_open_prs;
                  return next;
                });
              } else {
                var n = parseInt(v, 10);
                if (!isNaN(n)) updateField("issues.processing.max_open_prs", n);
              }
            }
          })
        )
      ),

      // ── Cron ──────────────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Cron"),
      React.createElement(CronSchedule, {
        value: getIn(config, ["cron", "schedule"], ""),
        onChange: function (v) { updateField("cron.schedule", v); }
      }),

      // ── Notifications ─────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Notifications"),
      React.createElement(NotificationsEditor, {
        targets: getIn(config, ["cron", "notifications"], []),
        methods: notifications,
        onChange: function (arr) { updateField("cron.notifications", arr); }
      }),

      // ── Auto-Merge ────────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Auto-Merge"),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: Object.assign({}, S.field, { flexDirection: "row", alignItems: "center", gap: "10px", cursor: "pointer" }) },
          React.createElement("input", {
            type: "checkbox",
            checked: !!getIn(config, ["execution", "auto_merge"], false),
            onChange: function (e) { updateField("execution.auto_merge", e.target.checked); },
            style: { width: "16px", height: "16px", cursor: "pointer", accentColor: "#3b82f6", flexShrink: 0 }
          }),
          React.createElement("div", null,
            React.createElement("div", { style: { fontSize: "13px", color: "#e2e8f0", fontWeight: 500 } }, "Automatically merge PR after all reviews pass"),
            React.createElement("div", { style: { fontSize: "11px", color: "#888", marginTop: "2px" } },
              "When enabled, the dispatcher merges the PR via the GitHub API after the documentation agent completes. When disabled (default), a human merges the PR."
            )
          )
        )
      ),
      getIn(config, ["execution", "auto_merge"], false)
        ? React.createElement("div", { style: S.fieldRow },
            React.createElement("label", { style: S.field },
              React.createElement("span", { style: S.fieldLabel }, "Merge Method"),
              React.createElement("select", {
                style: S.select,
                value: getIn(config, ["execution", "merge_method"], "squash"),
                onChange: function (e) { updateField("execution.merge_method", e.target.value); }
              },
                React.createElement("option", { value: "squash" }, "Squash and merge"),
                React.createElement("option", { value: "merge" }, "Create a merge commit"),
                React.createElement("option", { value: "rebase" }, "Rebase and merge")
              )
            )
          )
        : null,

      // ── Coding Agent ──────────────────────────────────────────────────────────
      React.createElement("div", { style: S.section }, "Coding Agent"),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: Object.assign({}, S.field, { flex: "1 1 100%" }) },
          React.createElement("span", { style: S.fieldLabel }, "Agent"),
          React.createElement("select", {
            style: S.select,
            value: getIn(config, ["execution", "coding_agent"], "hermes"),
            onChange: function (e) {
              var prevAgent = getIn(config, ["execution", "coding_agent"], "hermes");
              var nextAgent = e.target.value;
              updateField("execution.coding_agent", nextAgent);
              // Auto-fill the CLI command with the new agent's default so the
              // user doesn't have to look it up (Hermes/no-default clears it).
              // null means the agent didn't change — leave a typed command alone.
              var nextCmd = codingAgent.cmdForAgentChange(prevAgent, nextAgent);
              if (nextCmd !== null) {
                updateField("execution.coding_agent_cmd", nextCmd);
              }
            }
          },
            React.createElement("option", { value: "hermes" }, "Hermes — delegate via built-in subagent"),
            React.createElement("option", { value: "claude-code" }, "Claude Code"),
            React.createElement("option", { value: "codex" }, "Codex"),
            React.createElement("option", { value: "opencode" }, "OpenCode")
          ),
          React.createElement("div", { style: { fontSize: "11px", color: "#888", marginTop: "2px" } },
            "When set, the developer agent uses delegate_task to hand off coding work to a CLI agent subagent."
          )
        )
      ),
      codingAgent.isCliAgent(getIn(config, ["execution", "coding_agent"], "hermes"))
        ? (function() {
            var _currentAgent = getIn(config, ["execution", "coding_agent"], "hermes");
            var _defaultCmd = codingAgent.defaultCmdFor(_currentAgent);
            return React.createElement("div", { style: S.fieldRow },
              React.createElement("label", { style: Object.assign({}, S.field, { flex: "1 1 100%" }) },
                React.createElement("span", { style: S.fieldLabel }, "CLI Command"),
                React.createElement("input", {
                  style: S.input,
                  value: getIn(config, ["execution", "coding_agent_cmd"], ""),
                  placeholder: _defaultCmd ? ("default: " + _defaultCmd + " — override e.g. cc-rizq") : "e.g. cc, cc-rizq, cc-rewst",
                  onChange: function (e) { updateField("execution.coding_agent_cmd", e.target.value); }
                }),
                React.createElement("div", { style: { fontSize: "11px", color: "#888", marginTop: "2px" } },
                  "Custom shell command passed as acp_command to delegate_task. Leave blank to use the default above."
                )
              )
            );
          })()
        : null,

      // Errors
      fieldErrors && fieldErrors.length > 0 ? React.createElement("div", { style: { marginBottom: "8px" } },
        fieldErrors.map(function (errMsg, i) {
          return React.createElement("div", { key: i, style: { color: "#f87171", fontSize: "12px", margin: "2px 0", padding: "4px 8px", background: "rgba(248,113,113,0.08)", borderRadius: "4px" } }, errMsg);
        })
      ) : null,
      result && !result.ok ? React.createElement("div", { style: { marginBottom: "8px" } },
        (result.errors || []).map(function (errMsg, i) {
          return React.createElement("div", { key: i, style: S.err }, errMsg);
        })
      ) : null,
      result && result.ok ? React.createElement("div", { style: S.ok }, result.msg) : null,

      // Actions
      React.createElement("div", { style: S.modalBar },
        React.createElement(Button, {
          label: saving ? "Saving…" : (props.setupMode ? "Finish Setup" : "Save"),
          variant: "primary", disabled: saving, onClick: save
        }),
        React.createElement(Button, {
          label: props.setupMode ? "← Start Over" : "Cancel",
          disabled: saving,
          onClick: props.setupMode ? props.onAbort : props.onClose
        }),
        dirty && !saving ? React.createElement("span", {
          style: { color: "#f5a623", fontSize: "12px", alignSelf: "center", marginLeft: "4px" },
          title: "You have unsaved changes — click Save before closing or opening this project elsewhere."
        }, "● Unsaved changes") : null,
        props.setupMode ? null : React.createElement("div", { style: { marginLeft: "auto" } },
          React.createElement(Button, { label: "Remove Project", variant: "danger", onClick: function () { setShowRemoveModal(true); } })
        )
      )
    )
  ),
  showRemoveModal ? React.createElement(RemoveProjectModal, {
    name: name,
    onClose: function () { setShowRemoveModal(false); },
    onRemoved: props.onRemoved,
  }) : null
  );
}

// ── DeliverMultiPicker ──────────────────────────────────────────────────────
// Multiple platform → channel rows, no event filters.
// props: targets [{platform, target}], methods (meta/notifications map), onChange(arr)
function DeliverMultiPicker(props) {
  var targets = props.targets || [];
  var methods = props.methods || {};
  var methodNames = Object.keys(methods).sort();
  // Per-row test status keyed by row index: {ok: bool|null, msg: string}
  var ts = useState({}); var testStatuses = ts[0], setTestStatuses = ts[1];

  function updateRow(i, patch) {
    var next = targets.map(function (t, j) { return j === i ? Object.assign({}, t, patch) : t; });
    setTestStatuses(function (prev) { var n = Object.assign({}, prev); delete n[i]; return n; });
    props.onChange(next);
  }
  function removeRow(i) {
    setTestStatuses(function (prev) {
      var n = {};
      Object.keys(prev).forEach(function (k) {
        var ki = parseInt(k, 10);
        if (ki < i) n[k] = prev[k];
        else if (ki > i) n[String(ki - 1)] = prev[k];
      });
      return n;
    });
    props.onChange(targets.filter(function (_, j) { return j !== i; }));
  }
  function addRow() { props.onChange(targets.concat([{ platform: "", target: "" }])); }

  function testRow(i, target) {
    setTestStatuses(function (prev) { return Object.assign({}, prev, { [i]: { ok: null, msg: "Sending…" } }); });
    fetchJSON("/api/plugins/daedalus/meta/test-deliver", {
      method: "POST",
      body: { deliver: target },
    }).then(function (r) {
      setTestStatuses(function (prev) {
        return Object.assign({}, prev, { [i]: r && r.ok
          ? { ok: true, msg: "✓ Sent" }
          : { ok: false, msg: "✗ " + ((r && r.error) || "send failed") }
        });
      });
    }).catch(function (err) {
      setTestStatuses(function (prev) {
        return Object.assign({}, prev, { [i]: { ok: false, msg: "✗ " + String(err && err.message || err) } });
      });
    });
  }

  return React.createElement("div", null,
    targets.map(function (t, i) {
      var rawChannelOpts = t.platform && methods[t.platform] ? methods[t.platform] : [];
      var channelOpts = rawChannelOpts.map(function (e) {
        return typeof e === "string" ? { value: e, label: e } : e;
      });
      var testStatus = testStatuses[String(i)] || null;
      var isTesting = testStatus && testStatus.ok === null;
      var hasTarget = !!t.target;
      return React.createElement("div", { key: i, style: { marginBottom: "8px" } },
        React.createElement("div", { style: { display: "flex", gap: "6px", alignItems: "center" } },
          React.createElement("select", {
            style: Object.assign({}, S.select, { flex: "0 0 130px" }),
            value: t.platform || "",
            onChange: function (e) { updateRow(i, { platform: e.target.value, target: "" }); },
          },
            React.createElement("option", { value: "" }, "— service —"),
            methodNames.map(function (m) {
              return React.createElement("option", { key: m, value: m }, m);
            })
          ),
          t.platform ? (
            channelOpts.length > 0
              ? React.createElement("select", {
                  style: Object.assign({}, S.select, { flex: "1 1 auto" }),
                  value: t.target || "",
                  onChange: function (e) { updateRow(i, { target: e.target.value }); },
                },
                  React.createElement("option", { value: "" }, "— channel —"),
                  channelOpts.map(function (ch) {
                    return React.createElement("option", { key: ch.value, value: ch.value }, ch.label);
                  })
                )
              : React.createElement("input", {
                  style: Object.assign({}, S.input, { flex: "1 1 auto" }),
                  value: t.target || "",
                  placeholder: t.platform + ":channel-id",
                  onChange: function (e) { updateRow(i, { target: e.target.value }); },
                })
          ) : React.createElement("div", { style: { flex: "1 1 auto" } }),
          React.createElement("button", {
            style: Object.assign({}, S.btnSmall, { opacity: hasTarget && !isTesting ? 1 : 0.4 }),
            type: "button",
            disabled: !hasTarget || !!isTesting,
            onClick: function () { testRow(i, t.target); },
          }, isTesting ? "Sending…" : "Test"),
          React.createElement("button", {
            style: S.btnSmall, type: "button",
            onClick: function () { removeRow(i); },
          }, "×")
        ),
        testStatus ? React.createElement("div", {
          style: {
            fontSize: "11px", marginTop: "3px", marginLeft: "2px",
            color: testStatus.ok === true ? "#4ade80" : testStatus.ok === null ? "#888" : "#f87171",
          },
        }, testStatus.msg) : null
      );
    }),
    methodNames.length > 0
      ? React.createElement("button", {
          style: S.btnSmall, type: "button", onClick: addRow,
        }, "+ Add notification service")
      : React.createElement("span", { style: { fontSize: "12px", color: "#666" } },
          "No notification services configured in Hermes yet."
        )
  );
}

// ── add-project modal (Step 1 of 2) ────────────────────────────────────────
// Registers the project with minimal info, then hands off to ConfigModal
// (step 2) where all live dropdowns (branches, boards, labels) are available.
function AddProjectModal(props) {
  var nm = useState(""); var name = nm[0], setName = nm[1];
  var rp = useState(""); var repo = rp[0], setRepo = rp[1];
  var wd = useState(""); var workdir = wd[0], setWorkdir = wd[1];
  var pv = useState(""); var provider = pv[0], setProvider = pv[1];
  var ex = useState({}); var extra = ex[0], setExtra = ex[1];
  var so = useState({ github_issues: false, local_specs: true, kanban_triage: true });
  var srcToggles = so[0], setSrcToggles = so[1];
  var sv = useState(false); var saving = sv[0], setSaving = sv[1];
  var er = useState(null); var errors = er[0], setErrors = er[1];

  function applyDetected(d) {
    if (!d || !d.detected) return;
    if (d.name && !name) setName(d.name);
    if (d.repo && !repo) setRepo(d.repo);
    if (d.provider) {
      setProvider(d.provider);
      setSrcToggles(function (prev) { return Object.assign({}, prev, { github_issues: true }); });
    }
    if (d.vcs_extra && Object.keys(d.vcs_extra).length > 0) {
      setExtra(function (prev) { return Object.assign({}, prev, d.vcs_extra); });
    }
  }

  useEffect(function () {
    var trimmed = workdir.trim();
    if (!trimmed) return;
    var cancelled = false;
    var timer = setTimeout(function () {
      fetchJSON("/api/plugins/daedalus/meta/detect?workdir=" + encodeURIComponent(trimmed))
        .then(function (d) { if (!cancelled) applyDetected(d); })
        .catch(function () {});
    }, 600);
    return function () { cancelled = true; clearTimeout(timer); };
  }, [workdir]);

  function setExtraField(dotted, value) {
    var key = dotted.split(".").pop();
    setExtra(function (prev) { var next = Object.assign({}, prev); next[key] = value; return next; });
  }

  function register() {
    setSaving(true); setErrors(null);
    var vcs = Object.assign({}, extra);
    if (provider) vcs.provider = provider;
    var body = {
      name: name.trim(),
      repo: repo.trim(),
      workdir: workdir.trim(),
      vcs: vcs,
      sources: {
        github_issues: { enabled: !!srcToggles.github_issues },
        local_specs: { enabled: !!srcToggles.local_specs },
        kanban_triage: { enabled: !!srcToggles.kanban_triage },
      },
    };
    fetchJSON(API_PROJECT_CREATE, { method: "POST", body: body }).then(function (res) {
      setSaving(false);
      if (res && (res.status === "created" || res.status === "adopted")) {
        props.onRegistered(name.trim());
        return;
      }
      var detail = res && res.detail;
      if (detail && detail.errors) setErrors(detail.errors);
      else if (typeof detail === "string") setErrors([detail]);
      else setErrors(["Unexpected response: " + JSON.stringify(res).slice(0, 200)]);
    }).catch(function (err) {
      setSaving(false);
      setErrors([String(err && err.message || err)]);
    });
  }

  var canSubmit = name.trim() && workdir.trim() && !saving;

  return React.createElement("div", { style: S.overlay, onClick: props.onClose },
    React.createElement("div", { style: S.modal, onClick: function (e) { e.stopPropagation(); } },
      React.createElement("div", { style: S.modalHeader },
        React.createElement("span", { style: S.modalTitle }, "Add Project · Step 1 of 2"),
        React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "×")
      ),
      React.createElement("p", { style: { fontSize: "13px", color: "#888", margin: "0 0 16px" } },
        "Enter the basics — the next step lets you configure branches, boards, cron, and notifications with live dropdowns."
      ),

      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Working Directory (absolute path)"),
          React.createElement("div", { style: { display: "flex", gap: "6px" } },
            React.createElement("input", {
              style: Object.assign({}, S.input, { flex: "1 1 auto" }),
              value: workdir, placeholder: "/path/to/repo",
              onChange: function (e) { setWorkdir(e.target.value); },
            }),
            React.createElement("button", {
              style: S.btnSmall, type: "button",
              onClick: function () {
                fetchJSON("/api/plugins/daedalus/meta/pick-directory")
                  .then(function (d) {
                    if (!d || !d.path) return;
                    setWorkdir(d.path);
                    fetchJSON("/api/plugins/daedalus/meta/detect?workdir=" + encodeURIComponent(d.path))
                      .then(function (det) { applyDetected(det); })
                      .catch(function () {});
                  }).catch(function () {});
              },
            }, "Browse…")
          )
        )
      ),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Project Name"),
          React.createElement("input", {
            style: S.input, value: name, placeholder: "my-project",
            onChange: function (e) { setName(e.target.value); },
          })
        ),
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Provider"),
          React.createElement("select", {
            style: S.select, value: provider,
            onChange: function (e) { setProvider(e.target.value); setExtra({}); },
          },
            React.createElement("option", { value: "" }, "Auto-detect from git remote"),
            PROVIDERS.map(function (p) {
              return React.createElement("option", { key: p, value: p }, PROVIDER_LABELS[p] || p);
            })
          )
        )
      ),
      React.createElement("div", { style: S.fieldRow },
        React.createElement("label", { style: S.field },
          React.createElement("span", { style: S.fieldLabel },
            provider ? repoLabelForProvider(provider) : "Repository (auto-detected from origin remote)"),
          React.createElement("input", {
            style: S.input, value: repo,
            placeholder: provider ? repoPlaceholderForProvider(provider) : "leave empty to auto-detect",
            onChange: function (e) { setRepo(e.target.value); },
          })
        )
      ),
      providerExtraFields(provider,
        function (path) { return extra[path[path.length - 1]] || ""; },
        setExtraField),

      React.createElement("div", { style: S.section }, "Sources"),
      [["github_issues", "VCS Issues (GitHub/GitLab/Azure)"],
       ["local_specs", "Local Specs (.hermes/pending/*.md)"],
       ["kanban_triage", "Kanban Triage (manual cards)"]].map(function (pair) {
        var key = pair[0], label = pair[1];
        return React.createElement(Checkbox, {
          key: key, label: label, checked: !!srcToggles[key],
          onChange: function () {
            setSrcToggles(function (prev) {
              var next = Object.assign({}, prev); next[key] = !next[key]; return next;
            });
          },
        });
      }),

      errors && errors.length > 0 ? React.createElement("div", { style: { margin: "10px 0" } },
        errors.map(function (msg, i) { return React.createElement("div", { key: i, style: S.err }, String(msg)); })
      ) : null,

      React.createElement("div", { style: S.modalBar },
        React.createElement(Button, {
          label: saving ? "Registering…" : "Next: Configure →",
          variant: "primary", disabled: !canSubmit, onClick: register,
        }),
        React.createElement(Button, { label: "Cancel", onClick: props.onClose })
      )
    )
  );
}

// ── UninstallModal ───────────────────────────────────────────────────────────
function UninstallModal(props) {
  var busy = useState(false); var running = busy[0], setRunning = busy[1];
  var res = useState(null); var result = res[0], setResult = res[1];

  function doUninstall() {
    setRunning(true);
    fetchJSON(API_UNINSTALL, { method: "POST" })
      .then(function (d) { setResult(d); setRunning(false); })
      .catch(function (err) { setResult({ ok: false, removed: [], skipped: [], error: String(err) }); setRunning(false); });
  }

  if (result) {
    return React.createElement("div", { style: S.overlay },
      React.createElement("div", { style: Object.assign({}, S.modal, { maxWidth: 480 }) },
        React.createElement("h2", { style: { marginTop: 0, color: result.ok ? "var(--color-success, #4ade80)" : "var(--color-danger, #f87171)" } },
          result.ok ? "✓ Uninstall complete" : "✗ Uninstall failed"),
        result.error ? React.createElement("p", { style: { color: "var(--color-danger, #f87171)", fontSize: 13 } }, result.error) : null,
        result.removed && result.removed.length > 0 ? React.createElement("div", null,
          React.createElement("p", { style: { fontWeight: 600, margin: "8px 0 4px" } }, "Removed:"),
          React.createElement("ul", { style: { margin: 0, paddingLeft: 20, fontSize: 13 } },
            result.removed.map(function (item, i) { return React.createElement("li", { key: i }, item); }))) : null,
        result.skipped && result.skipped.length > 0 ? React.createElement("div", null,
          React.createElement("p", { style: { fontWeight: 600, margin: "8px 0 4px" } }, "Skipped:"),
          React.createElement("ul", { style: { margin: 0, paddingLeft: 20, fontSize: 13, opacity: 0.7 } },
            result.skipped.map(function (item, i) { return React.createElement("li", { key: i }, item); }))) : null,
        React.createElement("p", { style: { fontSize: 13, marginTop: 12, opacity: 0.8 } },
          "Restart the Hermes gateway to complete removal of the dashboard tab."),
        React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 16 } },
          React.createElement("button", { onClick: props.onClose, style: S.btn }, "Close"))));
  }

  return React.createElement("div", { style: S.overlay },
    React.createElement("div", { style: Object.assign({}, S.modal, { maxWidth: 420 }) },
      React.createElement("h2", { style: { marginTop: 0 } }, "Uninstall Daedalus"),
      React.createElement("p", { style: { fontSize: 14, lineHeight: 1.5 } },
        "This will permanently remove:"),
      React.createElement("ul", { style: { fontSize: 13, lineHeight: 1.8, paddingLeft: 20 } },
        React.createElement("li", null, "All Daedalus cron jobs"),
        React.createElement("li", null, "All 6 specialist agent profiles"),
        React.createElement("li", null, "All non-default kanban boards"),
        React.createElement("li", null, "The registry and plugin package")),
      React.createElement("p", { style: { fontSize: 13, color: "var(--color-danger, #f87171)", fontWeight: 600 } },
        "⚠️ This cannot be undone."),
      React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 20 } },
        React.createElement("button", {
          onClick: doUninstall,
          disabled: running,
          style: Object.assign({}, S.btnDanger, running ? { opacity: 0.6, cursor: "not-allowed" } : {})
        }, running ? "Uninstalling…" : "Uninstall"),
        React.createElement("button", { onClick: props.onClose, disabled: running, style: S.btn }, "Cancel"))));
}

// ── main App ────────────────────────────────────────────────────────────────
function App() {
  var s = useState(null); var data = s[0], setData = s[1];
  var l = useState(true); var loading = l[0], setLoading = l[1];
  var e = useState(null); var loadErr = e[0], setLoadErr = e[1];
  var m = useState(null); var modalProject = m[0], setModalProject = m[1];
  var ap = useState(false); var showAddProject = ap[0], setShowAddProject = ap[1];
  var sp = useState(null); var setupProject = sp[0], setSetupProject = sp[1];
  var rs = useState(null); var rosterStatus = rs[0], setRosterStatus = rs[1];
  var rp = useState(false); var provisioningRoster = rp[0], setProvisioningRoster = rp[1];
  var rr = useState(null); var rosterResult = rr[0], setRosterResult = rr[1];
  var ui = useState(false); var showUninstall = ui[0], setShowUninstall = ui[1];
  var vr = useState(null); var pluginVersion = vr[0], setPluginVersion = vr[1];
  var hu = useState(null); var hasUpdate = hu[0], setHasUpdate = hu[1];
  var lv = useState(null); var latestVersion = lv[0], setLatestVersion = lv[1];
  var up = useState(false); var updating = up[0], setUpdating = up[1];
  var ur = useState(null); var updateResult = ur[0], setUpdateResult = ur[1];
  var rg = useState(false); var restarting = rg[0], setRestarting = rg[1];
  var rgr = useState(null); var restartResult = rgr[0], setRestartResult = rgr[1];

  var load = useCallback(function () {
    setLoading(true); setLoadErr(null);
    fetchJSON(API_PROJECTS).then(function (projects) {
      setData(projects);
      setLoading(false);
    }).catch(function (err) { setLoadErr(String(err && err.message || err)); setLoading(false); });
  }, []);
  useEffect(function () { load(); }, [load]);

  // Check whether the seven specialist profiles are provisioned.
  useEffect(function () {
    fetchJSON("/api/plugins/daedalus/meta/roster-status").then(function (d) {
      setRosterStatus(d || null);
    }).catch(function () { setRosterStatus(null); });
  }, []);

  // Fetch plugin version + check for updates once on mount.
  useEffect(function () {
    fetchJSON("/api/plugins/daedalus/meta/version").then(function (d) {
      setPluginVersion((d && d.version) || null);
    }).catch(function () {});
    fetchJSON("/api/plugins/daedalus/meta/check-update").then(function (d) {
      setHasUpdate(d && d.has_update === true);
      setLatestVersion((d && d.latest) || null);
    }).catch(function () { setHasUpdate(false); });
  }, []);

  function updatePlugin() {
    setUpdating(true); setUpdateResult(null);
    fetchJSON("/api/plugins/daedalus/meta/update-plugin", { method: "POST" })
      .then(function (r) {
        setUpdating(false);
        var result = r || { ok: false, output: "no response" };
        setUpdateResult(result);
        if (result.ok) {
          setHasUpdate(false);
          // Re-fetch roster status — provisioner ran as part of the update,
          // so the banner reflects the new profile set accurately.
          fetchJSON("/api/plugins/daedalus/meta/roster-status").then(function (d) {
            setRosterStatus(d || null);
          }).catch(function () {});
        }
      })
      .catch(function (err) {
        setUpdating(false);
        setUpdateResult({ ok: false, output: String(err && err.message || err) });
      });
  }

  function restartHermes() {
    setRestarting(true); setRestartResult(null);
    fetchJSON("/api/plugins/daedalus/meta/restart")
      .then(function () {
        setRestarting(false);
        setRestartResult({ ok: true });
      })
      .catch(function () {
        // Gateway killed itself — treat as success and prompt user to reload.
        setRestarting(false);
        setRestartResult({ ok: true });
      });
  }

  function provisionRoster() {
    setProvisioningRoster(true); setRosterResult(null);
    fetchJSON("/api/plugins/daedalus/meta/provision-roster", { method: "POST" })
      .then(function (r) {
        setProvisioningRoster(false);
        setRosterResult(r || { ok: false, error: "no response" });
        if (r && r.ok) {
          fetchJSON("/api/plugins/daedalus/meta/roster-status").then(function (d) {
            setRosterStatus(d || null);
          }).catch(function () {});
        }
      })
      .catch(function (err) {
        setProvisioningRoster(false);
        setRosterResult({ ok: false, error: String(err && err.message || err) });
      });
  }

  if (loading) return React.createElement("div", { style: S.wrap },
    React.createElement("div", { style: { textAlign: "center", padding: "60px", color: "#888" } }, "Loading projects…")
  );
  if (loadErr) {
    var isNotLoaded = loadErr.indexOf("No such API endpoint") !== -1 || loadErr.indexOf("404") !== -1;
    return React.createElement("div", { style: S.wrap },
      React.createElement("div", { style: S.err },
        isNotLoaded
          ? "Plugin not active — restart the Hermes gateway to activate Daedalus."
          : "Failed to load: " + loadErr
      ),
      restartResult && restartResult.ok
        ? React.createElement("div", { style: { color: "#4ade80", marginBottom: "8px", fontSize: "13px" } },
            "Restarting… reload this tab in a few seconds.")
        : null,
      isNotLoaded
        ? React.createElement("div", { style: { display: "flex", gap: "8px" } },
            React.createElement("button", {
              style: Object.assign({}, S.btn, restarting ? { opacity: 0.6, cursor: "not-allowed" } : {}),
              disabled: restarting,
              onClick: restartHermes,
            }, restarting ? "Restarting…" : "Restart Hermes"),
            React.createElement("button", { style: S.btn, onClick: load }, "Retry")
          )
        : React.createElement("button", { style: S.btn, onClick: load }, "Retry")
    );
  }

  var projects = data || [];

  return React.createElement("div", { style: S.wrap },
    React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "flex-start" } },
      React.createElement("div", null,
        React.createElement("h1", { style: S.h1 }, "Daedalus"),
        React.createElement("p", { style: S.subtitle }, projects.length, " project", projects.length !== 1 ? "s" : "")
      ),
      React.createElement(Button, {
        label: "+ Add Project", variant: "primary",
        onClick: function () { setShowAddProject(true); },
      })
    ),

    // Roster provisioning banner — shown when any of the 9 profiles are missing
    rosterStatus && !rosterStatus.all_provisioned ? React.createElement("div", {
      style: {
        border: "1px solid #444", borderRadius: "8px", padding: "12px 16px",
        marginBottom: "16px", display: "flex", gap: "12px", alignItems: "center",
        background: "rgba(255,255,255,0.02)",
      },
    },
      React.createElement("div", { style: { flex: "1 1 auto", minWidth: 0 } },
        React.createElement("div", { style: { fontSize: "13px", fontWeight: 600, color: "#ccc", marginBottom: "2px" } },
          "Worker Agents not provisioned"
        ),
        React.createElement("div", { style: { fontSize: "12px", color: "#888" } },
          "Run postinstall.py to install the 9 specialist agent profiles and enable automated dispatch."
        ),
        rosterResult ? React.createElement("div", {
          style: { fontSize: "11px", marginTop: "4px", color: rosterResult.ok ? "#4ade80" : "#f87171" },
        }, rosterResult.ok ? "Provisioned successfully." : "Error: " + (rosterResult.error || "failed")) : null
      ),
      React.createElement("div", { style: { flexShrink: 0 } },
        React.createElement(Button, {
          label: provisioningRoster ? "Installing…" : "Install Agents",
          variant: "small",
          disabled: !!provisioningRoster,
          onClick: provisionRoster,
        })
      )
    ) : null,

    projects.length === 0 ? React.createElement("div", { style: { textAlign: "center", padding: "40px", color: "#666" } },
      "No projects configured. Click \"+ Add Project\" to get started."
    ) : null,

    React.createElement("div", { style: S.grid },
      projects.map(function (p) {
        return React.createElement(ProjectCard, {
          key: p.name,
          project: p,
          onSelect: function (name) { setModalProject(name); }
        });
      })
    ),

    // Refresh button
    React.createElement("div", { style: { textAlign: "center", marginTop: "20px" } },
      React.createElement(Button, { label: "Refresh", onClick: load })
    ),

    // Footer: version + update + uninstall
    React.createElement("div", {
      style: { marginTop: "40px", paddingTop: "16px", borderTop: "1px solid #2a2a2a",
               display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "8px" }
    },
      React.createElement("div", { style: { fontSize: "12px", color: "#888" } },
        "Daedalus" + (pluginVersion ? " v" + pluginVersion : "")
      ),
      React.createElement("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
        updateResult ? React.createElement("span", {
          style: { fontSize: "11px", color: updateResult.ok ? "#4ade80" : "#f87171" }
        }, updateResult.ok ? "Updated — restart the gateway then reload this tab" : "Update failed: " + (updateResult.output || "").slice(0, 80)) : null,
        hasUpdate ? React.createElement("button", {
          onClick: updatePlugin,
          disabled: updating,
          style: Object.assign({}, S.btnSmall, { color: "#4ade80", borderColor: "#166534" },
            updating ? { opacity: 0.5 } : {})
        }, updating ? "Updating…" : ("Update available" + (latestVersion ? " → v" + latestVersion : ""))) : null,
        React.createElement("button", {
          onClick: function () { setShowUninstall(true); },
          style: Object.assign({}, S.btnSmall, { color: "#f87171", borderColor: "#7f1d1d" })
        }, "Uninstall")
      )
    ),

    // Modals
    modalProject ? React.createElement(ConfigModal, {
      name: modalProject,
      onClose: function () { setModalProject(null); load(); },
      onRemoved: function () { setModalProject(null); load(); }
    }) : null,
    showAddProject ? React.createElement(AddProjectModal, {
      onClose: function () { setShowAddProject(false); },
      onRegistered: function (name) { setShowAddProject(false); setSetupProject(name); }
    }) : null,
    setupProject ? React.createElement(ConfigModal, {
      name: setupProject,
      setupMode: true,
      onClose: function () { setSetupProject(null); load(); },
      onRemoved: function () { setSetupProject(null); load(); },
      onAbort: function () {
        var abortName = setupProject;
        setSetupProject(null);
        fetchJSON(apiProject(abortName), { method: "DELETE" }).catch(function () {});
        setShowAddProject(true);
      }
    }) : null,
    showUninstall ? React.createElement(UninstallModal, {
      onClose: function () { setShowUninstall(false); }
    }) : null
  );
}

if (plugins && plugins.register) {
  plugins.register("daedalus", App);
} else {
  throw new Error("window.__HERMES_PLUGINS__.register not available");
}
