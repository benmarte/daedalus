// GENERATED FROM src/App.jsx — DO NOT EDIT. Rebuild with: npm run build
var __HERMES_DAEDALUS_DASHBOARD__ = (() => {
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __commonJS = (cb, mod) => function __require() {
    return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
  };

  // src/deriveMethod.js
  var require_deriveMethod = __commonJS({
    "src/deriveMethod.js"(exports, module) {
      function deriveMethodFromDeliver2(deliver, notifications) {
        if (!deliver || !notifications || !Object.keys(notifications).length) return "";
        var prefix = String(deliver).split(":")[0].toLowerCase();
        var methods = Object.keys(notifications);
        for (var i = 0; i < methods.length; i++) {
          var m = methods[i].toLowerCase();
          if (m === prefix || m.indexOf(prefix) === 0) return methods[i];
        }
        if (notifications[deliver]) return deliver;
        return "";
      }
      module.exports = { deriveMethodFromDeliver: deriveMethodFromDeliver2 };
    }
  });

  // src/cronSchedule.js
  var require_cronSchedule = __commonJS({
    "src/cronSchedule.js"(exports, module) {
      function parseSchedule2(str) {
        if (!str || typeof str !== "string") {
          return { unit: "Custom", n: null, dow: null, dom: null, hour: null, minute: null, raw: str || "" };
        }
        var s = str.trim();
        if (!s) {
          return { unit: "Custom", n: null, dow: null, dom: null, hour: null, minute: null, raw: s };
        }
        var everyMatch = s.match(/^every\s+(\d+)\s*([mhd])$/i);
        if (everyMatch) {
          var n = everyMatch[1];
          var unitChar = everyMatch[2].toLowerCase();
          var unitMap = { m: "Minutes", h: "Hours", d: "Days" };
          return {
            unit: unitMap[unitChar],
            n,
            dow: null,
            dom: null,
            hour: null,
            minute: null,
            raw: null
          };
        }
        var bareMatch = s.match(/^(\d+)\s*([mhd])$/i);
        if (bareMatch) {
          var nBare = bareMatch[1];
          var unitCharBare = bareMatch[2].toLowerCase();
          var unitMapBare = { m: "Minutes", h: "Hours", d: "Days" };
          return {
            unit: unitMapBare[unitCharBare],
            n: nBare,
            dow: null,
            dom: null,
            hour: null,
            minute: null,
            raw: null
          };
        }
        var fields = s.split(/\s+/);
        if (fields.length === 5) {
          var minute = fields[0];
          var hour = fields[1];
          var dom = fields[2];
          var month = fields[3];
          var dow = fields[4];
          if (/^[0-6]$/.test(dow) && dom === "*" && month === "*") {
            return {
              unit: "Weekly",
              n: null,
              dow,
              dom: null,
              hour,
              minute,
              raw: null
            };
          }
          var domNum = parseInt(dom, 10);
          if (!isNaN(domNum) && domNum >= 1 && domNum <= 28 && dow === "*" && month === "*") {
            return {
              unit: "Monthly",
              n: null,
              dow: null,
              dom,
              hour,
              minute,
              raw: null
            };
          }
        }
        return {
          unit: "Custom",
          n: null,
          dow: null,
          dom: null,
          hour: null,
          minute: null,
          raw: s
        };
      }
      function buildSchedule2(state) {
        if (!state) return "";
        var unit = state.unit;
        if (unit === "Minutes") {
          return "every " + (state.n || "60") + "m";
        }
        if (unit === "Hours") {
          return "every " + (state.n || "1") + "h";
        }
        if (unit === "Days") {
          return "every " + (state.n || "1") + "d";
        }
        if (unit === "Weekly") {
          var m = state.minute || "0";
          var h = state.hour || "9";
          var d = state.dow || "1";
          return m + " " + h + " * * " + d;
        }
        if (unit === "Monthly") {
          var m2 = state.minute || "0";
          var h2 = state.hour || "9";
          var dom = state.dom || "1";
          return m2 + " " + h2 + " " + dom + " * *";
        }
        if (unit === "Custom") {
          return state.raw || "";
        }
        return "";
      }
      module.exports = { parseSchedule: parseSchedule2, buildSchedule: buildSchedule2 };
    }
  });

  // src/App.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) throw new Error("Hermes Plugin SDK not loaded");
  var plugins = window.__HERMES_PLUGINS__;
  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var SdkComponents = SDK.components || {};
  var SdkButton = SdkComponents.Button || null;
  var SdkCheckbox = SdkComponents.Checkbox || null;
  var deriveMethodFromDeliver = require_deriveMethod().deriveMethodFromDeliver;
  var cronSchedule = require_cronSchedule();
  var parseSchedule = cronSchedule.parseSchedule;
  var buildSchedule = cronSchedule.buildSchedule;
  var fetchJSON = SDK.fetchJSON;
  if (!fetchJSON && SDK.authedFetch) {
    fetchJSON = function(url, opts) {
      return SDK.authedFetch(url, opts).then(function(r) {
        return r.json();
      });
    };
  }
  if (!fetchJSON && window.__HERMES_SESSION_TOKEN__) {
    _st = window.__HERMES_SESSION_TOKEN__;
    fetchJSON = function(url, opts) {
      opts = opts || {};
      opts.headers = opts.headers || {};
      opts.headers["Authorization"] = "Bearer " + _st;
      if (opts.body && typeof opts.body === "object" && !opts.headers["Content-Type"]) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(opts.body);
      }
      return fetch(url, opts).then(function(r) {
        return r.json();
      });
    };
  }
  var _st;
  if (!fetchJSON) throw new Error("No fetch implementation available");
  var _rawFetchJSON = fetchJSON;
  fetchJSON = function(url, opts) {
    opts = opts || {};
    if (opts.body && typeof opts.body === "object") {
      opts.headers = opts.headers || {};
      if (!opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    return _rawFetchJSON(url, opts);
  };
  var API_PROJECTS = "/api/plugins/daedalus/projects";
  var apiProjectConfig = function(name) {
    return "/api/plugins/daedalus/project/" + encodeURIComponent(name) + "/config";
  };
  var apiMetaUrl = function(name, endpoint) {
    return "/api/plugins/daedalus/meta/" + endpoint + "?project=" + encodeURIComponent(name);
  };
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
      var mins = Math.floor(diff / 6e4);
      if (mins < 1) return "just now";
      if (mins < 60) return mins + "m ago";
      var hrs = Math.floor(mins / 60);
      if (hrs < 24) return hrs + "h ago";
      return Math.floor(hrs / 24) + "d ago";
    } catch (e) {
      return null;
    }
  }
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
    overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1e3, display: "flex", alignItems: "center", justifyContent: "center" },
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
    chipEmptyHint: { fontSize: "12px", color: "#666", fontStyle: "italic", padding: "4px 0" }
  };
  function Button(props) {
    if (SdkButton) {
      return React.createElement(SdkButton, props, props.label || props.children);
    }
    return React.createElement("button", {
      style: props.variant === "primary" ? S.btnPrimary : props.variant === "danger" ? S.btnDanger : props.variant === "small" ? S.btnSmall : S.btn,
      disabled: props.disabled || false,
      onClick: props.onClick,
      type: props.type || "button"
    }, props.label || props.children);
  }
  function Checkbox(props) {
    var labelSpan = React.createElement(
      "span",
      { style: { fontSize: "13px", color: "#ccc" } },
      props.label || ""
    );
    if (SdkCheckbox) {
      return React.createElement(
        "div",
        { style: S.toggleRow },
        React.createElement(SdkCheckbox, {
          checked: props.checked || false,
          onCheckedChange: function(v) {
            if (props.onChange) props.onChange(!!v);
          }
        }),
        labelSpan
      );
    }
    return React.createElement(
      "label",
      { style: S.toggleRow },
      React.createElement("input", {
        type: "checkbox",
        checked: props.checked || false,
        onChange: function(e) {
          if (props.onChange) props.onChange(e.target.checked);
        },
        style: { margin: 0 }
      }),
      labelSpan
    );
  }
  function TagMultiSelect(props) {
    var selected = props.selected || [];
    var options = props.options || [];
    var placeholder = props.placeholder || "+ add\u2026";
    var emptyHint = props.emptyHint || "no options found";
    var availableOptions = options.filter(function(opt) {
      return selected.indexOf(opt.value) === -1;
    });
    function remove(val) {
      if (props.onChange) {
        props.onChange(selected.filter(function(v) {
          return v !== val;
        }));
      }
    }
    function handleAdd(e) {
      var val = e.target.value;
      if (!val) return;
      if (props.onChange) {
        props.onChange(selected.concat([val]));
      }
      e.target.value = "";
    }
    return React.createElement(
      "div",
      null,
      // Chips row
      React.createElement(
        "div",
        { style: S.chipWrap },
        selected.map(function(val) {
          var opt = null;
          for (var i = 0; i < options.length; i++) {
            if (options[i].value === val) {
              opt = options[i];
              break;
            }
          }
          var labelText = opt ? opt.label : val;
          var colorDot = opt && opt.color ? React.createElement("span", {
            style: Object.assign({}, S.chipDot, { background: "#" + opt.color })
          }) : null;
          return React.createElement(
            "span",
            { key: val, style: S.chip },
            colorDot,
            React.createElement("span", { style: S.chipLabel }, labelText),
            React.createElement("button", {
              style: S.chipRemove,
              onClick: function(e) {
                e.preventDefault();
                remove(val);
              },
              title: "Remove " + labelText,
              type: "button"
            }, "\xD7")
          );
        })
      ),
      // Add dropdown
      React.createElement(
        "select",
        {
          style: Object.assign({}, S.select, { marginTop: selected.length > 0 ? "8px" : "2px", width: "100%" }),
          value: "",
          onChange: handleAdd,
          disabled: options.length === 0
        },
        React.createElement("option", { value: "", disabled: true }, placeholder),
        options.length === 0 ? React.createElement("option", { value: "", disabled: true }, emptyHint) : availableOptions.length === 0 ? React.createElement("option", { value: "", disabled: true }, "all selected") : availableOptions.map(function(opt) {
          return React.createElement("option", { key: opt.value, value: opt.value }, opt.label);
        })
      )
    );
  }
  function ProjectCard(props) {
    var p = props.project;
    var hasAttention = p.needs_attention && p.needs_attention.length > 0;
    var openPrs = p.open_prs;
    var hasRedCI = openPrs && openPrs.prs && openPrs.prs.some(function(pr) {
      return pr.ci_green === false;
    });
    var cardBorder = hasAttention ? "1px solid #f87171" : hasRedCI ? "1px solid #dc2626" : S.card.border;
    var kanbanCounts = p.kanban_summary;
    var cronInfo = p.cron;
    var trackingMode = p.tracking_mode;
    var sources = p.sources;
    var attentionCount = hasAttention ? p.needs_attention.length : 0;
    var prCount = openPrs ? openPrs.count : 0;
    return React.createElement(
      "div",
      {
        style: Object.assign({}, S.card, { border: cardBorder }),
        onClick: function() {
          props.onSelect(p.name);
        }
      },
      // Header row: name + badges
      React.createElement(
        "div",
        { style: S.cardHeader },
        React.createElement(
          "div",
          { style: { minWidth: 0, flex: "1 1 auto" } },
          React.createElement("div", { style: S.cardName }, p.name || "(unnamed)"),
          React.createElement("div", { style: S.cardRepo }, p.repo || "\u2014")
        ),
        React.createElement(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "4px", alignItems: "flex-end", flexShrink: 0 } },
          hasAttention ? React.createElement(
            "span",
            { style: Object.assign({}, S.badge, S.badgeRed), onClick: function(e) {
              e.stopPropagation();
            } },
            "\u26A0 " + attentionCount + " needing attention"
          ) : null,
          hasRedCI ? React.createElement(
            "span",
            { style: Object.assign({}, S.badge, S.badgeRed) },
            "\u25CF CI failing"
          ) : null,
          trackingMode === "github" ? React.createElement(
            "span",
            { style: Object.assign({}, S.badge, S.badgeNeutral) },
            "github"
          ) : React.createElement("span", { style: Object.assign({}, S.badge, S.badgeNeutral) }, "kanban")
        )
      ),
      // Stats row
      React.createElement(
        "div",
        { style: S.cardSection },
        React.createElement(
          "div",
          { style: S.cardRow },
          // Kanban counts
          kanbanCounts ? Object.keys(kanbanCounts).sort().map(function(status) {
            var dotColor = {};
            if (status === "done") dotColor = S.dotGreen;
            else if (status === "in_progress") dotColor = S.dotYellow;
            else if (status === "blocked" || status === "gave_up") dotColor = S.dotRed;
            else dotColor = S.dotGray;
            return React.createElement(
              "div",
              { key: status, style: S.cardRowItem },
              React.createElement("span", { style: Object.assign({}, S.dot, dotColor) }),
              status + ": " + kanbanCounts[status]
            );
          }) : React.createElement("span", { style: S.cardLabel }, "no kanban data"),
          // PRs
          openPrs ? React.createElement(
            "div",
            { style: S.cardRowItem },
            React.createElement("span", { style: Object.assign({}, S.dot, openPrs.prs && openPrs.prs.every(function(p2) {
              return p2.ci_green;
            }) ? S.dotGreen : S.dotYellow) }),
            prCount + " open PR" + (prCount !== 1 ? "s" : "")
          ) : null,
          // Cron
          cronInfo && cronInfo.name ? React.createElement(
            "div",
            { style: S.cardRowItem },
            (function() {
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
                onClick: function(e) {
                  e.stopPropagation();
                }
              }, badgeText);
            })()
          ) : null,
          cronInfo && cronInfo.schedule ? React.createElement(
            "div",
            { style: S.cardRowItem },
            React.createElement("span", { style: Object.assign({}, S.dot, S.dotGray) }),
            cronInfo.schedule
          ) : null,
          cronInfo && cronInfo.last_run ? React.createElement(
            "div",
            { style: S.cardRowItem },
            React.createElement("span", { style: Object.assign({}, S.dot, S.dotGray) }),
            "last run " + formatRelativeTime(cronInfo.last_run)
          ) : null
        )
      )
    );
  }
  var CRON_UNITS = ["Minutes", "Hours", "Days", "Weekly", "Monthly", "Custom (cron)"];
  var MINUTE_VALUES = ["1", "2", "3", "5", "10", "15", "20", "30", "45", "60"];
  var HOUR_VALUES = ["1", "2", "3", "4", "6", "8", "12"];
  var DAY_VALUES = ["1", "2", "3", "5", "7"];
  var HOUR_OPTIONS = [];
  for (hi = 0; hi < 24; hi++) {
    HOUR_OPTIONS.push(String(hi));
  }
  var hi;
  var DOM_OPTIONS = [];
  for (di = 1; di <= 28; di++) {
    DOM_OPTIONS.push(String(di));
  }
  var di;
  var DOW_LABELS = {
    "0": "Sunday",
    "1": "Monday",
    "2": "Tuesday",
    "3": "Wednesday",
    "4": "Thursday",
    "5": "Friday",
    "6": "Saturday"
  };
  function CronSchedule(props) {
    var savedValue = props.value || "";
    var parsed = parseSchedule(savedValue);
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
    useEffect(function() {
      var p = parseSchedule(savedValue);
      setUnit(p.unit || "Minutes");
      setN(p.n || "60");
      setDow(p.dow || "1");
      setHour(p.hour || "9");
      setMinute(p.minute || "0");
      setDom(p.dom || "1");
      setCustomRaw(p.raw || "");
    }, [savedValue]);
    function emit(u2, n2, dow2, hour2, minute2, dom2, raw) {
      var state = { unit: u2, n: n2, dow: dow2, hour: hour2, minute: minute2, dom: dom2, raw };
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
      if (props.onChange) props.onChange(val);
    }
    var secondRow = null;
    if (unit === "Minutes" || unit === "Hours" || unit === "Days") {
      var values;
      if (unit === "Minutes") values = MINUTE_VALUES;
      else if (unit === "Hours") values = HOUR_VALUES;
      else values = DAY_VALUES;
      secondRow = React.createElement(
        "label",
        { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Every"),
        React.createElement(
          "select",
          {
            style: S.select,
            value: n,
            onChange: function(e) {
              onNChange(e.target.value);
            }
          },
          values.map(function(v) {
            return React.createElement("option", { key: v, value: v }, v);
          })
        )
      );
    } else if (unit === "Weekly") {
      secondRow = [
        React.createElement(
          "label",
          { key: "dow", style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Day"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: dow,
              onChange: function(e) {
                onDowChange(e.target.value);
              }
            },
            ["0", "1", "2", "3", "4", "5", "6"].map(function(d) {
              return React.createElement("option", { key: d, value: d }, DOW_LABELS[d]);
            })
          )
        ),
        React.createElement(
          "label",
          { key: "hour", style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Time (hour)"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: hour,
              onChange: function(e) {
                onHourChange(e.target.value);
              }
            },
            HOUR_OPTIONS.map(function(h) {
              var padded = h.length === 1 ? "0" + h : h;
              return React.createElement("option", { key: h, value: h }, padded + ":00");
            })
          )
        ),
        React.createElement(
          "label",
          { key: "minute", style: { display: "flex", flexDirection: "column", flex: "0 0 80px", minWidth: "80px" } },
          React.createElement("span", { style: S.fieldLabel }, "Minute"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: minute,
              onChange: function(e) {
                onMinuteChange(e.target.value);
              }
            },
            ["0", "15", "30", "45"].map(function(m) {
              var paddedM = m.length === 1 ? "0" + m : m;
              return React.createElement("option", { key: m, value: m }, ":" + paddedM);
            })
          )
        )
      ];
    } else if (unit === "Monthly") {
      secondRow = [
        React.createElement(
          "label",
          { key: "dom", style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Day of Month"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: dom,
              onChange: function(e) {
                onDomChange(e.target.value);
              }
            },
            DOM_OPTIONS.map(function(d) {
              return React.createElement("option", { key: d, value: d }, d);
            })
          )
        ),
        React.createElement(
          "label",
          { key: "hour", style: S.field },
          React.createElement("span", { style: S.fieldLabel }, "Time (hour)"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: hour,
              onChange: function(e) {
                onHourChange(e.target.value);
              }
            },
            HOUR_OPTIONS.map(function(h) {
              var padded = h.length === 1 ? "0" + h : h;
              return React.createElement("option", { key: h, value: h }, padded + ":00");
            })
          )
        ),
        React.createElement(
          "label",
          { key: "minute", style: { display: "flex", flexDirection: "column", flex: "0 0 80px", minWidth: "80px" } },
          React.createElement("span", { style: S.fieldLabel }, "Minute"),
          React.createElement(
            "select",
            {
              style: S.select,
              value: minute,
              onChange: function(e) {
                onMinuteChange(e.target.value);
              }
            },
            ["0", "15", "30", "45"].map(function(m) {
              var paddedM = m.length === 1 ? "0" + m : m;
              return React.createElement("option", { key: m, value: m }, ":" + paddedM);
            })
          )
        )
      ];
    } else if (unit === "Custom (cron)") {
      secondRow = React.createElement(
        "label",
        { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Cron Expression"),
        React.createElement("input", {
          style: S.input,
          value: customRaw,
          placeholder: "e.g. */5 * * * * or every 2h",
          onChange: function(e) {
            onCustomChange(e.target.value);
          }
        })
      );
    }
    return React.createElement(
      "div",
      { style: S.fieldRow },
      React.createElement(
        "label",
        { style: S.field },
        React.createElement("span", { style: S.fieldLabel }, "Frequency"),
        React.createElement(
          "select",
          {
            style: S.select,
            value: unit,
            onChange: function(e) {
              onUnitChange(e.target.value);
            }
          },
          CRON_UNITS.map(function(u2) {
            return React.createElement("option", { key: u2, value: u2 }, u2);
          })
        )
      ),
      secondRow
    );
  }
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
    max_open_prs: "Max Open PRs"
  };
  function ConfigModal(props) {
    var name = props.name;
    var s = useState(null);
    var config = s[0], setConfig = s[1];
    var l = useState(true);
    var loading = l[0], setLoading = l[1];
    var e = useState(null);
    var loadErr = e[0], setLoadErr = e[1];
    var sv = useState(false);
    var saving = sv[0], setSaving = sv[1];
    var r = useState(null);
    var result = r[0], setResult = r[1];
    var fe = useState(null);
    var fieldErrors = fe[0], setFieldErrors = fe[1];
    var ns = useState({});
    var notifications = ns[0], setNotifications = ns[1];
    var sm = useState("");
    var selectedMethod = sm[0], setSelectedMethod = sm[1];
    var td = useState(null);
    var testDeliverStatus = td[0], setTestDeliverStatus = td[1];
    var br = useState([]);
    var branches = br[0], setBranches = br[1];
    var la = useState([]);
    var labels = la[0], setLabels = la[1];
    var st = useState([]);
    var statuses = st[0], setStatuses = st[1];
    var gp = useState([]);
    var ghProjects = gp[0], setGhProjects = gp[1];
    var load = useCallback(function() {
      setLoading(true);
      setLoadErr(null);
      setFieldErrors(null);
      fetchJSON(apiProjectConfig(name)).then(function(data) {
        if (!data.cron) data.cron = {};
        if (!data.cron.schedule) data.cron.schedule = "every 60m";
        if (!data.vcs) data.vcs = {};
        if (!data.vcs.target_branch) data.vcs.target_branch = "main";
        if (!data.vcs.branch_prefix) data.vcs.branch_prefix = "fix";
        if (!data.vcs.pr_title_prefix) data.vcs.pr_title_prefix = "fix:";
        if (!data.issues) data.issues = {};
        if (!data.issues.processing) data.issues.processing = {};
        if (data.issues.processing.max_issues_per_run == null) data.issues.processing.max_issues_per_run = 20;
        if (data.issues.processing.max_open_prs == null) data.issues.processing.max_open_prs = 5;
        setConfig(data);
        setLoading(false);
      }).catch(function(err) {
        setLoadErr(String(err && err.message || err));
        setLoading(false);
      });
    }, [name]);
    useEffect(function() {
      load();
    }, [load]);
    useEffect(function() {
      fetchJSON("/api/plugins/daedalus/meta/notifications").then(function(data) {
        setNotifications(data || {});
      }).catch(function() {
        setNotifications({});
      });
    }, []);
    useEffect(function() {
      fetchJSON(apiMetaUrl(name, "branches")).then(function(data) {
        setBranches(data && data.branches ? data.branches.sort() : []);
      }).catch(function() {
        setBranches([]);
      });
    }, [name]);
    useEffect(function() {
      fetchJSON(apiMetaUrl(name, "labels")).then(function(data) {
        setLabels(data && data.labels ? data.labels : []);
      }).catch(function() {
        setLabels([]);
      });
    }, [name]);
    useEffect(function() {
      fetchJSON(apiMetaUrl(name, "projects")).then(function(data) {
        setGhProjects(data && data.projects ? data.projects : []);
      }).catch(function() {
        setGhProjects([]);
      });
    }, [name]);
    useEffect(function() {
      if (!config) return;
      var deliver = getIn(config, ["cron", "deliver"], "");
      var derived = deriveMethodFromDeliver(deliver, notifications);
      if (derived) setSelectedMethod(derived);
    }, [config, notifications]);
    function updateField(path, value) {
      setConfig(function(prev) {
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
      setConfig(function(prev) {
        var next = JSON.parse(JSON.stringify(prev));
        var sources2 = next.sources || {};
        var entry = sources2[key] || {};
        entry.enabled = !entry.enabled;
        sources2[key] = entry;
        next.sources = sources2;
        return next;
      });
    }
    function save() {
      setSaving(true);
      setResult(null);
      setFieldErrors(null);
      var body = {};
      if (config.name !== void 0) {
        body.name = config.name;
      }
      if (config.tracking) {
        body.tracking = config.tracking;
      }
      if (config.vcs) body.vcs = config.vcs;
      if (config.cron) body.cron = config.cron;
      if (config.sources) body.sources = config.sources;
      if (config.issues) body.issues = config.issues;
      fetchJSON(apiProjectConfig(name), { method: "POST", body }).then(function(res) {
        setSaving(false);
        if (res && res.status === "saved") {
          if (res.cron) {
            var cr = res.cron;
            var cronMsg = cr.name || "";
            if (cr.error) {
              cronMsg += " \xB7 \u26A0\uFE0F " + cr.error;
            } else if (cr.cron && cr.cron !== "skipped") {
              cronMsg += " \xB7 cron " + cr.cron;
            }
            setResult({ ok: true, msg: "Saved \xB7 " + cronMsg });
          } else {
            setResult({ ok: true, msg: "Saved" });
          }
          setTimeout(function() {
            props.onClose();
          }, 1200);
        } else {
          setResult({ ok: false, errors: ["Unexpected response"] });
        }
      }).catch(function(err) {
        setSaving(false);
        if (err && err.detail) {
          var msg = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
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
    function testDeliver() {
      var deliver = getIn(config, ["cron", "deliver"], "");
      if (!deliver) {
        setTestDeliverStatus({ ok: false, msg: "no delivery target selected" });
        return;
      }
      setTestDeliverStatus({ ok: null, msg: "Sending\u2026" });
      fetchJSON("/api/plugins/daedalus/meta/test-deliver", {
        method: "POST",
        body: { deliver }
      }).then(function(r2) {
        if (r2 && r2.ok) {
          setTestDeliverStatus({ ok: true, msg: "\u2713 Sent to " + r2.target });
        } else {
          var errMsg = r2 && r2.error || "send failed";
          setTestDeliverStatus({ ok: false, msg: "\u2717 " + errMsg });
        }
      }).catch(function(err) {
        setTestDeliverStatus({ ok: false, msg: "\u2717 " + String(err && err.message || err) });
      });
    }
    if (loading) return React.createElement(
      "div",
      { style: S.overlay, onClick: props.onClose },
      React.createElement(
        "div",
        { style: S.modal, onClick: function(e2) {
          e2.stopPropagation();
        } },
        React.createElement("div", { style: { textAlign: "center", padding: "40px", color: "#888" } }, "Loading config\u2026")
      )
    );
    if (loadErr) return React.createElement(
      "div",
      { style: S.overlay, onClick: props.onClose },
      React.createElement(
        "div",
        { style: S.modal, onClick: function(e2) {
          e2.stopPropagation();
        } },
        React.createElement(
          "div",
          { style: S.modalHeader },
          React.createElement("span", { style: S.modalTitle }, name),
          React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "\xD7")
        ),
        React.createElement("div", { style: S.err }, "Failed to load config: ", loadErr),
        React.createElement("button", { style: S.btn, onClick: load }, "Retry")
      )
    );
    var sources = config && config.sources ? Object.keys(config.sources).filter(function(k) {
      return k !== "secret";
    }) : [];
    return React.createElement(
      "div",
      { style: S.overlay, onClick: props.onClose },
      React.createElement(
        "div",
        { style: S.modal, onClick: function(e2) {
          e2.stopPropagation();
        } },
        // Header
        React.createElement(
          "div",
          { style: S.modalHeader },
          React.createElement("span", { style: S.modalTitle }, "Edit: ", name),
          React.createElement("button", { style: S.btnSmall, onClick: props.onClose }, "\xD7")
        ),
        // Read-only identity (full-width, stacked, label + bare value)
        React.createElement(
          "div",
          { style: { marginBottom: "12px" } },
          React.createElement("div", { style: S.fieldLabel }, FIELD_LABELS.repo),
          React.createElement("span", { style: Object.assign({}, S.readOnlyText, { display: "block", width: "100%" }) }, config.repo || "\u2014")
        ),
        React.createElement(
          "div",
          { style: { marginBottom: "12px" } },
          React.createElement("div", { style: S.fieldLabel }, FIELD_LABELS.workdir),
          React.createElement("span", { style: Object.assign({}, S.readOnlyText, { display: "block", width: "100%" }) }, config.workdir || "\u2014")
        ),
        // Editable: tracking (GitHub mode shows board select + statuses)
        React.createElement("div", { style: S.section }, "Tracking"),
        (function() {
          var isGitHub = !!(config.sources && config.sources.github_issues && config.sources.github_issues.enabled);
          var boardNum = getIn(config, ["tracking", "github_project_number"], null);
          var hasProjects = ghProjects && ghProjects.length > 0;
          var hasStatuses = statuses && statuses.length > 0;
          return [
            // GitHub Project board select (only in GitHub mode) — always a <select>
            isGitHub ? React.createElement(
              "div",
              { key: "track-board", style: S.fieldRow },
              React.createElement(
                "label",
                { style: S.field },
                React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.github_project_number),
                React.createElement(
                  "select",
                  {
                    style: S.select,
                    value: boardNum != null ? String(boardNum) : "",
                    onChange: function(e2) {
                      var v = e2.target.value.trim();
                      if (v === "") {
                        updateField("tracking.github_project_number", void 0);
                        setStatuses([]);
                      } else {
                        var n = parseInt(v, 10);
                        if (!isNaN(n)) {
                          updateField("tracking.github_project_number", n);
                          fetchJSON(apiMetaUrl(name, "statuses") + "&github_project_number=" + n).then(function(data) {
                            setStatuses(data && data.statuses ? data.statuses : []);
                          }).catch(function() {
                            setStatuses([]);
                          });
                        }
                      }
                    }
                  },
                  React.createElement("option", { value: "" }, "\u2014 none \u2014"),
                  ghProjects.map(function(p) {
                    return React.createElement("option", { key: p.number, value: String(p.number) }, "#" + p.number + " " + (p.title || ""));
                  })
                ),
                !hasProjects ? React.createElement(
                  "span",
                  { style: { fontSize: "11px", color: "#666", display: "block", marginTop: "2px" } },
                  "no open project boards found for this repo owner"
                ) : null
              )
            ) : null,
            // Statuses to process (TagMultiSelect, only in GitHub mode when statuses exist)
            isGitHub && boardNum && hasStatuses ? React.createElement(
              "div",
              { key: "track-statuses", style: { marginBottom: "12px" } },
              React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.ready_statuses),
              React.createElement(TagMultiSelect, {
                selected: getIn(config, ["tracking", "ready_statuses"], ["Ready"]),
                options: statuses.map(function(s2) {
                  return { value: s2, label: s2 };
                }),
                onChange: function(arr) {
                  updateField("tracking.ready_statuses", arr);
                },
                placeholder: "+ add status\u2026",
                emptyHint: "no statuses found"
              })
            ) : null
          ];
        })(),
        // Editable: vcs
        React.createElement("div", { style: S.section }, "VCS"),
        React.createElement(
          "div",
          { style: S.fieldRow },
          React.createElement(
            "label",
            { style: S.field },
            React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.target_branch),
            branches.length > 0 ? React.createElement(
              "select",
              {
                style: S.select,
                value: getIn(config, ["vcs", "target_branch"], ""),
                onChange: function(e2) {
                  updateField("vcs.target_branch", e2.target.value);
                }
              },
              React.createElement("option", { value: "" }, "\u2014 none \u2014"),
              branches.map(function(b) {
                return React.createElement("option", { key: b, value: b }, b);
              })
            ) : React.createElement("input", {
              style: S.input,
              value: getIn(config, ["vcs", "target_branch"], ""),
              placeholder: "main",
              onChange: function(e2) {
                updateField("vcs.target_branch", e2.target.value);
              }
            })
          ),
          React.createElement(
            "label",
            { style: S.field },
            React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.branch_prefix),
            React.createElement("input", {
              style: S.input,
              value: getIn(config, ["vcs", "branch_prefix"], ""),
              placeholder: "fix",
              onChange: function(e2) {
                updateField("vcs.branch_prefix", e2.target.value);
              }
            })
          ),
          React.createElement(
            "label",
            { style: S.field },
            React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.pr_title_prefix),
            React.createElement("input", {
              style: S.input,
              value: getIn(config, ["vcs", "pr_title_prefix"], ""),
              placeholder: "fix:",
              onChange: function(e2) {
                updateField("vcs.pr_title_prefix", e2.target.value);
              }
            })
          )
        ),
        // Editable: Cron
        React.createElement("div", { style: S.section }, "Cron"),
        React.createElement(CronSchedule, {
          value: getIn(config, ["cron", "schedule"], ""),
          onChange: function(v) {
            updateField("cron.schedule", v);
          }
        }),
        React.createElement(
          "div",
          { style: S.fieldRow },
          // Cascade deliver: method → channel. Built from /meta/notifications endpoint.
          (function() {
            var methodNames = Object.keys(notifications).sort();
            var channelOpts = selectedMethod && notifications[selectedMethod] ? notifications[selectedMethod] : [];
            var savedDeliver = getIn(config, ["cron", "deliver"], "");
            var selectedChannel = savedDeliver;
            if (selectedChannel && channelOpts.indexOf(selectedChannel) === -1 && selectedMethod) {
              selectedChannel = "";
            }
            if (methodNames.length === 0) {
              return React.createElement(
                "label",
                { key: "deliver", style: S.field },
                React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.deliver),
                React.createElement("input", {
                  style: S.input,
                  value: savedDeliver,
                  placeholder: "e.g. slack:tasks",
                  onChange: function(e2) {
                    updateField("cron.deliver", e2.target.value);
                  }
                })
              );
            }
            return [
              React.createElement(
                "label",
                { key: "deliver-method", style: S.field },
                React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.deliver),
                React.createElement(
                  "select",
                  {
                    style: S.select,
                    value: selectedMethod,
                    onChange: function(e2) {
                      setSelectedMethod(e2.target.value);
                      updateField("cron.deliver", "");
                    }
                  },
                  React.createElement("option", { value: "" }, "\u2014 default \u2014"),
                  methodNames.map(function(m) {
                    return React.createElement("option", { key: m, value: m }, m);
                  })
                )
              ),
              selectedMethod ? React.createElement(
                "label",
                { key: "deliver-channel", style: S.field },
                React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.channel),
                channelOpts.length > 0 ? React.createElement(
                  "select",
                  {
                    style: S.select,
                    value: selectedChannel,
                    onChange: function(e2) {
                      updateField("cron.deliver", e2.target.value);
                    }
                  },
                  React.createElement("option", { value: "" }, "\u2014 none \u2014"),
                  channelOpts.map(function(ch) {
                    return React.createElement("option", { key: ch, value: ch }, ch);
                  })
                ) : React.createElement("input", {
                  style: S.input,
                  value: selectedChannel,
                  placeholder: "e.g. slack:tasks",
                  onChange: function(e2) {
                    updateField("cron.deliver", e2.target.value);
                  }
                })
              ) : null
            ];
          })(),
          // "Send test message" button — uses current in-modal deliver value
          (function() {
            var currentDeliver = getIn(config, ["cron", "deliver"], "");
            var hasTarget = !!currentDeliver;
            var isSending = testDeliverStatus && testDeliverStatus.ok === null;
            var statusStyle = null;
            if (testDeliverStatus && testDeliverStatus.ok === true) {
              statusStyle = Object.assign({}, S.ok, { fontSize: "12px", marginLeft: "8px" });
            } else if (testDeliverStatus && testDeliverStatus.ok === false) {
              statusStyle = Object.assign({}, S.err, { fontSize: "12px", marginLeft: "8px" });
            }
            return [
              React.createElement(
                "label",
                { key: "test-deliver-btn", style: Object.assign({}, S.field, { flex: "0 0 auto", justifyContent: "flex-end", minWidth: "auto" }) },
                React.createElement("button", {
                  style: Object.assign({}, S.btnSmall, { opacity: hasTarget && !isSending ? 1 : 0.5, marginTop: "20px" }),
                  disabled: !hasTarget || isSending,
                  onClick: testDeliver,
                  type: "button"
                }, isSending ? "Sending\u2026" : "Send test message")
              ),
              testDeliverStatus ? React.createElement("span", { key: "test-deliver-status", style: statusStyle }, testDeliverStatus.msg) : null
            ];
          })()
        ),
        // Editable: Source toggles with human-readable labels and enabled/disabled status
        sources.length > 0 ? React.createElement("div", { style: S.section }, "Sources") : null,
        sources.length > 0 ? React.createElement(
          "div",
          { style: { marginBottom: "12px" } },
          sources.map(function(key) {
            var enabled = !!(config.sources[key] && config.sources[key].enabled);
            var labelMap = { github_issues: "GitHub Issues", local_specs: "Local Specs", kanban_triage: "Kanban Triage" };
            var humanLabel = labelMap[key] || key;
            var statusSuffix = enabled ? " (enabled)" : " (disabled)";
            return React.createElement(Checkbox, { key, label: humanLabel + statusSuffix, checked: enabled, onChange: function() {
              toggleSource(key);
            } });
          })
        ) : null,
        // Editable: Labels (TagMultiSelect, GitHub mode only)
        (function() {
          var isGitHub = !!(config.sources && config.sources.github_issues && config.sources.github_issues.enabled);
          if (!isGitHub) return null;
          var labelOptions = (labels || []).map(function(l2) {
            return { value: l2.name, label: l2.name, color: l2.color };
          });
          return [
            React.createElement("div", { key: "labels-section", style: S.section }, "Issue Labels"),
            React.createElement(
              "div",
              { key: "labels-container", style: { marginBottom: "12px" } },
              React.createElement(TagMultiSelect, {
                selected: getIn(config, ["issues", "filters", "labels"], []),
                options: labelOptions,
                onChange: function(arr) {
                  updateField("issues.filters.labels", arr);
                },
                placeholder: "+ add label\u2026",
                emptyHint: "no labels found"
              })
            )
          ];
        })(),
        // Throughput caps
        React.createElement("div", { style: S.section }, "Throughput"),
        React.createElement(
          "div",
          { style: S.fieldRow },
          React.createElement(
            "label",
            { style: S.field },
            React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.max_issues_per_run),
            React.createElement("input", {
              style: S.input,
              type: "number",
              value: getIn(config, ["issues", "processing", "max_issues_per_run"], ""),
              placeholder: "20",
              onChange: function(e2) {
                var v = e2.target.value.trim();
                if (v === "") {
                  updateField("issues.processing.max_issues_per_run", void 0);
                } else {
                  var n = parseInt(v, 10);
                  if (!isNaN(n)) updateField("issues.processing.max_issues_per_run", n);
                }
              }
            })
          ),
          React.createElement(
            "label",
            { style: S.field },
            React.createElement("span", { style: S.fieldLabel }, FIELD_LABELS.max_open_prs),
            React.createElement("input", {
              style: S.input,
              type: "number",
              value: getIn(config, ["issues", "processing", "max_open_prs"], ""),
              placeholder: "5",
              onChange: function(e2) {
                var v = e2.target.value.trim();
                if (v === "") {
                  setConfig(function(prev) {
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
        // Errors
        fieldErrors && fieldErrors.length > 0 ? React.createElement(
          "div",
          { style: { marginBottom: "8px" } },
          fieldErrors.map(function(errMsg, i) {
            return React.createElement("div", { key: i, style: { color: "#f87171", fontSize: "12px", margin: "2px 0", padding: "4px 8px", background: "rgba(248,113,113,0.08)", borderRadius: "4px" } }, errMsg);
          })
        ) : null,
        result && !result.ok ? React.createElement(
          "div",
          { style: { marginBottom: "8px" } },
          (result.errors || []).map(function(errMsg, i) {
            return React.createElement("div", { key: i, style: S.err }, errMsg);
          })
        ) : null,
        result && result.ok ? React.createElement("div", { style: S.ok }, result.msg) : null,
        // Actions
        React.createElement(
          "div",
          { style: S.modalBar },
          React.createElement(Button, { label: saving ? "Saving\u2026" : "Save", variant: "primary", disabled: saving, onClick: save }),
          React.createElement(Button, { label: "Cancel", onClick: props.onClose })
        )
      )
    );
  }
  function App() {
    var s = useState(null);
    var data = s[0], setData = s[1];
    var l = useState(true);
    var loading = l[0], setLoading = l[1];
    var e = useState(null);
    var loadErr = e[0], setLoadErr = e[1];
    var m = useState(null);
    var modalProject = m[0], setModalProject = m[1];
    var load = useCallback(function() {
      setLoading(true);
      setLoadErr(null);
      fetchJSON(API_PROJECTS).then(function(projects2) {
        setData(projects2);
        setLoading(false);
      }).catch(function(err) {
        setLoadErr(String(err && err.message || err));
        setLoading(false);
      });
    }, []);
    useEffect(function() {
      load();
    }, [load]);
    if (loading) return React.createElement(
      "div",
      { style: S.wrap },
      React.createElement("div", { style: { textAlign: "center", padding: "60px", color: "#888" } }, "Loading projects\u2026")
    );
    if (loadErr) return React.createElement(
      "div",
      { style: S.wrap },
      React.createElement("div", { style: S.err }, "Failed to load: ", loadErr),
      React.createElement("button", { style: S.btn, onClick: load }, "Retry")
    );
    var projects = data || [];
    return React.createElement(
      "div",
      { style: S.wrap },
      React.createElement("h1", { style: S.h1 }, "Daedalus"),
      React.createElement("p", { style: S.subtitle }, projects.length, " project", projects.length !== 1 ? "s" : ""),
      projects.length === 0 ? React.createElement(
        "div",
        { style: { textAlign: "center", padding: "40px", color: "#666" } },
        "No projects configured. Add projects to your daedalus.yaml to get started."
      ) : null,
      React.createElement(
        "div",
        { style: S.grid },
        projects.map(function(p) {
          return React.createElement(ProjectCard, {
            key: p.name,
            project: p,
            onSelect: function(name) {
              setModalProject(name);
            }
          });
        })
      ),
      // Refresh button
      React.createElement(
        "div",
        { style: { textAlign: "center", marginTop: "20px" } },
        React.createElement(Button, { label: "Refresh", onClick: load })
      ),
      // Modal
      modalProject ? React.createElement(ConfigModal, {
        name: modalProject,
        onClose: function() {
          setModalProject(null);
          load();
        }
      }) : null
    );
  }
  if (plugins && plugins.register) {
    plugins.register("daedalus", App);
  } else {
    throw new Error("window.__HERMES_PLUGINS__.register not available");
  }
})();
