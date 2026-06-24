/**
 * Pure dirty-state helper for the config modal (own module so it can be
 * unit-tested in plain node — see test-configDirty.js).
 *
 * The modal re-fetches the project config from the server every time it mounts
 * (or `name` changes), which silently discards unsaved edits — confusing for
 * users who navigate away, reload, or open a second tab before clicking Save
 * (see issue #66). isDirty() lets the modal surface an "unsaved changes" badge
 * so the user knows to save first.
 */

// Deterministic JSON with object keys sorted, so two structurally-equal configs
// compare equal regardless of key insertion order.
function stableStringify(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map(stableStringify).join(",") + "]";
  }
  var keys = Object.keys(value).sort();
  var parts = keys.map(function (k) {
    return JSON.stringify(k) + ":" + stableStringify(value[k]);
  });
  return "{" + parts.join(",") + "}";
}

// Whether `current` differs from the last-saved `pristine` snapshot. Returns
// false when either side is missing (nothing loaded yet → never "dirty").
function isDirty(pristine, current) {
  if (pristine == null || current == null) return false;
  return stableStringify(pristine) !== stableStringify(current);
}

module.exports = {
  stableStringify: stableStringify,
  isDirty: isDirty,
};
